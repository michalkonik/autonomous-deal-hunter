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
  at its start and again right before saving. `_save` is a compare-and-swap on `improve_state.rev`; on a conflict
  the round reloads, re-applies the queue and saves once more.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .. import config as dhconfig
from . import gates, monitor, probation, reflect, replay, shadow
from .cusum import PoissonCusum, make
from .playbook import (ROOT, Delta, Playbook, PlaybookError, active_version, ensure_baseline, prompt_sha, set_status,
                       version_sha)

STATE_SQL = "create table if not exists improve_state (key text primary key, value text not null)"
COMMANDS_SQL = """create table if not exists improve_commands (
  id integer primary key, cmd text not null, args text not null, created_at text not null, applied_at text,
  result text)"""
COMMANDS = ("rollback", "maintenance", "label", "apply")
PHASES = ("idle", "shadow", "probation", "maintenance")
PROPOSAL_COLUMNS = ("version_id integer", "stage text", "decided_at text", "detail text")
BASE_COOLDOWN = gates.COOLDOWN_WATCHED_HOURS[0]


class InFlight(RuntimeError):
    """A change is already in shadow or probation (one change in flight)."""


class StateConflict(RuntimeError):
    """Another writer saved the controller state since this one loaded it."""


def _default() -> dict:
    return {"phase": "idle", "maintenance": False, "maintenance_reason": None, "proposal_id": None,
            "challenger": None, "champion": None, "previous": None, "door": None, "shadow_started_wh": None,
            "probation": None, "promoted_wh": None, "consecutive_rejections": 0, "cooldown_until_wh": 0.0,
            "last_review_wh": 0.0, "last_health_id": 0, "health_pending": {}, "last_mark_id": 0, "monitor": None,
            "events": []}


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
        store.ensure_table(replay.EPOCHS_SQL)
        have = {r["name"] for r in store.conn.execute("pragma table_info(proposals)")}
        for col in PROPOSAL_COLUMNS:
            if col.split()[0] not in have:
                store.ensure_table(f"alter table proposals add column {col}")
        self.rev = 0
        self._new: list[dict] = []
        self.s = self._load()
        if self.s["champion"] is None:
            self.s["champion"] = ensure_baseline(store, self.root)
            self.s["sentinel_version"] = sentinel_version or self.s["champion"]
            self.s["last_review_wh"] = self.clock()
            self.s["last_health_id"] = self._max_health_id()
            self._save()
        self._reconcile(self.clock())
        self._save_retry()

    # ------------------------------------------------------------ persistence
    def _load(self) -> dict:
        r = self.store.conn.execute("select value, rev from improve_state where key='controller'").fetchone()
        self.rev = int(r["rev"]) if r else 0
        return {**_default(), **(json.loads(r["value"]) if r else {})}

    def _save(self, commit: bool = True) -> None:
        """Compare-and-swap on `rev`. Raises StateConflict (and rolls back the open transaction) on a conflict."""
        self.s["events"] = self.s["events"][-50:]
        value = json.dumps(self.s, default=str)
        cur = self.store.conn.execute("update improve_state set value=?, rev=rev+1 where key='controller' and rev=?",
                                      (value, self.rev))
        if cur.rowcount == 0:
            exists = self.store.conn.execute("select 1 from improve_state where key='controller'").fetchone()
            if exists is not None:
                self.store.conn.rollback()
                raise StateConflict("the controller state changed since it was loaded")
            self.store.conn.execute("insert into improve_state (key, value, rev) values ('controller', ?, 1)", (value,))
            self.rev = 1
        else:
            self.rev += 1
        if commit:
            self.store.conn.commit()

    def _save_retry(self) -> None:
        try:
            self._save()
        except StateConflict:
            self.s = self._load()
            self._event(self.clock(), "state_conflict", note="another writer saved first; reloaded")
            self._save()

    def _event(self, wh: float, what: str, **kw) -> dict:
        e = {"wh": round(wh, 2), "event": what, **kw}
        self.s["events"].append(e)
        self._new.append(e)
        return e

    def _health(self, kind: str, detail: str) -> None:
        self.store.add_health("improve", kind, detail[:500])

    def _max_health_id(self) -> int:
        return int(self.store.conn.execute("select coalesce(max(id),0) from health").fetchone()[0])

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
        return Playbook.load(self.s["champion"], store=self.store)

    def sentinel_playbook(self) -> Playbook:
        return Playbook.load(self.s.get("sentinel_version") or self.s["champion"], store=self.store)

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
    def _reconcile(self, wh: float) -> None:
        active = active_version(self.store)
        if active is None:
            active = ensure_baseline(self.store, self.root)
        if self.s["champion"] != active:
            prev = self.s["champion"]
            self._health("state_mismatch", f"controller state said v{prev}, the active version is v{active}")
            status = self.store.conn.execute("select status, proposal_id from playbook_versions where id=?",
                                             (active,)).fetchone()
            self.s.update(champion=active, previous=prev, monitor=None)
            if status is not None and status["status"] == "promoted" and self.phase != "probation":
                # a promotion whose state was never saved: the running version is watched, never unprobated
                cc, sc = monitor.counts(self.store, version=prev, last=gates.CUSUM_BASELINE_ROUNDS)
                self.s.update(proposal_id=status["proposal_id"] or self.s.get("proposal_id"), challenger=active,
                              probation=probation.Probation.start(active, prev, wh, cc, sc).to_dict(), promoted_wh=wh)
                self._set_phase("probation")
                self._event(wh, "reconciled_probation", version=active, previous=prev)
            else:
                self._event(wh, "reconciled", version=active, previous=prev)
        if self.phase == "shadow" and self.s.get("challenger"):
            st = self.store.conn.execute("select status from playbook_versions where id=?",
                                         (self.s["challenger"],)).fetchone()
            if st is None or st["status"] != "shadow":  # decided, but the state save was lost
                self.s.update(challenger=None, proposal_id=None, door=None, shadow_started_wh=None)
                self._rest()
                self._event(wh, "reconciled_shadow_end")
        try:
            file_pb = Playbook.from_file(self.root)
        except PlaybookError as e:
            self._health("playbook_file_invalid", str(e))
            self.champion().activate(self.root)
            self._event(wh, "file_restored", version=active)
            return
        want = version_sha(self.store, self.s["champion"])
        have = file_pb.sha()
        if have == want:
            return
        known = self.store.conn.execute("select id from playbook_versions where sha=? order by id desc limit 1",
                                        (have,)).fetchone()
        if known is not None:  # an older version on disk: a crash between the commit and the file write
            self.champion().activate(self.root)
            self._health("file_reconciled", f"playbook file was v{known['id']}, rewrote v{self.s['champion']}")
            self._event(wh, "file_reconciled", was=known["id"], now=self.s["champion"])
            return
        self._owner_edit(file_pb, wh)

    def _owner_edit(self, file_pb: Playbook, wh: float) -> None:
        """playbook/items.yaml changed outside the loop: the owner's edit becomes the new baseline."""
        errs = file_pb.errors()
        if errs:
            self._health("owner_edit_invalid", "; ".join(errs[:5]))
            self.champion().activate(self.root)
            self._event(wh, "owner_edit_rejected", errors=errs[:5])
            return
        pid = self.s.get("proposal_id")
        if pid and self.phase in ("shadow", "probation"):
            self._proposal(pid, "aborted", "owner_edit", {"note": "the owner edited playbook/items.yaml"})
            if self.s.get("challenger") and self.phase == "shadow":
                set_status(self.store, self.s["challenger"], "aborted")
        vid = file_pb.save_version(self.store, status="baseline", parent=self.s["champion"],
                                   note="owner edit (playbook/items.yaml changed outside the loop)")
        self._health("owner_edit", f"playbook file edited by hand: stored as baseline v{vid}"
                     + (f"; proposal {pid} aborted" if pid and self.phase in ("shadow", "probation") else ""))
        self.s.update(champion=vid, challenger=None, proposal_id=None, door=None, probation=None,
                      shadow_started_wh=None, monitor=None)
        self._rest()
        self._event(wh, "owner_edit", version=vid)

    # ------------------------------------------------------------ owner commands
    async def apply_commands(self, wh: float) -> int:
        rows = self.store.conn.execute("select * from improve_commands where applied_at is null order by id").fetchall()
        for r in rows:
            args = json.loads(r["args"] or "{}")
            try:
                result = await self._command(r["cmd"], args, wh)
            except Exception as e:  # noqa: BLE001 - a bad command is reported, never retried forever
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
            self.store.conn.execute("update improve_commands set applied_at=?, result=? where id=?",
                                    (_now(), json.dumps(result, default=str), r["id"]))
        if rows:
            self.store.conn.commit()
        return len(rows)

    async def _command(self, cmd: str, args: dict, wh: float) -> dict:
        if cmd == "rollback":
            return self._force_rollback(int(args["version"]), wh)
        if cmd == "maintenance":
            self._set_maintenance(bool(args["on"]), wh, args.get("reason", "owner"))
            return {"ok": True, "phase": self.phase, "maintenance": self.s["maintenance"]}
        if cmd == "label":
            w = shadow.label(self.store, int(args["id"]), str(args["truth"]), source="owner")
            return {"ok": True, "winner": w}
        if cmd == "apply":
            return await self._apply_owner(int(args["proposal_id"]), wh)
        raise ValueError(f"unknown command {cmd!r}")

    async def process_commands(self) -> int:
        """Apply the queued owner commands now (the CLI does this when the agent is not running)."""
        self.s = self._load()
        wh = self.clock()
        n = await self.apply_commands(wh)
        self._reconcile(wh)
        self._save_retry()
        return n

    # ------------------------------------------------------------ the round hook
    async def on_round(self, round_stats: dict | None, *, champion_n: int, sentinel_n: int,
                       candidates: list[int] | None = None, round_id: int | None = None,
                       deals_n: int | None = None) -> list[dict]:
        """Called once after every round. Returns the events it produced."""
        self.s = self._load()
        wh = self.clock()
        self._new = []
        await self.apply_commands(wh)
        self._reconcile(wh)
        replay.record_prompt_epoch(self.store, prompt_sha(self.champion()), datetime.now(timezone.utc))
        health = self._take_health()
        rid = round_id if round_id is not None else (round_stats or {}).get("round_id")
        if rid is not None:
            monitor.record(self.store, rid, wh, self.s["champion"], champion_n, sentinel_n, len(candidates or []),
                           deals_n=deals_n, health=health)
        failures = reflect.collect_failures(self.store, rid, since_mark_id=self.s["last_mark_id"]) if rid else []
        if failures:
            rep = await reflect.round_report(round_stats or {}, failures, llm=self.llm, playbook=self.champion())
            reflect.store_report(self.store, rep, wh)
        m = self.store.conn.execute("select coalesce(max(id),0) from marks").fetchone()[0]
        self.s["last_mark_id"] = max(self.s["last_mark_id"], int(m))
        if self.phase == "shadow":
            await self._shadow_round(candidates or [], rid, wh)
        elif self.phase == "probation":
            await self._probation_round(champion_n, sentinel_n, wh)
        else:
            for k, v in health.items():
                self.s["health_pending"][k] = self.s["health_pending"].get(k, 0) + v
            alarm = self._watch(champion_n, sentinel_n, wh)
            self._check_mde(wh)
            trig = self._trigger(wh, alarm)
            if trig is not None:
                await self._reflect_and_propose(trig[0], trig[1], wh)
        await self.apply_commands(wh)  # an owner command that arrived during this round
        try:
            self._save()
        except StateConflict:
            self.s = self._load()
            await self.apply_commands(wh)
            self._event(wh, "state_conflict", note="another writer saved during the round; reloaded")
            self._save()
        return list(self._new)

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
        if d is None:
            reflect.consume(self.store, cause, 0)  # stamped: the same cluster does not call the advisor again
            self.s["cooldown_until_wh"] = max(self.s["cooldown_until_wh"], wh + BASE_COOLDOWN)
            self._event(wh, "no_proposal", trigger=trigger, cooldown_wh=BASE_COOLDOWN)
            return
        await self._run(d, trigger, wh, cause=cause)

    # ------------------------------------------------------------ a proposal's life
    async def submit(self, delta: Delta, trigger: str = "owner") -> dict:
        """Put one delta through the gates (owner or test injection; the reflector uses the same path).
        Raises InFlight while another change is in shadow or probation."""
        self.s = self._load()
        wh = self.clock()
        self._reconcile(wh)
        await self._run(delta, trigger, wh)
        self._save_retry()
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
        self.store.conn.commit()
        pid = cur.lastrowid
        reflect.consume(self.store, cause, pid)
        self.s["proposal_id"] = pid
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
                                          proposal_id=pid)
            self._proposal(pid, "owner_review", "owner_review", res.detail, version_id=vid)
            self._event(wh, "owner_review", proposal=pid, note="apply with the 'apply' command after reading it")
            self.s["proposal_id"] = None
            return
        vid = challenger.save_version(self.store, status="shadow", parent=self.s["champion"], delta=delta,
                                      proposal_id=pid)
        self.s.update(challenger=vid, door=res.detail.get("door") or delta.door())
        self._proposal(pid, "shadow", "offline", res.detail, version_id=vid)
        if self.s["door"] == "arm":  # a query nobody ran cannot be shadowed: the bandit's trial + the CUSUM judge it
            self._promote(wh)
        else:
            self._set_phase("shadow")
            self.s["shadow_started_wh"] = wh
            self._event(wh, "shadow_started", proposal=pid, version=vid)

    async def _apply_owner(self, pid: int, wh: float) -> dict:
        """Owner command: promote a prompt proposal waiting for review, after a must-pass run with the LIVE model."""
        if self.phase in ("shadow", "probation"):
            return {"ok": False, "error": f"one change in flight: proposal {self.s['proposal_id']} is in {self.phase}"}
        r = self.store.conn.execute("select status, payload from proposals where id=?", (pid,)).fetchone()
        if r is None or r["status"] != "owner_review":
            return {"ok": False, "error": f"proposal {pid} is not waiting for owner review"}
        if self.llm is None:
            return {"ok": False, "error": "the live model is needed: start the agent (or pass an LLM client)"}
        delta = Delta.from_dict(json.loads(r["payload"])["delta"])
        champ = self.champion()
        try:
            challenger = champ.apply(champ.make_delta(delta.op, delta.item, hypothesis=delta.hypothesis,
                                                      evidence=delta.evidence, predicted_effect=delta.predicted_effect))
        except PlaybookError as e:
            self._proposal(pid, "rejected", "owner_apply", {"errors": [str(e)]})
            return {"ok": False, "error": str(e)}
        ok, bad = await gates.must_pass_ok(challenger, cfg=self.cfg, llm=self.llm)
        detail = {"live_must_pass": ok, "failed": bad, "applied_at_wh": round(wh, 2)}
        if not ok:
            self._proposal(pid, "rejected", "owner_live_must_pass", detail)
            self._event(wh, "owner_apply_failed", proposal=pid, failed=bad)
            return {"ok": False, "error": "must-pass failed with the live model", "failed": bad}
        vid = challenger.save_version(self.store, status="shadow", parent=self.s["champion"], delta=delta,
                                      proposal_id=pid)
        self.s.update(proposal_id=pid, challenger=vid, door="llm")
        self._proposal(pid, "probation", "owner_applied", detail, version_id=vid)
        self._promote(wh)
        return {"ok": True, "version": vid}

    async def _shadow_round(self, candidates: list[int], rid: int | None, wh: float) -> None:
        pid = self.s["proposal_id"]
        champ, chall = self.champion(), Playbook.load(self.s["challenger"], store=self.store)
        await shadow.score_round(champ, chall, candidates, store=self.store, proposal_id=pid, round_id=rid,
                                 cfg=self.cfg)
        shadow.auto_label(self.store, pid)
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
        self.s.update(champion=vid, previous=prev, probation=p.to_dict(), monitor=None, promoted_wh=wh)
        self._set_phase("probation")
        self._event(wh, "promoted", proposal=pid, version=vid, previous=prev)
        set_status(self.store, vid, "promoted", commit=False)
        self._proposal(pid, "probation", "promoted", {}, commit=False)
        self._save()  # one commit: version status + proposal + state
        Playbook.load(vid, store=self.store).activate(self.root)

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
        the equal watched window before it, provided the sentinel's sources show no such rise (else: the sites)."""
        t0 = self.s.get("promoted_wh")
        if t0 is None:
            return []
        span = wh - float(t0)
        after = monitor.health_between(self.store, float(t0), wh)
        before = monitor.health_between(self.store, float(t0) - span, float(t0))
        rising = [k for k, n in after.items() if n >= gates.HEALTH_ALARM_EVENTS and n > 2 * before.get(k, 0)]
        sent = monitor.sentinel_sources(self.sentinel_playbook())
        if any(k.split("|", 1)[0] in sent or any(s in k.split("|", 1)[0] for s in sent) for k in rising):
            return []  # the control group is unhealthy too: the sites changed, not the playbook
        return sorted(rising)

    def _rollback(self, wh: float, why: list) -> None:
        """Version status and state in ONE transaction, then the file."""
        failed, pid = self.s["champion"], self.s["proposal_id"]
        set_status(self.store, failed, "failed_in_prod", commit=False)
        restored = active_version(self.store)
        if restored is None or restored == failed:
            raise PlaybookError("no promoted or baseline version to roll back to")
        self.s.update(champion=restored, monitor=None)
        self._event(wh, "rolled_back", proposal=pid, failed=failed, to=restored, why=why)
        self._reject(wh, "probation", {"why": why}, status="failed_in_prod", version_status=None, commit=False)
        self._save()
        Playbook.load(restored, store=self.store).activate(self.root)

    def _accept(self, wh: float) -> None:
        pid = self.s["proposal_id"]
        self._proposal(pid, "accepted", "probation_passed", {})
        self._event(wh, "accepted", proposal=pid, version=self.s["champion"])
        self.s.update(consecutive_rejections=0, cooldown_until_wh=wh, proposal_id=None, challenger=None, door=None,
                      probation=None, shadow_started_wh=None, promoted_wh=None)
        self._rest()

    def _reject(self, wh: float, stage: str, detail: dict, *, status: str = "rejected",
                version_status: str | None = "rejected", commit: bool = True) -> None:
        pid = self.s["proposal_id"]
        if version_status and self.s.get("challenger"):
            set_status(self.store, self.s["challenger"], version_status, commit=commit)
        self._proposal(pid, status, stage, detail, commit=commit)
        n = self.s["consecutive_rejections"] + 1
        cool = gates.cooldown_hours(n)
        self.s.update(consecutive_rejections=n, cooldown_until_wh=wh + cool, proposal_id=None, challenger=None,
                      door=None, probation=None, shadow_started_wh=None, promoted_wh=None)
        self._event(wh, "rejected", proposal=pid, stage=stage, consecutive=n, cooldown_wh=cool)
        if n >= gates.MAINTENANCE_AFTER_REJECTIONS and not self.s["maintenance"]:
            self._enter_maintenance(wh, f"{n} consecutive rejections")
        self._rest()

    def _proposal(self, pid: int | None, status: str, stage: str, detail: dict, version_id: int | None = None,
                  commit: bool = True) -> None:
        if pid is None:
            return
        r = self.store.conn.execute("select payload from proposals where id=?", (pid,)).fetchone()
        payload = json.loads(r["payload"]) if r else {}
        payload["stage"] = stage
        self.store.conn.execute(
            "update proposals set status=?, stage=?, payload=?, detail=?, decided_at=?, version_id=coalesce(?, version_id) "
            "where id=?", (status, stage, json.dumps(payload, default=str), json.dumps(detail, default=str)[:20000],
                           _now(), version_id, pid))
        if commit:
            self.store.conn.commit()

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
        if pid:
            self._proposal(pid, "aborted", "owner_rollback", {"to": version}, commit=False)
            if self.s.get("challenger") and self.phase == "shadow":
                set_status(self.store, self.s["challenger"], "aborted", commit=False)
        vid = target.save_version(self.store, status="promoted", parent=self.s["champion"],
                                  note=f"owner rollback to v{version}", commit=False)
        self.s.update(champion=vid, challenger=None, proposal_id=None, door=None, probation=None,
                      shadow_started_wh=None, monitor=None, promoted_wh=None)
        self._rest()
        self._event(wh, "owner_rollback", to=version, as_version=vid)
        self._save()
        target.activate(self.root)
        return {"ok": True, "version": vid, "to": version}
