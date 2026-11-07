"""The improvement controller: one state machine that decides when the agent may change itself and when it must stop
(spec §9.3-§9.4; research §3.9, §4.4, §4.8). Persisted in the store table `improve_state`, so it survives restarts.
All waits are WATCHED hours (monitor.watched_hours: the union of observation windows).

    idle --trigger--> [gates] --pass--> shadow --promote--> probation --48 wh--> idle | maintenance
                         |                 |                    |
                         +--reject---------+--reject------------+--rollback (failed_in_prod)--> idle | maintenance

- Triggers (evidence-gated; empty rounds alone never trigger): >= 3 failures with the same cause, a health alarm
  (>= 3 events of one source and kind), a CUSUM alarm, or the weekly review every 168 watched hours (a review due
  during a cooldown waits for its end; it is never lost).
- One change in flight: nothing new starts while a proposal is in shadow or probation (`InFlight`).
- Cooldown after a rejection: 8, 16, 32, then 64 watched hours (consecutive rejections; a rollback counts). A
  trigger that produced no proposal starts the base cooldown (8 wh) without counting as a rejection. An accepted
  change resets the count; a CUSUM drift alarm resets the wait (research §3.9 rule 4).
- Maintenance mode after 3 consecutive rejections, or when the smallest effect 2 weeks of watched data (deals, tier
  1-3) could detect exceeds the largest plausible gain of one item. Only alarms and the weekly review reopen it,
  for one targeted change that ends back in maintenance. The owner can leave it.
- Doors: judgment (alias, scam keyword) -> shadow -> probation; arm (query, cadence) -> probation (replay cannot
  score a query nobody ran); llm (prompt) -> stops at "owner_review": the loop never promotes a prompt (ruling 5),
  the owner applies it (`apply` command), which runs the must-pass set with the LIVE model first.
- Probation rolls back on a CUSUM breakage (Fisher against the sentinel), a failed daily must-pass, or a health
  rise: a source/kind whose events since the promotion are >= 3 and > 2x the equal watched window before it, while
  the sentinel's sources stay healthy.

Robustness:
- Promotion and rollback commit the version status and the controller state in ONE transaction, then write
  playbook/items.yaml. `_reconcile` (start of every round and at start-up) repairs a crash in between: a state
  that disagrees with the active version enters probation for the running version (never unprobated), and a file
  that is an older version is rewritten. A file that is no known version is a human edit: stored as a new baseline
  ("owner edit"), any change in flight is aborted, and the rounds go on.
- Owner commands (rollback, maintenance, label, apply) are queued in `improve_commands`; `on_round` applies them
  at its start and again after its own decision. SINGLE WRITER: only the agent-lock holder runs a step. One step =
  one transaction, checked by `_attempt` before its commit; the `rev` compare-and-swap is an assertion. A step whose
  transaction was ended under it (another coroutine committing on the same connection) or whose state was saved by
  someone else is rolled back and reported (health event); its events are not returned.
- No real I/O is awaited inside an open transaction: the reflection and advisor calls run before their step's first
  write, an owner apply's LIVE must-pass before its step starts (`_prepare_live`); an apply whose live check does
  not match the current champion, or that has no model, stays queued and is skipped: safety commands (rollback,
  maintenance, label) never wait behind it.
"""
from __future__ import annotations

import copy
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .. import config as dhconfig
from .. import ledger
from . import gates, monitor, probation, reflect, replay, shadow
from .cusum import PoissonCusum, make
from .playbook import migrate as pb_migrate
from .playbook import (ROOT, Delta, Playbook, PlaybookError, active_version, ensure_baseline, prompt_sha, set_status,
                       version_sha)

log = logging.getLogger(__name__)
STATE_SQL = "create table if not exists improve_state (key text primary key, value text not null)"
COMMANDS_SQL = """create table if not exists improve_commands (
  id integer primary key, cmd text not null, args text not null, created_at text not null, applied_at text,
  result text)"""
COMMANDS = ("rollback", "maintenance", "label", "apply")
PHASES = ("idle", "shadow", "probation", "maintenance")
PROPOSAL_COLUMNS = ("version_id integer", "stage text", "decided_at text", "detail text")
BASE_COOLDOWN = gates.COOLDOWN_WATCHED_HOURS[0]
NO_LIVE_MODEL = "apply waits for the running agent: its live must-pass needs the model"
STALE_LIVE = "apply waits for the next step: its live must-pass checked another champion"


class InFlight(RuntimeError):
    """A change is already in shadow or probation (one change in flight)."""


class StateConflict(RuntimeError):
    """Another writer saved the controller state since this one loaded it (impossible under the agent lock)."""


class StepAborted(RuntimeError):
    """A step was rolled back because of a state conflict; nothing of it was applied."""


class StepInterrupted(RuntimeError):
    """The step's transaction was ended under it (a commit or rollback by someone else on the connection)."""


class _Deferred(Exception):
    """An owner command that cannot run in this step (an apply without its live check): it stays queued."""


def _default() -> dict:
    return {"phase": "idle", "maintenance": False, "maintenance_reason": None, "proposal_id": None,
            "challenger": None, "champion": None, "previous": None, "door": None, "shadow_started_wh": None,
            "probation": None, "promoted_wh": None, "consecutive_rejections": 0, "cooldown_until_wh": 0.0,
            "last_review_wh": 0.0, "last_health_id": 0, "health_pending": {}, "last_mark_id": 0, "monitor": None,
            "delta_sources": [], "file_pending": None, "events": []}


def _sources_of(item) -> list[str]:
    """Sources a delta's item names: a cadence's source, a query's explicit targets ('all' names none)."""
    if item.kind == "cadence":
        return [item.target.strip().lower()]
    if item.kind == "query":
        t = item.target.strip().lower()
        return [] if t in ("all", "*") else [x.strip() for x in t.split(",") if x.strip()]
    return []


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def enqueue(store, cmd: str, **args) -> int:
    """Queue an owner command for the controller (the CLI never edits the state directly)."""
    if cmd not in COMMANDS:
        raise ValueError(f"unknown command {cmd!r}")
    store.ensure_table(COMMANDS_SQL)
    cur = store.conn.execute("insert into improve_commands (cmd, args, created_at) values (?,?,?)",
                             (cmd, json.dumps(args), _now()))
    store.conn.commit()
    return cur.lastrowid


