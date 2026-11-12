"""Deferred minors of the improvement loop: the rollback reply warns about queued applies, queued applies expire
(improve.apply_expiry_days), and serve waits for an owner command that holds the agent lock."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

from dh import config, runtime
from dh.improve import cli_rollback
from dh.improve.controller import Controller, enqueue
from improve_fix import REPO
from test_improve_controller import step
from test_improve_review2 import TriageLLM
from test_improve_review4 import _triage_proposal, applied, env, saved, warm  # noqa: F401 - env is a fixture


def run(coro):
    return asyncio.run(coro)


def _age(store, cid, days):
    at = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    store.conn.execute("update improve_commands set created_at=? where id=?", (at, cid))
    store.conn.commit()


def test_rollback_reply_warns_about_queued_applies_when_applied_now(env):
    store, root, clock, d = env
    ctl = Controller(store, root=root, clock=clock)
    run(warm(ctl, clock, 2))
    pid = _triage_proposal(store, root, clock)
    cid = enqueue(store, "apply", proposal_id=pid)
    text, code = cli_rollback(1, root=root, store=store, running=False)  # no agent: applied at once, no model
    res = json.loads(text)
    assert code == 0 and res["ok"] is True
    assert res["pending_applies"] == [{"command": cid, "proposal": pid, "queued_at": res["pending_applies"][0]["queued_at"]}]
    assert "warning: 1 owner apply still queued" in res["warning"] and f"proposal {pid}" in res["warning"]
    assert applied(store) == [False, True]  # the rollback does not cancel the apply


def test_rollback_reply_warns_about_queued_applies_when_queued(env):
    store, root, clock, d = env
    pid = _triage_proposal(store, root, clock)
    cid = enqueue(store, "apply", proposal_id=pid)
    text, code = cli_rollback(1, root=root, store=store, running=True)  # the agent applies it at its next round
    assert code == 0 and text.startswith("queued as command")
    assert f"command {cid} (proposal {pid})" in text and "on top of v1" in text


def test_rollback_reply_without_queued_applies_has_no_warning(env):
    store, root, clock, d = env
    ctl = Controller(store, root=root, clock=clock)
    run(warm(ctl, clock, 2))
    text, code = cli_rollback(1, root=root, store=store, running=False)
    assert "warning" not in json.loads(text)
    text, code = cli_rollback(1, root=root, store=store, running=True)
    assert "warning" not in text


def test_a_queued_apply_expires_after_apply_expiry_days(env):
    store, root, clock, d = env
    pid = _triage_proposal(store, root, clock)
    old = enqueue(store, "apply", proposal_id=pid)
    fresh = enqueue(store, "apply", proposal_id=pid)
    _age(store, old, 8)  # default improve.apply_expiry_days: 7
    _age(store, fresh, 6)
    llm = TriageLLM()
    ctl = Controller(store, root=root, clock=clock)  # the model is down: a fresh apply waits, an old one expires
    assert run(ctl.process_commands()) == 1
    assert applied(store) == [True, False]
    res = json.loads(store.conn.execute("select result from improve_commands where id=?", (old,)).fetchone()[0])
    assert res["ok"] is False and res["expired"] is True and f"--apply {pid}" in res["error"]
    assert store.conn.execute("select status from proposals where id=?", (pid,)).fetchone()[0] == "owner_review"
    _age(store, fresh, 30)
    ctl = Controller(store, root=root, clock=clock, llm=llm)  # even with the model up, an expired apply never runs
    calls_before = len(llm.systems)
    assert run(ctl.process_commands()) == 1 and applied(store) == [True, True]
    assert len(llm.systems) == calls_before  # no live must-pass spent on it
    assert saved(store)["phase"] != "probation"  # nothing was promoted


def test_apply_expiry_days_comes_from_config(env):
    store, root, clock, d = env
    pid = _triage_proposal(store, root, clock)
    cid = enqueue(store, "apply", proposal_id=pid)
    _age(store, cid, 2)
    data = config.load(REPO).to_dict()
    data["improve"] = {**data.get("improve", {}), "apply_expiry_days": 1}
    ctl = Controller(store, root=root, clock=clock, cfg=config.Config(data, REPO))
    assert run(ctl.process_commands()) == 1 and applied(store) == [True]


def test_serve_waits_for_an_owner_command_longer_than_for_another_agent(tmp_path, monkeypatch):
    """The CLI holding the lock for a queued owner command (a live must-pass can take minutes) must not make the
    starting unit give up after AGENT_LOCK_WAIT_S; another agent holding it still does."""
    seen = []

    async def fake_round(ctx):
        seen.append(1)
        ctx.stop.set()
        from dh.scheduler import RoundStats
        return RoundStats(round_id=None)

    async def fake_publish(store, cfg, root, stop):
        await stop.wait()

    async def fake_warm(cfg, **kw):
        return {}
    monkeypatch.setattr(runtime, "warm_models", fake_warm)
    monkeypatch.setattr(runtime.scheduler, "run_round", fake_round)
    monkeypatch.setattr(runtime.publish, "run_loop", fake_publish)
    monkeypatch.setattr(runtime, "AGENT_LOCK_WAIT_S", 0.2)
    monkeypatch.setattr(runtime, "AGENT_LOCK_STEP_S", 0.05)
    data = config.load(REPO).to_dict()
    data["scheduler"] = {**data.get("scheduler", {}), "round_interval_s": 0}
    c = config.Config(data, REPO)

    cli = runtime.AgentLock(tmp_path, role="cli")
    assert cli.acquire() and runtime.lock_holder(tmp_path)[1] == "cli"

    async def go():
        asyncio.get_running_loop().call_later(0.8, cli.release)  # 4x longer than the agent wait
        return await runtime.serve_async(c, tmp_path, stop=asyncio.Event(), install_signals=False, web=False)
    assert run(go()) == 0 and seen == [1]

    other = runtime.AgentLock(tmp_path)  # another agent (serve, `dh round`) that never lets go
    assert other.acquire() and runtime.lock_holder(tmp_path)[1] == "agent"
    try:
        assert run(runtime.serve_async(c, tmp_path, stop=asyncio.Event(), install_signals=False, web=False)) == 1
    finally:
        other.release()


# ------------------------------------------------------------------ review of the 30 Oct wave


class _Unavailable:
    """Every call refused as unavailable (model down, or another workload's model loaded: scheduler._CountingLLM)."""

    def __init__(self):
        self.calls = 0

    async def chat_json(self, *a, **kw):
        from dh.llm import LLMUnavailable
        self.calls += 1
        raise LLMUnavailable("llama-swap runs a model outside the roles: gemma-4-26b-a4b")


def test_an_owner_apply_whose_live_calls_were_unavailable_stays_queued(env):
    """Important (review): refused live calls made `_apply_owner` reject the owner's apply for good."""
    store, root, clock, d = env
    pid = _triage_proposal(store, root, clock)
    cid = enqueue(store, "apply", proposal_id=pid)
    dead = _Unavailable()
    assert run(Controller(store, root=root, clock=clock, llm=dead).process_commands()) == 0
    assert dead.calls > 0 and applied(store) == [False]  # still queued, not consumed
    assert store.conn.execute("select status from proposals where id=?", (pid,)).fetchone()[0] == "owner_review"
    assert run(Controller(store, root=root, clock=clock, llm=TriageLLM()).process_commands()) == 1  # model back
    res = json.loads(store.conn.execute("select result from improve_commands where id=?", (cid,)).fetchone()[0])
    assert res["ok"] is True and saved(store)["phase"] == "probation"


def _cluster(store, cause="false_match", n=3):
    from dh.improve import reflect
    reflect.migrate(store)
    for k in range(n):
        store.conn.execute("insert into improve_failures (ref, cause) values (?, ?)", (f"x{k}", cause))
    store.conn.commit()


def test_an_unavailable_advisor_keeps_the_failure_cluster_and_starts_no_cooldown(env):
    from test_improve_controller import FakeLLM
    store, root, clock, d = env
    dead = _Unavailable()
    ctl = Controller(store, root=root, clock=clock, llm=dead)
    run(warm(ctl, clock, 2))
    _cluster(store)
    run(step(ctl, clock, 0.5))
    assert dead.calls >= 1
    s = saved(store)
    assert s["cooldown_until_wh"] <= clock.wh and s["events"][-1]["event"] == "advisor_unavailable"
    unconsumed = store.conn.execute("select count(*) from improve_failures where proposal_id is null").fetchone()[0]
    assert unconsumed == 3
    llm = FakeLLM()  # the model is back: the same trigger reaches the advisor at once
    ctl = Controller(store, root=root, clock=clock, llm=llm)
    run(step(ctl, clock, 0.5))
    assert sum(1 for c in llm.calls if c[0] == "advisor") == 1
