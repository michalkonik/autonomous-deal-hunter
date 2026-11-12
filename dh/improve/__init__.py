"""Gated self-improvement loop (spec §9). See docs/SELF-IMPROVEMENT.md.

Wiring (done by the controller after Task 19; this package never edits dh/cli.py or dh/scheduler.py):
- after every round:     `await improve.after_round(ctx, stats)`   (runtime.serve's rounds loop and `dh round`)
- weekly report:         `await improve.report.weekly_async(store)` -> Markdown for the log repo
- CLI:                   cli_playbook_show, cli_eval, cli_rollback, cli_status, cli_maintenance, cli_pending,
                         cli_label, cli_apply, cli_weekly; `dh eval --add-from-marks` -> evals.marks.add_from_marks
                         (owner only; refused while the agent runs)
Every cli_* function returns (text, exit_code). SINGLE WRITER: only the holder of the agent lock
(runtime.AgentLock: serve, a foreground `dh round`, or `apply_queued_now`) mutates the loop's state. Owner commands
(rollback, maintenance, label, apply) are always QUEUED (improve_commands); the running agent applies them at its
next round; with no agent running the CLI applies them under the lock (`apply_queued_now`).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from . import gates, monitor, report, shadow
from .controller import Controller, InFlight, command_result, enqueue, pending_applies, rollback_warning
from .playbook import ROOT, Playbook, active_version, versions

__all__ = ["after_round", "apply_queued_now", "Controller", "InFlight", "cli_playbook_show", "cli_eval", "cli_rollback", "cli_status",
           "cli_maintenance", "cli_pending", "cli_label", "cli_apply", "cli_weekly"]


async def after_round(ctx, stats, *, controller: Controller | None = None) -> list[dict]:
    """Round hook: per-round counts (champion deals, sentinel judged by the pinned playbook), the round's evaluated
    listings for shadow scoring, then one controller step. Never raises into the round loop's caller for data
    problems of a single round; returns the controller's events."""
    store = ctx.store
    ctl = controller or Controller(store, root=Path(ctx.root), cfg=ctx.cfg, llm=ctx.llm)
    rid = getattr(stats, "round_id", None)
    if rid is None:
        return []
    c, s, d, lids = await monitor.round_counts(store, rid, ctl.sentinel_playbook(), list(ctx.adapters), cfg=ctx.cfg)
    st = stats.to_dict() if hasattr(stats, "to_dict") else dict(stats)
    return await ctl.on_round(st, champion_n=c, sentinel_n=s, deals_n=d, candidates=lids, round_id=rid)


# ------------------------------------------------------------------ CLI helpers


def _store(root: Path | None, store):
    if store is not None:
        return store, False
    from ..runtime import db_path
    from ..store import Store
    return Store(db_path(Path(root or ROOT))), True


def _with_store(fn):
    def wrapper(*a, root: Path | None = None, store=None, **kw):
        st, own = _store(root, store)
        try:
            return fn(*a, root=Path(root or ROOT), store=st, **kw)
        finally:
            if own:
                st.close()
    wrapper.__name__, wrapper.__doc__ = fn.__name__, fn.__doc__
    return wrapper


@_with_store
def cli_playbook_show(*, root: Path, store) -> tuple[str, int]:
    """`dh playbook`: the active items and the version history."""
    pb = Playbook.load(store=store, root=root)
    lines = [f"active playbook: {'v' + str(active_version(store)) if active_version(store) else 'file (no versions yet)'}"
             f", {len(pb)} items"]
    for it in pb.items:
        v = it.value if it.kind != "prompt" else f"<{len(str(it.value))} chars>"
        lines.append(f"  {'*' if it.sentinel else ' '} {it.id:<34} {it.kind:<8} {it.target:<28} {v}")
    lines.append("versions:")
    for v in versions(store):
        d = json.loads(v["delta"]) if v["delta"] else None
        what = f"{d['op']} {d['item']['id']}" if d else "(full snapshot)"
        lines.append(f"  {v['version']:<5} {v['status'] or '':<15} {what:<40} {v['created_at']}")
    return "\n".join(lines), 0


@_with_store
def cli_eval(version: int | None = None, *, root: Path, store) -> tuple[str, int]:
    """`dh eval`: must-pass and golden scores of the active (or a given) playbook. Exit 1 if must-pass < 100%."""
    pb = Playbook.load(version, store=store, root=root)

    async def go():
        mp = await gates.score_cases(pb, gates.load_cases(gates.MUST_PASS))
        gold = await gates.score_cases(pb, gates.load_cases(gates.GOLDEN))
        return mp, gold
    mp, gold = asyncio.run(go())
    bad = [r for r in mp if not r.ok]
    lines = [f"must-pass: {len(mp) - len(bad)}/{len(mp)}" + ("" if not bad else " FAILED"),
             *[f"  FAIL {r.id}: {r.why}" for r in bad],
             f"golden:    {sum(r.ok for r in gold)}/{len(gold)} correct",
             *[f"  miss {r.id}: {r.why}" for r in gold if not r.ok][:30]]
    return "\n".join(lines), 1 if bad or not mp else 0


