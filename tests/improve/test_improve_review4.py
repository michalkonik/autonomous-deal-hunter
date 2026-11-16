"""M3 review round 4 (single-writer follow-ups): the pending file write keeps its original `from` across several
champion changes (I1); no step ever has its transaction ended under it, by a shared connection or a model call
awaited inside it (I2, probe C); and the minors (lock wait in serve, `dh start` vs the lock, reconcile without a
commit, a queued apply waits for the live model, no events from an aborted step, pgrep in agent_running)."""
import asyncio
import io
import json
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from dh import config, notify, runtime
from dh.improve import _queue, agent_running, apply_queued_now, cli_maintenance, cli_rollback
from dh.improve import gates as gmod
from dh.improve.controller import Controller, enqueue
from dh.improve.playbook import Item, Playbook, version_sha
from dh.store import Store
from improve_fix import REPO, seed_playbook
from test_improve_controller import Clock, step
from test_improve_review2 import TriageLLM


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "repo"
    (root / "playbook").mkdir(parents=True)
    seed_playbook().activate(root)
    store = Store(tmp_path / "dh.db")
    yield store, root, Clock(), tmp_path
    store.close()


def saved(store):
    r = store.conn.execute("select value from improve_state where key='controller'").fetchone()
    return json.loads(r["value"])


def vstat(store):
    return {r["id"]: r["status"] for r in store.conn.execute("select id, status from playbook_versions")}


def notes(store, like):
    return store.conn.execute("select count(*) from playbook_versions where note like ?", (like,)).fetchone()[0]


def health(store, kind):
    return store.conn.execute("select count(*) from health where kind=?", (kind,)).fetchone()[0]


ARC1 = Item("query.arc_b580", "query", "all", "arc b580", provenance="owner")
ARC2 = Item("query.arc_b770", "query", "all", "arc b770", provenance="owner")


def owner_edit(root, item):
    fb = Playbook.from_file(root)
    (root / "playbook" / "items.yaml").write_text(fb.apply(fb.make_delta("add", item)).to_yaml())


def on_disk(root):
    return Playbook.from_file(root).sha()


async def warm(ctl, clock, n=20):
    for r in range(1, n + 1):
        await step(ctl, clock, 1 / 6, rid=r)


# ------------------------------------------------------------------ I1: file_pending keeps its original `from`


def test_i1_two_rollbacks_in_one_step_write_the_last_target(env):
    store, root, clock, d = env
    ctl = Controller(store, root=root, clock=clock)
    run(warm(ctl, clock, 2))  # v1 baseline
    owner_edit(root, ARC1)
    run(step(ctl, clock, 1 / 6, rid=3))  # v2
    owner_edit(root, ARC2)
    run(step(ctl, clock, 1 / 6, rid=4))  # v3, on disk
    assert saved(store)["champion"] == 3 and on_disk(root) == version_sha(store, 3)
    enqueue(store, "rollback", version=1)
    enqueue(store, "rollback", version=2)
    assert run(ctl.process_commands()) == 2  # ONE step: v4 (= v1), then v5 (= v2)
    s = saved(store)
    assert s["file_pending"] is None and s["champion"] == 5
    assert on_disk(root) == version_sha(store, 2)  # the file is the LAST target, written over the original v3
    run(step(ctl, clock, 1 / 6, rid=5))
    assert notes(store, "owner revert%") == 0 and notes(store, "owner edit%") == 2
    assert saved(store)["champion"] == 5


def test_i1_crash_recovery_then_a_rollback_in_the_same_step(env, monkeypatch):
    store, root, clock, d = env
    ctl = Controller(store, root=root, clock=clock)
    run(warm(ctl, clock))
    owner_edit(root, ARC1)
    run(step(ctl, clock, 1 / 6, rid=21))  # v2 baseline (owner edit), on disk

    def crash(self):
        raise KeyboardInterrupt("crash")
    monkeypatch.setattr(Controller, "_sync_file", crash)
    with pytest.raises(KeyboardInterrupt):  # v3 (cadence) promoted and committed; the file is still v2
        run(ctl.submit(ctl.champion().make_delta("add", Item("cadence.olx", "cadence", "olx", 15))))
    monkeypatch.undo()
    assert on_disk(root) == version_sha(store, 2)
    enqueue(store, "rollback", version=1)
    fresh = Controller(Store(d / "dh.db"), root=root, clock=clock)
    assert run(fresh.process_commands()) == 1  # reconcile (file_reconciled) + the rollback, one step
    s = saved(store)
    assert health(store, "file_reconciled") == 1
    assert s["file_pending"] is None and on_disk(root) == version_sha(store, 1)
    run(step(fresh, clock, 1 / 6, rid=22))
    assert notes(store, "owner revert%") == 0 and notes(store, "owner edit%") == 1
    assert saved(store)["champion"] == s["champion"]