def command_result(store, cid: int) -> dict | None:
    store.ensure_table(COMMANDS_SQL)
    r = store.conn.execute("select applied_at, result from improve_commands where id=?", (cid,)).fetchone()
    return None if r is None or r["applied_at"] is None else json.loads(r["result"] or "{}")


class Controller:
    def __init__(self, store, *, root: Path = ROOT, cfg=None, llm=None,
                 clock: Callable[[], float] | None = None, sentinel_version: int | None = None):
        self.store, self.root = store, Path(root)
        self.cfg = cfg if cfg is not None else dhconfig.load(ROOT)
        self.llm = llm
        self.clock = clock or (lambda: monitor.watched_hours(store))
        store.ensure_table(STATE_SQL)
        store.ensure_table(COMMANDS_SQL)
        if "rev" not in {r["name"] for r in store.conn.execute("pragma table_info(improve_state)")}:
            store.ensure_table("alter table improve_state add column rev integer not null default 0")
        monitor.migrate(store)
        reflect.migrate(store)
        shadow.migrate(store)
        replay.migrate(store)
        pb_migrate(store)
        ledger.migrate(store)  # every schema check runs here, never inside a step's transaction
        have = {r["name"] for r in store.conn.execute("pragma table_info(proposals)")}
        for col in PROPOSAL_COLUMNS:
            if col.split()[0] not in have:
                store.ensure_table(f"alter table proposals add column {col}")
        self.rev = 0
        self._new: list[dict] = []
        self._live: dict[int, tuple[str, dict]] = {}
        self._deferred: set[str] = set()
        self._applied = 0
        self._c0 = 0
        self._sentinel_version = sentinel_version
        self.s = self._load()  # READ-ONLY construction: `dh status` never writes; on_round / commands write

    def _init(self, wh: float) -> None:
        """First real use: the file becomes baseline v1 and the state starts (saved by the next _commit)."""
        if self.s["champion"] is not None:
            return
        self.s["champion"] = ensure_baseline(self.store, self.root, commit=False)
        self.s["sentinel_version"] = self._sentinel_version or self.s["champion"]
        self.s["last_review_wh"] = wh
        # history from before the loop existed (older than a day) never counts towards a first alarm
        self.s["last_health_id"] = int(self.store.conn.execute(
            "select coalesce(max(id),0) from health where at < datetime('now','-1 day')").fetchone()[0])

    # ------------------------------------------------------------ persistence
    def _load(self) -> dict:
        r = self.store.conn.execute("select value, rev from improve_state where key='controller'").fetchone()
        self.rev = int(r["rev"]) if r else 0
        st = {**_default(), **(json.loads(r["value"]) if r else {})}
        self._snap = copy.deepcopy(st)
        return st

    def _save_cas(self) -> None:
        """Compare-and-swap on `rev` inside the open transaction (nothing committed here)."""
        self.s["events"] = self.s["events"][-50:]
        value = json.dumps(self.s, default=str)
        cur = self.store.conn.execute("update improve_state set value=?, rev=rev+1 where key='controller' and rev=?",
                                      (value, self.rev))
        if cur.rowcount == 0:
            if self.store.conn.execute("select 1 from improve_state where key='controller'").fetchone() is not None:
                raise StateConflict("the controller state changed since it was loaded")
            self.store.conn.execute("insert into improve_state (key, value, rev) values ('controller', ?, 1)", (value,))
            self.rev = 1
        else:
            self.rev += 1

    def _commit(self) -> None:
        """THE commit point of a step: the state (CAS on `rev`, an assertion: only the agent-lock holder writes) and
        every row of the step (version statuses, proposals, labels, command marks, health events) in ONE commit,
        then playbook/items.yaml. A conflict means a second writer, i.e. a bug: the whole step is rolled back (no
        partial effect), a health event `improve_state_conflict` is recorded, the state is reloaded and the step
        raises StepAborted (the next round goes on from the stored state)."""
        try:
            self._save_cas()
        except StateConflict:
            self.store.conn.rollback()
            self.store.conn.execute("insert into health (source, kind, detail) values (?,?,?)",
                                    ("improve", "improve_state_conflict",
                                     "the controller state changed under the agent-lock holder; step rolled back"))
            self.store.conn.commit()
            self.s = self._load()
            raise StepAborted("improve_state_conflict") from None
        self.store.conn.commit()
        self._snap = copy.deepcopy(self.s)
        if self.s.get("file_pending"):
            self._sync_file()
            # the write is done (or deliberately skipped): a different file is from now on a human edit
            self.s["file_pending"] = None
            try:
                self._save_cas()
                self.store.conn.commit()
                self._snap = copy.deepcopy(self.s)
            except StateConflict:  # assertion only: leave file_pending stored, _reconcile repairs it
                self.store.conn.rollback()
                self.s = self._load()

    def _sync_file(self) -> None:
        """Write playbook/items.yaml for the new champion ONLY over our own content: the file must still be the old
        version (or already the new one, or marked invalid by _reconcile). Anything else is the owner's edit made
        meanwhile: it is left alone and the next _reconcile records it as an owner edit."""
        fp = self.s.get("file_pending") or {}
        try:
            on_disk = Playbook.from_file(self.root).sha()
        except PlaybookError:
            on_disk = None
        if on_disk == fp.get("to"):
            return
        if fp.get("force") or on_disk == fp.get("from"):
            self.champion().activate(self.root)

    def _io_done(self) -> None:
        """Called by a step right after its model call, before its first write: whatever ran on the connection while
        it awaited (nothing of the step was pending) is not the step's own writes."""
        if not self.store.conn.in_transaction:
            self._c0 = self.store.conn.total_changes

    def _sp(self, sql: str) -> None:
        """A savepoint statement of the step; failing means the step's transaction is gone."""
        try:
            self.store.conn.execute(sql)
        except sqlite3.OperationalError as e:
            raise StepInterrupted(f"{sql}: {e}") from None

    async def _attempt(self, coro) -> bool:
        """Run one step: its writes and the state commit together or not at all. Before the COMMIT, a step that
        wrote rows must still have its transaction open (nobody committed or rolled back on the connection in
        between; a command's savepoint vanishing is the same signal); otherwise the step is rolled back and reported
        (`improve_step_interrupted`). Events of a step that did not commit are not returned by `on_round`.
        The transaction is opened by the step's first write (never at its start: a read snapshot held across the
        step would make the first write fail as soon as another process committed meanwhile)."""
        conn = self.store.conn
        n0 = len(self._new)
        self._c0 = conn.total_changes
        try:
            await coro
            if conn.total_changes != self._c0 and not conn.in_transaction:
                raise StepInterrupted("the step wrote rows but its transaction was no longer open before the commit")
            self._commit()
            return True
        except StepAborted:
            del self._new[n0:]
            return False
        except StepInterrupted as e:
            log.error("improvement step interrupted: %s", e)
            conn.rollback()
            conn.execute("insert into health (source, kind, detail) values (?,?,?)",
                         ("improve", "improve_step_interrupted",
                          f"the step's transaction was ended under it ({e}); step rolled back"[:500]))
            conn.commit()
            self.s = self._load()
            del self._new[n0:]
            return False
        except BaseException:
            conn.rollback()
            self.s = self._load()
            del self._new[n0:]
            raise

    def _champion_changed(self, old: int | None, new: int) -> None:
        """Record the file write this commit owes (a crash before it is repaired, not mistaken for an edit). When a
        write is already owed (several champion changes in one step, or a crash being recovered), the file on disk
        is still that write's ORIGINAL content: keep its `from` (and `force`), only the target moves."""
        to = version_sha(self.store, new)
        fp = self.s.get("file_pending")
        if fp:
            self.s["file_pending"] = {**fp, "to": to}
        else:
            self.s["file_pending"] = {"from": version_sha(self.store, old) if old else None, "to": to}
        self.s["champion"] = new

    def _event(self, wh: float, what: str, **kw) -> dict:
        e = {"wh": round(wh, 2), "event": what, **kw}
        self.s["events"].append(e)
        self._new.append(e)
        return e

    def _health(self, kind: str, detail: str) -> None:
        """A health event in the CURRENT transaction (committed with the state change it reports)."""
        self.store.conn.execute("insert into health (source, kind, detail) values (?,?,?)",
                                ("improve", kind, detail[:500]))

    def _set_phase(self, phase: str) -> None:
        """Every phase change consumes the health events gathered so far (they belonged to the previous state)."""
        if phase != self.s["phase"]:
            self.s["health_pending"] = {}
        self.s["phase"] = phase

    def _rest(self) -> None:
        self._set_phase("maintenance" if self.s["maintenance"] else "idle")

    # ------------------------------------------------------------ views
    @property
    def phase(self) -> str:
        return self.s["phase"]

    def champion(self) -> Playbook:
        if self.s["champion"] is None:
            return Playbook.load(store=self.store, root=self.root)
        return Playbook.load(self.s["champion"], store=self.store)

    def sentinel_playbook(self) -> Playbook:
        v = self.s.get("sentinel_version") or self.s["champion"]
        return Playbook.load(v, store=self.store) if v else Playbook.load(store=self.store, root=self.root)

    def status(self) -> dict:
        wh = self.clock()
        out = {k: self.s[k] for k in ("phase", "maintenance", "maintenance_reason", "proposal_id", "champion",
                                      "challenger", "door", "consecutive_rejections")}
        out.update(watched_hours=round(wh, 2), cooldown_left_wh=round(max(0.0, self.s["cooldown_until_wh"] - wh), 2),
                   next_review_in_wh=round(max(0.0, self.s["last_review_wh"] + gates.WEEKLY_REVIEW_WATCHED_HOURS - wh), 2),
                   owner_review=[r["id"] for r in self.store.conn.execute(
                       "select id from proposals where status='owner_review' order by id")],
                   events=self.s["events"][-10:])
        if self.phase == "shadow" and self.s["proposal_id"]:
            out["shadow"] = shadow.tally(self.store, self.s["proposal_id"])
        if self.phase == "probation" and self.s["probation"]:
            out["probation_left_wh"] = round(max(0.0, self.s["probation"]["started_wh"]
                                                 + gates.PROBATION_WATCHED_HOURS - wh), 2)
        return out

    # ------------------------------------------------------------ reconciliation
    def _version_status(self, vid: int | None) -> str | None:
        if vid is None:
            return None
        r = self.store.conn.execute("select status from playbook_versions where id=?", (vid,)).fetchone()
        return None if r is None else r["status"]

    def _abort_in_flight(self, wh: float, why: str) -> None:
        pid = self.s.get("proposal_id")
        if pid:
            self._proposal(pid, "aborted", "reconcile", {"why": why})
        for v in {self.s.get("challenger"), self.s["champion"] if self.phase == "probation" else None} - {None}:
            if self._version_status(v) in ("shadow", "promoted") and v != active_version(self.store):
                set_status(self.store, v, "aborted", commit=False)
        self._health("in_flight_aborted", f"proposal {pid}: {why}")
        self.s.update(challenger=None, proposal_id=None, door=None, probation=None, shadow_started_wh=None,
                      promoted_wh=None, delta_sources=[])
        self._rest()
        self._event(wh, "in_flight_aborted", proposal=pid, why=why)

    def _reconcile(self, wh: float) -> None:
        """Make state, versions and the file agree (start of every round and of every owner command)."""
        active = active_version(self.store)
        if active is None:
            active = ensure_baseline(self.store, self.root, commit=False)
        # 1. a change in flight must still be the one that is live (probation) or waiting (shadow)
        if self.phase == "probation" and (self.s["champion"] != active
                                          or self._version_status(self.s["champion"]) != "promoted"):
            self._abort_in_flight(wh, f"probation version v{self.s['champion']} is no longer the active one (v{active})")
        unsaved_promotion = (self.phase == "shadow" and self.s.get("challenger") == active
                             and self._version_status(active) == "promoted")
        if self.phase == "shadow" and not unsaved_promotion and (
                self._version_status(self.s.get("challenger")) != "shadow" or self.s["champion"] != active):
            self._abort_in_flight(wh, f"shadow challenger v{self.s.get('challenger')} is stale (active v{active})")
        if self.s["champion"] != active:
            prev = self.s["champion"]
            self._health("state_mismatch", f"controller state said v{prev}, the active version is v{active}")
            self.s.update(previous=prev, monitor=None)
            if unsaved_promotion:  # (legacy) a promotion whose state was lost: watched, never run unprobated
                cc, sc = monitor.counts(self.store, version=prev, last=gates.CUSUM_BASELINE_ROUNDS)
                self.s.update(probation=probation.Probation.start(active, prev, wh, cc, sc).to_dict(), promoted_wh=wh)
                self._champion_changed(prev, active)
                self._set_phase("probation")
                self._event(wh, "reconciled_probation", version=active, previous=prev)
            else:
                self._champion_changed(prev, active)
                self._event(wh, "reconciled", version=active, previous=prev)
        # 2. the file
        try:
            file_pb = Playbook.from_file(self.root)
        except PlaybookError as e:
            self._health("playbook_file_invalid", str(e))
            self._champion_changed(self.s["champion"], self.s["champion"])
            self.s["file_pending"]["force"] = True
            self._event(wh, "file_restored", version=active)
            return
        have, want = file_pb.sha(), version_sha(self.store, self.s["champion"])
        pending = self.s.get("file_pending")
        if have == want:
            self.s["file_pending"] = None
            return
        if pending and have == pending.get("from"):  # a crash between the commit and the file write
            self._health("file_reconciled", f"playbook file was the previous version; rewrote v{self.s['champion']}")
            self._event(wh, "file_reconciled", now=self.s["champion"])
            return  # file_pending stays: _commit's _sync_file writes it
        self._owner_edit(file_pb, wh)

    def _owner_edit(self, file_pb: Playbook, wh: float) -> None:
        """playbook/items.yaml changed outside the loop: the owner's edit (or hand revert) becomes the new baseline.
        All rows here join the state in the next commit (one transaction)."""
        errs = file_pb.errors()
        if errs:
            self._health("owner_edit_invalid", "; ".join(errs[:5]))
            self._champion_changed(self.s["champion"], self.s["champion"])  # the file is restored after the commit
            self.s["file_pending"]["force"] = True
            self._event(wh, "owner_edit_rejected", errors=errs[:5])
            return
        known = self.store.conn.execute("select id from playbook_versions where sha=? order by id desc limit 1",
                                        (file_pb.sha(),)).fetchone()
        note = (f"owner revert to v{known['id']} (playbook/items.yaml set back by hand)" if known else
                "owner edit (playbook/items.yaml changed outside the loop)")
        pid = self.s.get("proposal_id")
        in_flight = bool(pid) and self.phase in ("shadow", "probation")
        old = self.s["champion"]
        if in_flight:
            self._proposal(pid, "aborted", "owner_edit", {"note": note})
            for v in {self.s.get("challenger"), old if self.phase == "probation" else None} - {None}:
                set_status(self.store, v, "aborted", commit=False)
        vid = file_pb.save_version(self.store, status="baseline", parent=old, note=note, commit=False)
        self._health("owner_edit", f"{note}: stored as baseline v{vid}" + (f"; proposal {pid} aborted" if in_flight else ""))
        self.s.update(challenger=None, proposal_id=None, door=None, probation=None, shadow_started_wh=None,
                      monitor=None, promoted_wh=None, delta_sources=[], previous=old)
        self.s["champion"] = vid
        self.s["file_pending"] = None  # the file IS this version
        self._rest()
        self._event(wh, "owner_edit", version=vid, note=note)

    # ------------------------------------------------------------ owner commands
    async def apply_commands(self, wh: float) -> int:
        """Apply the queued owner commands inside the current step (`_attempt`): each command's effects and its
        applied mark go into the step's one transaction (a failing command is undone alone, via a savepoint, and
        marked failed). A command that cannot run now (an apply without its live check) stays queued and is skipped;
        every other command still applies, in queue order (safety commands never wait behind an apply). Returns the
        number applied."""
        self._deferred: set[str] = set()
        rows = self.store.conn.execute("select * from improve_commands where applied_at is null order by id").fetchall()
        done = 0
        for r in rows:
            args = json.loads(r["args"] or "{}")
            before, n_ev = copy.deepcopy(self.s), len(self._new)
            if not self.store.conn.in_transaction:  # an outermost savepoint's RELEASE would COMMIT the step early
                self.store.conn.execute("begin")
            self._sp("savepoint improve_cmd")
            try:
                result = await self._command(r["cmd"], args, wh, cid=r["id"])
            except StepInterrupted:
                raise
            except _Deferred as e:
                self._sp("rollback to savepoint improve_cmd")
                self._sp("release savepoint improve_cmd")
                self.s = before
                del self._new[n_ev:]
                self._deferred.add(str(e))
                continue
            except Exception as e:  # noqa: BLE001 - a bad command is reported, never retried forever
                self._sp("rollback to savepoint improve_cmd")
                self._sp("release savepoint improve_cmd")
                self.s = before
                del self._new[n_ev:]
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
            else:
                self._sp("release savepoint improve_cmd")
            self.store.conn.execute("update improve_commands set applied_at=?, result=? where id=?",
                                    (_now(), json.dumps(result, default=str), r["id"]))
            done += 1
        return done

    async def _prepare_live(self) -> None:
        """The LIVE must-pass of every queued owner apply, run BEFORE the step opens its transaction: no model call
        is ever awaited inside a step. Kept per command with the sha of the challenger it checked; `_apply_owner`
        uses it only for that exact challenger (otherwise the apply waits for the next step)."""
        self._live = {}
        if self.llm is None:
            return
        rows = self.store.conn.execute("select id, args from improve_commands where applied_at is null and cmd='apply' "
                                       "order by id").fetchall()
        for r in rows:
            try:
                built = self._owner_challenger(int(json.loads(r["args"] or "{}")["proposal_id"]))
            except Exception:  # noqa: BLE001 - a bad command is reported by the step itself
                continue
            if built is None:
                continue
            delta, challenger = built
            try:
                live = await gates.must_pass_live(challenger, self.llm, delta.item.id, cfg=self.cfg)
            except Exception as e:  # noqa: BLE001 - an unreachable model never approves anything
                live = {"ok": False, "failed": [], "why": [f"live must-pass raised {type(e).__name__}: {e}"[:300]],
                        "calls": 0, "unavailable": 1, "reached_changed_prompt": 0}
            self._live[r["id"]] = (challenger.sha(), live)

    def _owner_challenger(self, pid: int) -> tuple[Delta, Playbook] | None:
        """The challenger an owner apply would promote: the proposal's delta re-made on the current champion
        (read only). None when the proposal does not wait for owner review; PlaybookError when it no longer applies."""
        r = self.store.conn.execute("select status, payload from proposals where id=?", (pid,)).fetchone()
        if r is None or r["status"] != "owner_review":
            return None
        delta = Delta.from_dict(json.loads(r["payload"])["delta"])
        champ = self.champion()
        return delta, champ.apply(champ.make_delta(delta.op, delta.item, hypothesis=delta.hypothesis,
                                                   evidence=delta.evidence, predicted_effect=delta.predicted_effect))

    async def _command(self, cmd: str, args: dict, wh: float, cid: int | None = None) -> dict:
        if cmd == "rollback":
            return self._force_rollback(int(args["version"]), wh)
        if cmd == "maintenance":
            self._set_maintenance(bool(args["on"]), wh, args.get("reason", "owner"))
            return {"ok": True, "phase": self.phase, "maintenance": self.s["maintenance"]}
        if cmd == "label":
            w = shadow.label(self.store, int(args["id"]), str(args["truth"]), source="owner", commit=False)
            return {"ok": True, "winner": w}
        if cmd == "apply":
            return await self._apply_owner(int(args["proposal_id"]), wh, cid)
        raise ValueError(f"unknown command {cmd!r}")

    async def _commands_step(self, wh: float) -> None:
        self._init(wh)
        self._reconcile(wh)
        self._applied = await self.apply_commands(wh)

    async def process_commands(self) -> int:
        """The command step alone, in one transaction. Call it only while holding the agent lock (the CLI does so
        through `improve.apply_queued_now`). An apply whose live check was made against a champion an earlier
        command changed gets one more step (its live check re-run first). Returns the number of commands applied."""
        self._new = []
        total = 0
        for _ in range(3):
            self.s = self._load()
            await self._prepare_live()
            wh = self.clock()
            self._applied = 0
            if not await self._attempt(self._commands_step(wh)):
                return total
            total += self._applied
            if STALE_LIVE not in self._deferred:
                break
        return total

    # ------------------------------------------------------------ the round hook
    async def on_round(self, round_stats: dict | None, *, champion_n: int, sentinel_n: int,
                       candidates: list[int] | None = None, round_id: int | None = None,
                       deals_n: int | None = None) -> list[dict]:
        """Called once after every round, by the agent-lock holder. Four steps, each ONE transaction:
        1. commands: reconcile, then the queued owner commands (durable before the round's own work can fail);
        2. observe: the round's counts, failure report, shadow scoring or probation watch (and their decisions);
        3. decide (idle / maintenance): triggers and at most one proposal; its LLM call runs before any write;
        4. commands queued while the round ran."""
        self.s = self._load()
        wh = self.clock()
        self._new = []
        await self._prepare_live()  # an owner apply's live model call, before any step opens its transaction
        if not await self._attempt(self._commands_step(wh)):
            return list(self._new)
        rid = round_id if round_id is not None else (round_stats or {}).get("round_id")
        ctx = {"alarm": None}
        if not await self._attempt(self._observe_step(round_stats, rid, wh, champion_n, sentinel_n,
                                                      candidates or [], deals_n, ctx)):
            return list(self._new)
        if self.phase in ("idle", "maintenance"):
            if not await self._attempt(self._decide_step(wh, ctx["alarm"])):
                return list(self._new)
        await self._prepare_live()
        await self._attempt(self.apply_commands(wh))
        return list(self._new)

    async def _observe_step(self, round_stats, rid, wh, champion_n, sentinel_n, candidates, deals_n, ctx) -> None:
        failures = reflect.collect_failures(self.store, rid, since_mark_id=self.s["last_mark_id"]) if rid else []
        rep = (await reflect.round_report(round_stats or {}, failures, llm=self.llm, playbook=self.champion())
               if failures else None)  # the model call happens before this step writes anything
        self._io_done()
        replay.record_prompt_epoch(self.store, prompt_sha(self.champion()), datetime.now(timezone.utc), commit=False)
        health = self._take_health()
        if rid is not None:
            monitor.record(self.store, rid, wh, self.s["champion"], champion_n, sentinel_n, len(candidates),
                           deals_n=deals_n, health=health, commit=False)
        if rep is not None:
            reflect.store_report(self.store, rep, wh, commit=False)
        m = self.store.conn.execute("select coalesce(max(id),0) from marks").fetchone()[0]
        self.s["last_mark_id"] = max(self.s["last_mark_id"], int(m))
        if self.phase == "shadow":
            await self._shadow_round(candidates, rid, wh)
        elif self.phase == "probation":
            await self._probation_round(champion_n, sentinel_n, wh)
        else:
            for k, v in health.items():
                self.s["health_pending"][k] = self.s["health_pending"].get(k, 0) + v
            ctx["alarm"] = self._watch(champion_n, sentinel_n, wh)
            self._check_mde(wh)

    async def _decide_step(self, wh: float, alarm: str | None) -> None:
        trig = self._trigger(wh, alarm)
        if trig is not None:
            await self._reflect_and_propose(trig[0], trig[1], wh)

    def _take_health(self) -> dict[str, int]:
        rows = self.store.conn.execute("select id, source, kind from health where id > ? and source != 'improve' "
                                       "order by id", (self.s["last_health_id"],)).fetchall()
        out: dict[str, int] = {}
        for r in rows:
            k = f"{r['source']}|{r['kind']}"
            out[k] = out.get(k, 0) + 1
        self.s["last_health_id"] = max([self.s["last_health_id"], *(r["id"] for r in rows)])
        return out

    # ------------------------------------------------------------ monitoring outside probation
    def _watch(self, champion_n: int, sentinel_n: int, wh: float) -> str | None:
        m = self.s.get("monitor")
        if not m or m.get("version") != self.s["champion"]:
            m = {"version": self.s["champion"], "c": None, "s": None}
        c, s = PoissonCusum.from_dict(m["c"]), PoissonCusum.from_dict(m["s"])
        alarm = None
        if c is None:
            cc, sc = monitor.counts(self.store, version=self.s["champion"], last=gates.CUSUM_BASELINE_ROUNDS)
            c, s = make(cc), make(sc)
        else:
            c.update(champion_n)
            if s is not None:
                s.update(sentinel_n)
            if c.alarm:  # research §3.8 table, no recent change: the sentinel tells the market from a local break
                alarm = "market_shift" if (s is not None and s.alarm) else "cusum"
                self._event(wh, "cusum_alarm", attribution=alarm)
                c = s = None  # re-baseline on the newest rounds
        m.update(c=c.to_dict() if c else None, s=s.to_dict() if s else None)
        self.s["monitor"] = m
        return alarm

    def _check_mde(self, wh: float) -> None:
        if self.s["maintenance"] or wh < gates.MDE_WINDOW_WATCHED_HOURS:
            return
        r = self.store.conn.execute("select coalesce(sum(deals_n),0) n from improve_rounds where watched_h >= ?",
                                    (wh - gates.MDE_WINDOW_WATCHED_HOURS,)).fetchone()
        mde = gates.mde_ratio(float(r["n"]))
        if mde > gates.PLAUSIBLE_GAIN:
            self._enter_maintenance(wh, f"2 weeks of watched data ({r['n']} deals, tier 1-3) can only detect a "
                                        f"x{mde:g} change; one item plausibly gains at most x{gates.PLAUSIBLE_GAIN:g}")

    # ------------------------------------------------------------ triggers
    def _trigger(self, wh: float, alarm: str | None) -> tuple[str, list[dict]] | None:
        clusters = reflect.open_clusters(self.store)
        evidence_all = [f for fs in clusters.values() for f in fs]
        if alarm is not None:
            if alarm == "cusum":  # research §3.9: a drift alarm resets the backoff
                self.s["cooldown_until_wh"] = min(self.s["cooldown_until_wh"], wh)
            return f"alarm:{alarm}", evidence_all
        hot = sorted((k for k, v in self.s["health_pending"].items() if v >= gates.HEALTH_ALARM_EVENTS))
        if hot:
            return f"health:{hot[0].replace('|', ':')}", evidence_all
        if wh - self.s["last_review_wh"] >= gates.WEEKLY_REVIEW_WATCHED_HOURS:
            return "weekly", evidence_all
        if self.s["maintenance"]:
            return None  # only alarms and the weekly review reopen a frozen playbook
        for cause, fs in sorted(clusters.items(), key=lambda kv: -len(kv[1])):
            if len(fs) >= gates.SAME_CAUSE_TRIGGER and cause not in ("other",):
                return f"failures:{cause}", fs
        return None

    def _cooling(self, wh: float) -> bool:
        return wh < self.s["cooldown_until_wh"]

    async def _reflect_and_propose(self, trigger: str, evidence: list[dict], wh: float) -> None:
        if self._cooling(wh):
            last = self.s["events"][-1] if self.s["events"] else {}
            if last.get("event") != "trigger_in_cooldown" or last.get("trigger") != trigger:
                self._event(wh, "trigger_in_cooldown", trigger=trigger,
                            left_wh=round(self.s["cooldown_until_wh"] - wh, 2))
            return  # the trigger stays pending (a due weekly review is not lost)
        if trigger == "weekly":
            self.s["last_review_wh"] = wh
        if trigger.startswith("health:"):
            self.s["health_pending"] = {}
        cause = trigger.split(":", 1)[1] if trigger.startswith("failures:") else None
        d = await reflect.propose(trigger, evidence, self.champion(), llm=self.llm,
                                  rejected=reflect.rejected_ideas(self.store))
        self._io_done()  # the advisor call ran before this step wrote anything
        if d is None:
            reflect.consume(self.store, cause, 0, commit=False)  # stamped: the cluster does not call the advisor again
            self.s["cooldown_until_wh"] = max(self.s["cooldown_until_wh"], wh + BASE_COOLDOWN)
            self._event(wh, "no_proposal", trigger=trigger, cooldown_wh=BASE_COOLDOWN)
            return
        await self._run(d, trigger, wh, cause=cause)

    # ------------------------------------------------------------ a proposal's life
    async def submit(self, delta: Delta, trigger: str = "owner") -> dict:
        """Put one delta through the gates (owner or test injection; the reflector uses the same path).
        Raises InFlight while another change is in shadow or probation."""
        self.s = self._load()
        self._new = []
        wh = self.clock()
        await self._prepare_live()
        if await self._attempt(self._commands_step(wh)):
            await self._attempt(self._run(delta, trigger, wh))
        return self.status()

    async def _run(self, delta: Delta, trigger: str, wh: float, cause: str | None = None) -> None:
        if self.phase in ("shadow", "probation"):
            raise InFlight(f"proposal {self.s['proposal_id']} is in {self.phase}: one change in flight")
        champ = self.champion()
        if delta.inverse is None:
            try:
                delta.inverse = champ._inverse(delta)
            except Exception:  # noqa: BLE001 - the gates reject it at the schema stage
                pass
        payload = {"trigger": trigger, "delta": delta.to_dict(), "door": delta.door(), "stage": "proposed"}
        cur = self.store.conn.execute("insert into proposals (kind, payload, status, stage) values (?,?,?,?)",
                                      (delta.item.kind, json.dumps(payload, default=str), "proposed", "proposed"))
        pid = cur.lastrowid
        reflect.consume(self.store, cause, pid, commit=False)
        self.s["proposal_id"] = pid
        self.s["delta_sources"] = _sources_of(delta.item)
        try:
            challenger = champ.apply(delta)
        except Exception as e:  # noqa: BLE001 - PlaybookError: schema, caps, sentinel
            return self._reject(wh, "schema", {"errors": [str(e)]})
        res = await gates.evaluate(champ, challenger, self.store, cfg=self.cfg)
        self._event(wh, "gates", proposal=pid, stage=res.stage, passed=res.passed)
        if not res.passed:
            return self._reject(wh, res.stage, res.detail)
        if res.stage == "owner_review":  # ruling 5: the loop never promotes a prompt change
            vid = challenger.save_version(self.store, status="candidate", parent=self.s["champion"], delta=delta,
                                          proposal_id=pid, commit=False)
            self._proposal(pid, "owner_review", "owner_review", res.detail, version_id=vid)
            self._event(wh, "owner_review", proposal=pid, note="apply with the 'apply' command after reading it")
            self.s["proposal_id"] = None
            return
        vid = challenger.save_version(self.store, status="shadow", parent=self.s["champion"], delta=delta,
                                      proposal_id=pid, commit=False)
        self.s.update(challenger=vid, door=res.detail.get("door") or delta.door())
        self._proposal(pid, "shadow", "offline", res.detail, version_id=vid)
        if self.s["door"] == "arm":  # a query nobody ran cannot be shadowed: the bandit's trial + the CUSUM judge it
            self._promote(wh)
        else:
            self._set_phase("shadow")
            self.s["shadow_started_wh"] = wh
            self._event(wh, "shadow_started", proposal=pid, version=vid)

    async def _apply_owner(self, pid: int, wh: float, cid: int | None = None) -> dict:
        """Owner command: promote a prompt proposal waiting for review, after a must-pass run with the LIVE model.
        That run happened before the step (`_prepare_live`); without it (no model, or it checked another champion)
        the command waits in the queue (`_Deferred`), it is never consumed."""
        if self.phase in ("shadow", "probation"):
            return {"ok": False, "error": f"one change in flight: proposal {self.s['proposal_id']} is in {self.phase}"}
        r = self.store.conn.execute("select status from proposals where id=?", (pid,)).fetchone()
        if r is None or r["status"] != "owner_review":
            return {"ok": False, "error": f"proposal {pid} is not waiting for owner review"}
        if self.llm is None:
            raise _Deferred(NO_LIVE_MODEL)
        try:
            delta, challenger = self._owner_challenger(pid)
        except PlaybookError as e:
            self._proposal(pid, "rejected", "owner_apply", {"errors": [str(e)]})
            return {"ok": False, "error": str(e)}
        pre = self._live.get(cid) if cid is not None else None
        if pre is None or pre[0] != challenger.sha():
            raise _Deferred(STALE_LIVE)
        live = pre[1]
        detail = {"live_must_pass": live, "applied_at_wh": round(wh, 2)}
        if not live["ok"]:
            self._proposal(pid, "rejected", "owner_live_must_pass", detail)
            self._event(wh, "owner_apply_failed", proposal=pid, why=live["why"], failed=live["failed"])
            return {"ok": False, "error": "; ".join(live["why"]), "failed": live["failed"],
                    "unavailable": live["unavailable"], "reached_changed_prompt": live["reached_changed_prompt"]}
        vid = challenger.save_version(self.store, status="shadow", parent=self.s["champion"], delta=delta,
                                      proposal_id=pid, commit=False)
        self.s.update(proposal_id=pid, challenger=vid, door="llm", delta_sources=[])
        self._proposal(pid, "probation", "owner_applied", detail, version_id=vid)
        self._promote(wh)
        return {"ok": True, "version": vid}

    async def _shadow_round(self, candidates: list[int], rid: int | None, wh: float) -> None:
        pid = self.s["proposal_id"]
        champ, chall = self.champion(), Playbook.load(self.s["challenger"], store=self.store)
        await shadow.score_round(champ, chall, candidates, store=self.store, proposal_id=pid, round_id=rid,
                                 cfg=self.cfg, commit=False)
        shadow.auto_label(self.store, pid, commit=False)
        verdict, d = shadow.decide(self.store, pid, wh - float(self.s["shadow_started_wh"] or wh))
        if verdict == "promote":
            ok, bad = await gates.must_pass_ok(chall, cfg=self.cfg)
            if not ok:
                return self._reject(wh, "shadow_must_pass", d | {"failed": bad})
            self._proposal(pid, "shadow", "shadow_passed", d)
            self._promote(wh)
        elif verdict == "reject":
            self._reject(wh, "shadow", d)

    def _promote(self, wh: float) -> None:
        """Version status and state in ONE transaction, then the file (a crash in between: `_reconcile`)."""
        prev, vid, pid = self.s["champion"], self.s["challenger"], self.s["proposal_id"]
        cc, sc = monitor.counts(self.store, version=prev, last=gates.CUSUM_BASELINE_ROUNDS)
        p = probation.Probation.start(vid, prev, wh, cc, sc)
        self.s.update(previous=prev, probation=p.to_dict(), monitor=None, promoted_wh=wh)
        self._champion_changed(prev, vid)
        self._set_phase("probation")
        self._event(wh, "promoted", proposal=pid, version=vid, previous=prev)
        set_status(self.store, vid, "promoted", commit=False)
        self._proposal(pid, "probation", "promoted", {})
        # committed with the state by the caller's _commit (one transaction), then the file is written

    async def _probation_round(self, champion_n: int, sentinel_n: int, wh: float) -> None:
        p = probation.Probation(self.s["probation"])
        verdict = p.observe(champion_n, sentinel_n, wh)
        if verdict != "rollback":
            why = self._health_rise(wh)
            if why:
                p.state.setdefault("events", []).append({"wh": round(wh, 2), "event": "health_rise", "keys": why})
                verdict = "rollback"
        if verdict != "rollback" and p.must_pass_due(wh):
            p.state["last_must_pass_wh"] = wh
            ok, bad = await gates.must_pass_ok(self.champion(), cfg=self.cfg)
            if not ok:
                p.state.setdefault("events", []).append({"wh": round(wh, 2), "event": "must_pass_failed", "cases": bad})
                verdict = "rollback"
        self.s["probation"] = p.to_dict()
        if verdict == "market_shift":
            self._event(wh, "market_shift", proposal=self.s["proposal_id"], note="no rollback: the sentinel fell too")
        elif verdict == "rollback":
            self._rollback(wh, p.state.get("events", [])[-1:] or [])
        elif verdict == "passed":
            self._accept(wh)

    def _health_rise(self, wh: float) -> list[str]:
        """Source/kind keys whose health events since the promotion are >= HEALTH_ALARM_EVENTS and more than twice
        the equal watched window before it. A rise on a source the delta names (cadence or query target) is always
        the change's; the control is every OTHER sentinel source: if one of them rises too, the sites changed."""
        t0 = self.s.get("promoted_wh")
        if t0 is None:
            return []
        span = wh - float(t0)
        after = monitor.health_between(self.store, float(t0), wh)
        before = monitor.health_between(self.store, float(t0) - span, float(t0))
        rising = [k for k, n in after.items() if n >= gates.HEALTH_ALARM_EVENTS and n > 2 * before.get(k, 0)]
        named = set(self.s.get("delta_sources") or [])
        control = monitor.sentinel_sources(self.sentinel_playbook()) - named

        def on(key: str, sources: set[str]) -> bool:
            src = key.split("|", 1)[0]
            return any(s == src or s in src for s in sources)
        if any(on(k, control) for k in rising):
            return []  # the control group is unhealthy too: the sites changed, not the playbook
        return sorted(rising)

    def _rollback(self, wh: float, why: list) -> None:
        """Version status and state in ONE transaction, then the file."""
        failed, pid = self.s["champion"], self.s["proposal_id"]
        set_status(self.store, failed, "failed_in_prod", commit=False)
        restored = active_version(self.store)
        if restored is None or restored == failed:
            raise PlaybookError("no promoted or baseline version to roll back to")
        self.s["monitor"] = None
        self._champion_changed(failed, restored)
        self._event(wh, "rolled_back", proposal=pid, failed=failed, to=restored, why=why)
        self._reject(wh, "probation", {"why": why}, status="failed_in_prod", version_status=None)

    def _accept(self, wh: float) -> None:
        pid = self.s["proposal_id"]
        self._proposal(pid, "accepted", "probation_passed", {})
        self._event(wh, "accepted", proposal=pid, version=self.s["champion"])
        self.s.update(consecutive_rejections=0, cooldown_until_wh=wh, proposal_id=None, challenger=None, door=None,
                      probation=None, shadow_started_wh=None, promoted_wh=None, delta_sources=[])
        self._rest()

    def _reject(self, wh: float, stage: str, detail: dict, *, status: str = "rejected",
                version_status: str | None = "rejected") -> None:
        pid = self.s["proposal_id"]
        if version_status and self.s.get("challenger"):
            set_status(self.store, self.s["challenger"], version_status, commit=False)
        self._proposal(pid, status, stage, detail)
        n = self.s["consecutive_rejections"] + 1
        cool = gates.cooldown_hours(n)
        self.s.update(consecutive_rejections=n, cooldown_until_wh=wh + cool, proposal_id=None, challenger=None,
                      door=None, probation=None, shadow_started_wh=None, promoted_wh=None, delta_sources=[])
        self._event(wh, "rejected", proposal=pid, stage=stage, consecutive=n, cooldown_wh=cool)
        if n >= gates.MAINTENANCE_AFTER_REJECTIONS and not self.s["maintenance"]:
            self._enter_maintenance(wh, f"{n} consecutive rejections")
        self._rest()

    def _proposal(self, pid: int | None, status: str, stage: str, detail: dict, version_id: int | None = None) -> None:
        """Update a proposal in the current transaction. `detail` is MERGED into what is stored (a later stage never
        erases an earlier stage's record, e.g. the live must-pass result of an owner apply)."""
        if pid is None:
            return
        r = self.store.conn.execute("select payload, detail from proposals where id=?", (pid,)).fetchone()
        payload = json.loads(r["payload"]) if r and r["payload"] else {}
        try:
            old = json.loads(r["detail"]) if r and r["detail"] else {}
        except ValueError:
            old = {}
        payload["stage"] = stage
        merged = {**(old if isinstance(old, dict) else {}), **(detail or {})}
        self.store.conn.execute(
            "update proposals set status=?, stage=?, payload=?, detail=?, decided_at=?, version_id=coalesce(?, version_id) "
            "where id=?", (status, stage, json.dumps(payload, default=str), json.dumps(merged, default=str)[:20000],
                           _now(), version_id, pid))

    # ------------------------------------------------------------ maintenance and owner actions
    def _enter_maintenance(self, wh: float, reason: str) -> None:
        self.s.update(maintenance=True, maintenance_reason=reason)
        if self.phase == "idle":
            self._set_phase("maintenance")
        self._event(wh, "maintenance", reason=reason)

    def _set_maintenance(self, on: bool, wh: float, reason: str = "owner") -> None:
        if on:
            self._enter_maintenance(wh, reason)
        else:
            self.s.update(maintenance=False, maintenance_reason=None, consecutive_rejections=0)
            if self.phase == "maintenance":
                self._set_phase("idle")
            self._event(wh, "maintenance_off", reason=reason)

    def _force_rollback(self, version: int, wh: float) -> dict:
        """Make `version` active again (stored as a new promoted copy) and abort any change in flight."""
        target = Playbook.load(version, store=self.store)
        pid = self.s.get("proposal_id")
        old = self.s["champion"]
        if pid:
            self._proposal(pid, "aborted", "owner_rollback", {"to": version})
            if self.s.get("challenger") and self.phase == "shadow":
                set_status(self.store, self.s["challenger"], "aborted", commit=False)
            if self.phase == "probation":
                set_status(self.store, old, "aborted", commit=False)
        vid = target.save_version(self.store, status="promoted", parent=old, note=f"owner rollback to v{version}",
                                  commit=False)
        self.s.update(challenger=None, proposal_id=None, door=None, probation=None, shadow_started_wh=None,
                      monitor=None, promoted_wh=None, delta_sources=[])
        self._champion_changed(old, vid)
        self._rest()
        self._event(wh, "owner_rollback", to=version, as_version=vid)
        return {"ok": True, "version": vid, "to": version}  # committed with the command mark by the caller