def agent_running(root: Path | None = None) -> bool:
    """The systemd unit is active, a foreground `dh round` runs, or a `dh.runtime serve` process runs outside the
    unit (pgrep) (belt and braces: a process started before the agent lock existed holds no lock)."""
    try:
        from .. import runtime
        return (bool(runtime.is_active()) or (root is not None and runtime.round_running(root) is not None)
                or bool(runtime.serve_processes()))
    except Exception:  # noqa: BLE001 - no systemd (tests, other OS): treat as not running
        return False


def apply_queued_now(store, root: Path, *, llm=None, cfg=None) -> int | None:
    """One-shot single writer for when no agent runs: take the agent lock, apply the queued owner commands (the
    controller's command step, in one transaction), release. None when another process holds the lock (the agent
    applies them at its next round). Without `llm` a queued apply is NOT consumed: it stays queued for the running
    agent, and every other command still applies in queue order. Returns the number applied."""
    from ..runtime import AgentLock
    lock = AgentLock(root, role="cli")  # serve waits for an owner command to finish
    if not lock.acquire():
        return None
    try:
        return asyncio.run(Controller(store, root=root, llm=llm, cfg=cfg).process_commands())
    finally:
        lock.release()


def _queue(store, root: Path, cmd: str, running: bool | None, llm=None, **args) -> tuple[str, int]:
    """The CLI never applies a command itself: it is always queued; with no agent running, it is then applied
    under the agent lock by `apply_queued_now`."""
    cid = enqueue(store, cmd, **args)
    if running is None:
        running = agent_running(root)
    queued = (f"queued as command {cid}: the running agent applies it at its next round", 0)
    if running or apply_queued_now(store, root, llm=llm) is None:
        return queued
    res = command_result(store, cid)
    if res is None:  # left queued: an owner apply needs the live model (other commands never wait behind it)
        why = ("apply waits for the running agent (it needs the live model)" if cmd == "apply"
               else "the running agent applies it at its next round")
        return f"queued as command {cid}: {why}", 0
    return json.dumps(res, default=str), 0 if res.get("ok") else 1


@_with_store
def cli_rollback(version: int, *, root: Path, store, running: bool | None = None) -> tuple[str, int]:
    """`dh playbook --rollback N`: make version N active again (aborts any change in flight). The reply warns about
    owner applies still queued: the rollback does not cancel them (applied now: in the JSON as `warning`)."""
    text, code = _queue(store, root, "rollback", running, version=int(version))
    if text.startswith("queued"):
        w = rollback_warning(pending_applies(store), int(version))
        text = text + ("\n" + w if w else "")
    return text, code


@_with_store
def cli_status(*, root: Path, store) -> tuple[str, int]:
    """`dh status` (improvement section): phase, cooldown, shadow tally, probation, owner reviews, recent events."""
    return json.dumps(Controller(store, root=root).status(), indent=2, default=str), 0


@_with_store
def cli_maintenance(on: bool, *, root: Path, store, running: bool | None = None) -> tuple[str, int]:
    """Force maintenance mode on (freeze the playbook) or off (allow gated proposals again)."""
    return _queue(store, root, "maintenance", running, on=bool(on))


@_with_store
def cli_pending(*, root: Path, store) -> tuple[str, int]:
    """What waits for the owner: prompt proposals to review, and shadow disagreements to label."""
    lines = []
    for r in store.conn.execute("select id, payload from proposals where status='owner_review' order by id"):
        d = json.loads(r["payload"])["delta"]
        lines.append(f"proposal {r['id']} (prompt, owner review): {d['op']} {d['item']['id']}\n"
                     f"  hypothesis: {d.get('hypothesis')}\n  predicted: {d.get('predicted_effect')}\n"
                     f"  new text:\n" + "\n".join("    " + x for x in str(d['item']['value']).splitlines()))
    for d in shadow.pending(store):
        lines.append(f"disagreement {d.id}: listing {d.listing_id} ({d.kind}) champion {d.champion.key}/"
                     f"{d.champion.tier} vs challenger {d.challenger.key}/{d.challenger.tier}")
    return "\n".join(lines) or "nothing waits for the owner", 0


@_with_store
def cli_label(disagreement_id: int, truth: str, *, root: Path, store, running: bool | None = None) -> tuple[str, int]:
    """Owner label for one disagreement: truth 'deal' | 'not_deal' | 'key:<key or none>'."""
    return _queue(store, root, "label", running, id=int(disagreement_id), truth=str(truth))


@_with_store
def cli_apply(proposal_id: int, *, root: Path, store, running: bool | None = None, llm=None) -> tuple[str, int]:
    """Owner: apply a prompt proposal waiting for review. It first runs the must-pass set with the LIVE model; on
    success the change is promoted and enters probation. Needs the model (the running agent, or `llm`)."""
    if llm is None and not (agent_running(root) if running is None else running):
        from .. import config as dhconfig
        from ..llm import Client
        llm = Client(dhconfig.load(root))
    return _queue(store, root, "apply", running, llm=llm, proposal_id=int(proposal_id))


@_with_store
def cli_weekly(*, root: Path, store) -> tuple[str, int]:
    """The weekly report (Markdown)."""
    return report.weekly(store), 0