def test_i1_state_mismatch_then_a_rollback_in_the_same_step(env):
    store, root, clock, d = env
    ctl = Controller(store, root=root, clock=clock)
    run(warm(ctl, clock, 2))
    owner_edit(root, ARC1)
    run(step(ctl, clock, 1 / 6, rid=3))  # v2, on disk, the state's champion
    fb = Playbook.from_file(root)  # (legacy) a version made active behind the state's back
    fb.apply(fb.make_delta("add", ARC2)).save_version(store, status="promoted", parent=2, note="legacy")
    enqueue(store, "rollback", version=1)
    assert run(ctl.process_commands()) == 1
    s = saved(store)
    assert health(store, "state_mismatch") == 1
    assert s["file_pending"] is None and on_disk(root) == version_sha(store, 1)
    run(step(ctl, clock, 1 / 6, rid=4))
    assert notes(store, "owner revert%") == 0 and notes(store, "owner edit%") == 1
    assert saved(store)["champion"] == s["champion"]


# ------------------------------------------------------------------ I2: the step's transaction


def _triage_proposal(store, root, clock):
    ctl = Controller(store, root=root, clock=clock)
    pb = ctl.champion()
    old = pb.get("prompt.triage")
    run(ctl.submit(pb.make_delta("update", Item(old.id, "prompt", old.target, old.value.replace(
        "Judge only what the listing shows;", "Judge only what this listing shows;"), extra=old.extra))))
    pid = store.conn.execute("select max(id) from proposals").fetchone()[0]
    assert store.conn.execute("select status from proposals where id=?", (pid,)).fetchone()[0] == "owner_review"
    return pid


class YieldingTriage(TriageLLM):
    """A live model: every call really suspends (network I/O), so other coroutines run meanwhile."""

    async def chat_json(self, role, system, user, schema, max_tokens=600):
        await asyncio.sleep(0)
        return await super().chat_json(role, system, user, schema, max_tokens)


def test_probe_c_a_concurrent_ensure_table_during_an_apply(env):
    """Probe C: the old serve shared `store.conn` with the publisher, whose ensure_table COMMITs. While the owner's
    apply awaited the live model inside the step, that commit ended the step's transaction ("no such savepoint").
    The live must-pass now runs before the step opens its transaction: the step is fully applied."""
    store, root, clock, d = env
    pid = _triage_proposal(store, root, clock)
    enqueue(store, "apply", proposal_id=pid)
    ctl = Controller(store, root=root, clock=clock, llm=YieldingTriage())
    commits = {"n": 0}

    async def go():
        done = asyncio.Event()

        async def publisher():  # the worst case: the SAME connection
            while not done.is_set():
                store.ensure_table("create table if not exists probe_c (x integer)")
                commits["n"] += 1
                await asyncio.sleep(0)
        t = asyncio.create_task(publisher())
        try:
            return await ctl.process_commands()
        finally:
            done.set()
            await t
    assert run(go()) == 1
    assert commits["n"] > 1  # the publisher really ran during the apply
    res = json.loads(store.conn.execute("select result from improve_commands").fetchone()[0])
    assert res["ok"] is True
    s = saved(store)
    assert s["phase"] == "probation" and s["champion"] == res["version"] and s["file_pending"] is None
    assert store.conn.execute("select status from proposals where id=?", (pid,)).fetchone()[0] == "probation"
    assert vstat(store)[res["version"]] == "promoted"
    assert "this listing shows" in Playbook.from_file(root).get("prompt.triage").value


def test_the_live_must_pass_runs_outside_the_step_transaction(env, monkeypatch):
    store, root, clock, d = env
    pid = _triage_proposal(store, root, clock)
    enqueue(store, "apply", proposal_id=pid)
    seen = []
    orig = gmod.must_pass_live

    async def spy(*a, **kw):
        seen.append(store.conn.in_transaction)
        return await orig(*a, **kw)
    monkeypatch.setattr(gmod, "must_pass_live", spy)
    assert run(Controller(store, root=root, clock=clock, llm=TriageLLM()).process_commands()) == 1
    assert seen == [False]


def test_a_step_whose_transaction_was_ended_under_it_is_aborted(env, monkeypatch):
    store, root, clock, d = env
    ctl = Controller(store, root=root, clock=clock)
    run(ctl.process_commands())
    n_versions = len(vstat(store))
    before = saved(store)
    enqueue(store, "rollback", version=1)
    orig = ctl._force_rollback

    def committed_meanwhile(version, wh):  # a coroutine sharing the connection commits in the middle of the step
        store.conn.commit()
        return orig(version, wh)
    monkeypatch.setattr(ctl, "_force_rollback", committed_meanwhile)
    assert run(ctl.process_commands()) == 0  # aborted, with a health event, never committed half-way
    monkeypatch.undo()
    assert len(vstat(store)) == n_versions
    assert store.conn.execute("select applied_at from improve_commands").fetchone()[0] is None
    assert health(store, "improve_step_interrupted") == 1
    assert saved(store) == before
    assert run(ctl.process_commands()) == 1  # the next step applies it
    assert len(vstat(store)) == n_versions + 1


def test_serve_gives_the_publisher_its_own_connection_and_waits_for_the_lock(tmp_path, monkeypatch):
    seen = {}

    async def fake_round(ctx):
        seen["round"] = ctx.store
        ctx.stop.set()
        from dh.scheduler import RoundStats
        return RoundStats(round_id=None)

    async def fake_publish(store, cfg, root, stop):
        seen["publish"] = store
        await stop.wait()

    async def fake_warm(cfg, **kw):
        return {}
    monkeypatch.setattr(runtime, "warm_models", fake_warm)
    monkeypatch.setattr(runtime.scheduler, "run_round", fake_round)
    monkeypatch.setattr(runtime.publish, "run_loop", fake_publish)
    monkeypatch.setattr(runtime, "AGENT_LOCK_WAIT_S", 10.0)
    monkeypatch.setattr(runtime, "AGENT_LOCK_STEP_S", 0.05)
    data = config.load(REPO).to_dict()
    data["scheduler"] = {**data.get("scheduler", {}), "round_interval_s": 0}
    c = config.Config(data, REPO)
    holder = runtime.AgentLock(tmp_path)  # e.g. the CLI applying a queued command right when the unit starts
    assert holder.acquire()

    async def go():
        asyncio.get_running_loop().call_later(0.3, holder.release)
        return await runtime.serve_async(c, tmp_path, stop=asyncio.Event(), install_signals=False, web=False)
    assert run(go()) == 0 and "round" in seen
    assert seen["publish"] is not seen["round"] and seen["publish"].conn is not seen["round"].conn
    with pytest.raises(sqlite3.ProgrammingError):  # closed when serve ends
        seen["publish"].conn.execute("select 1")
    assert holder.acquire()  # a holder that never lets go: serve gives up after the wait
    monkeypatch.setattr(runtime, "AGENT_LOCK_WAIT_S", 0.2)
    seen.clear()
    try:
        assert run(runtime.serve_async(c, tmp_path, stop=asyncio.Event(), install_signals=False, web=False)) == 1
        assert seen == {}
    finally:
        holder.release()


def test_retry_pending_commits_before_each_delivery(tmp_path, monkeypatch):
    """With the publisher on its own connection, no coroutine may keep a write transaction open across real I/O."""
    store = Store(tmp_path / "dh.db")
    now = datetime.now(timezone.utc)
    notify._enqueue(store, {"title": "a"}, now - timedelta(hours=7), "x")  # expired: deleted
    notify._enqueue(store, {"title": "b"}, now, "x")
    notify._enqueue(store, {"title": "c"}, now, "x")
    seen = []

    async def deliver(payload, store_, *a):
        seen.append(store.conn.in_transaction)
        return None if payload["title"] == "c" else "down"
    monkeypatch.setattr(notify, "_deliver", deliver)
    assert run(notify.retry_pending(store, None, tmp_path, now=now)) == 1
    assert seen == [False, False]
    assert not store.conn.in_transaction
    store.close()


# ------------------------------------------------------------------ minors


def test_dh_start_refuses_while_another_process_holds_the_agent_lock(tmp_path):
    calls = []

    def sysrun(cmd, **kw):
        calls.append(list(cmd))
        out, rc = ("inactive\n", 3) if cmd[:3] == ["systemctl", "--user", "is-active"] else ("", 0)
        return subprocess.CompletedProcess(cmd, rc, out, "")
    holder = runtime.AgentLock(tmp_path)
    assert holder.acquire()
    try:
        out = io.StringIO()
        rc = runtime.start(config.load(REPO), tmp_path, run=sysrun, out=out, meminfo=tmp_path / "none", wait_s=0)
        assert rc == 1 and "agent lock" in out.getvalue()
        assert not any(c[0] == "systemd-run" for c in calls)
    finally:
        holder.release()


def test_reconcile_writes_its_baseline_inside_the_step(env):
    store, root, clock, d = env
    ctl = Controller(store, root=root, clock=clock)
    run(ctl.process_commands())
    store.conn.execute("delete from playbook_versions")  # e.g. the table restored from an older backup
    store.conn.commit()

    def crash():
        raise KeyboardInterrupt("crash")
    ctl._commit = crash
    with pytest.raises(KeyboardInterrupt):
        run(ctl.process_commands())
    assert len(vstat(store)) == 0  # the baseline written by _reconcile was rolled back with the step


def applied(store):
    return [r["applied_at"] is not None for r in store.conn.execute("select applied_at from improve_commands order by id")]


def test_a_queued_apply_waits_for_the_live_model_but_nothing_waits_behind_it(env):
    store, root, clock, d = env
    pid = _triage_proposal(store, root, clock)
    enqueue(store, "apply", proposal_id=pid)  # queued while the agent ran; the agent has stopped since
    text, code = cli_maintenance(True, root=root, store=store, running=False)  # applied under the lock, no model
    assert code == 0 and json.loads(text)["maintenance"] is True  # the safety command applied at once
    assert applied(store) == [False, True]  # the apply stays queued, not consumed
    assert store.conn.execute("select status from proposals where id=?", (pid,)).fetchone()[0] == "owner_review"
    assert run(Controller(store, root=root, clock=clock, llm=TriageLLM()).process_commands()) == 1  # the apply
    res = [json.loads(r["result"]) for r in store.conn.execute("select result from improve_commands order by id")]
    assert all(r["ok"] for r in res)
    s = saved(store)
    assert s["maintenance"] is True and s["phase"] == "probation"


def test_a_rollback_never_waits_behind_a_queued_apply(env):
    """Ruling: [apply X, rollback 1] queued with the agent stopped: the rollback applies at once, the apply waits."""
    store, root, clock, d = env
    ctl = Controller(store, root=root, clock=clock)
    run(warm(ctl, clock, 2))
    owner_edit(root, ARC1)
    run(step(ctl, clock, 1 / 6, rid=3))  # v2 on disk
    pid = _triage_proposal(store, root, clock)
    text, code = _queue(store, root, "apply", False, proposal_id=pid)  # no agent, no model: it waits
    assert code == 0 and "apply waits for the running agent" in text
    assert apply_queued_now(store, root) == 0
    text, code = cli_rollback(1, root=root, store=store, running=False)  # no agent, no model
    assert code == 0 and json.loads(text)["ok"] is True
    assert applied(store) == [False, True]
    s = saved(store)
    assert on_disk(root) == version_sha(store, 1) and s["file_pending"] is None
    assert store.conn.execute("select status from proposals where id=?", (pid,)).fetchone()[0] == "owner_review"
    # the agent's command step with its model down skips the apply the same way
    assert run(Controller(store, root=root, clock=clock).process_commands()) == 0
    assert applied(store) == [False, True]


def test_on_round_returns_no_events_from_an_aborted_step(env, monkeypatch):
    store, root, clock, d = env
    ctl = Controller(store, root=root, clock=clock)
    run(warm(ctl, clock, 2))
    enqueue(store, "maintenance", on=True)
    orig = ctl._set_maintenance

    def second_writer_meanwhile(on, wh, reason="owner"):
        b = Controller(Store(d / "dh.db"), root=root, clock=clock)
        b.s["consecutive_rejections"] = 2
        b._commit()
        b.store.close()
        return orig(on, wh, reason)
    monkeypatch.setattr(ctl, "_set_maintenance", second_writer_meanwhile)
    clock.wh += 1
    evs = run(ctl.on_round({}, champion_n=1, sentinel_n=1, candidates=[], round_id=None))
    assert not any(e["event"] == "maintenance" for e in evs)
    assert evs == [] and saved(store)["maintenance"] is False


def test_agent_running_also_sees_serve_processes(env, monkeypatch):
    store, root, clock, d = env
    monkeypatch.setattr(runtime, "is_active", lambda run=None: False)
    monkeypatch.setattr(runtime, "serve_processes", lambda run=None: ["4242 python -m dh.runtime serve"])
    assert agent_running(root) is True  # a serve started by hand, outside the unit
    monkeypatch.setattr(runtime, "serve_processes", lambda run=None: [])
    assert agent_running(root) is False
