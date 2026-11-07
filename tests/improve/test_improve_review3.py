"""M3 review round 3 (single writer): the reviewer's probes Q1, Q2, Q4, Q5, Q6 (scratchpad/probe27r2.py) as
regression tests, plus the agent lock across processes and the CLI applying queued commands only under it."""
import asyncio
import json
import subprocess
import sys
import textwrap

import pytest

from dh import runtime
from dh.improve import apply_queued_now, cli_maintenance, cli_rollback
from dh.improve import controller as cmod
from dh.improve import playbook as pbm
from dh.improve.controller import Controller, StepAborted, enqueue
from dh.improve.playbook import Item, Playbook
from dh.store import Store
from improve_fix import REPO, alias_fe, seed_playbook
from test_improve_controller import Clock, FakeLLM, health_alarm, step
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


def vstat(store):
    return {r["id"]: r["status"] for r in store.conn.execute("select id, status from playbook_versions")}


def saved(store):
    r = store.conn.execute("select value from improve_state where key='controller'").fetchone()
    return json.loads(r["value"])


CAD = {"op": "add", "item_id": "cadence.hardverapro", "kind": "cadence", "target": "hardverapro", "value": "15"}


async def warm(ctl, clock, n=20):
    for r in range(1, n + 1):
        await step(ctl, clock, 1 / 6, rid=r)


async def accept_olx(ctl, clock):
    await ctl.submit(ctl.champion().make_delta("add", Item("cadence.olx", "cadence", "olx", 15)))
    r = 100
    while ctl.phase == "probation":
        r += 1
        await step(ctl, clock, 2.0, rid=r)
    return r


# ------------------------------------------------------------------ Q1 / Q2: an owner rollback during a round


def test_q1_owner_rollback_during_a_round_is_queued_and_applied_after_it(env):
    store, root, clock, d = env
    out = {}

    class RollbackMidAdvisor(FakeLLM):
        async def chat_json(self, role, system, user, schema, max_tokens=600):
            if role == "advisor":  # the owner types `dh playbook --rollback 1` while the agent thinks
                sb = Store(d / "dh.db")
                out["cli"] = cli_rollback(1, root=root, store=sb, running=False)
                sb.close()
            return await super().chat_json(role, system, user, schema, max_tokens)

    ctl = Controller(store, root=root, clock=clock)
    with runtime.AgentLock(root) as held:  # this process is the agent
        assert held

        async def go():
            await warm(ctl, clock)
            r = await accept_olx(ctl, clock)
            ctl.llm = RollbackMidAdvisor([CAD])
            health_alarm(store)
            await step(ctl, clock, 0.5, rid=r + 1)
            # the round's own decision (a hardverapro cadence, into probation) was committed first; the owner's
            # command, queued meanwhile, was applied by the round's last step, after it: the owner wins
            ev = [e["event"] for e in ctl.s["events"]]
            assert ev.index("promoted", len(ev) - 6) < ev.index("owner_rollback")
            assert store.conn.execute("select applied_at from improve_commands").fetchone()[0] is not None
        run(go())
    assert "queued" in out["cli"][0]
    s = saved(store)
    hv = store.conn.execute("select id from playbook_versions where content like '%cadence.hardverapro%' "
                            "and status != 'promoted' order by id").fetchone()["id"]
    assert s["phase"] == "idle" and vstat(store)[hv] == "aborted"
    assert Playbook.load(store=store) == Playbook.load(1, store=store)
    fb = Playbook.from_file(root)
    assert fb.get("cadence.olx") is None and fb.get("cadence.hardverapro") is None
    assert any(e["event"] == "owner_rollback" for e in s["events"])


def test_q2_a_cli_never_writes_while_the_agent_holds_the_lock(env):
    store, root, clock, d = env
    ctl = Controller(store, root=root, clock=clock)
    run(warm(ctl, clock, 3))
    before = saved(store)
    with runtime.AgentLock(root):
        sb = Store(d / "dh.db")
        text, code = cli_rollback(1, root=root, store=sb, running=False)
        assert "queued" in text and code == 0
        assert apply_queued_now(sb, root) is None
        sb.close()
    assert saved(store) == before  # nothing changed: no stale state can ever be merged over the agent's


def test_q2b_a_state_conflict_aborts_the_step_without_partial_effects(env, monkeypatch):
    store, root, clock, d = env
    a = Controller(store, root=root, clock=clock)
    run(a.process_commands())
    n_versions = len(vstat(store))
    enqueue(store, "rollback", version=1)
    orig = a._force_rollback

    def second_writer_meanwhile(version, wh):  # a bug under the single-writer rule: someone else saves the state
        b = Controller(Store(d / "dh.db"), root=root, clock=clock)
        b.s["consecutive_rejections"] = 2
        b._commit()
        b.store.close()
        return orig(version, wh)
    monkeypatch.setattr(a, "_force_rollback", second_writer_meanwhile)
    assert run(a.process_commands()) == 0  # a's step (a new version + the command mark + its state) is aborted
    monkeypatch.undo()
    assert len(vstat(store)) == n_versions  # rolled back: no partial effect
    assert store.conn.execute("select applied_at from improve_commands").fetchone()[0] is None
    assert store.conn.execute("select count(*) from health where kind='improve_state_conflict'").fetchone()[0] == 1
    assert saved(store)["consecutive_rejections"] == 2  # nothing merged over the other writer
    assert run(a.process_commands()) == 1  # reloaded: the next step applies it
    assert saved(store)["consecutive_rejections"] == 2 and pbm.active_version(store) == n_versions + 1


# ------------------------------------------------------------------ Q4: the owner edits the file mid-round


def test_q4_owner_edit_during_a_promoting_round_is_never_overwritten(env, monkeypatch):
    store, root, clock, d = env
    ctl = Controller(store, root=root, clock=clock)

    async def mp(pb, cfg=None):  # while the round checks the challenger, the owner saves an edit
        fb = Playbook.from_file(root)
        (root / "playbook" / "items.yaml").write_text(fb.apply(fb.make_delta(
            "add", Item("query.arc_b580", "query", "all", "arc b580", provenance="owner"))).to_yaml())
        return True, []

    async def go():
        await warm(ctl, clock)
        await ctl.submit(ctl.champion().make_delta("add", alias_fe()))
        alias_v = ctl.s["challenger"]
        monkeypatch.setattr(cmod.shadow, "decide", lambda store, pid, wh: ("promote", {"forced": True}))
        monkeypatch.setattr(cmod.gates, "must_pass_ok", mp)
        await step(ctl, clock, 1 / 6, rid=21)  # promotes the alias, but must not write over the owner's file
        assert Playbook.from_file(root).get("query.arc_b580") is not None
        monkeypatch.undo()
        await step(ctl, clock, 1 / 6, rid=22)  # reconcile: the owner's edit becomes the baseline
        return alias_v
    alias_v = run(go())
    s = saved(store)
    owner = store.conn.execute("select id from playbook_versions where note like 'owner edit%'").fetchone()["id"]
    assert s["phase"] == "idle" and s["champion"] == owner and vstat(store)[alias_v] == "aborted"
    fb = Playbook.from_file(root)
    assert fb.get("query.arc_b580") is not None and fb.get("alias.fe_glued_4090") is None


# ------------------------------------------------------------------ Q5 / Q6: crashes


def test_q5_crash_between_the_commit_and_the_file_write(env, monkeypatch):
    store, root, clock, d = env
    ctl = Controller(store, root=root, clock=clock)
    run(warm(ctl, clock))

    def crash(self):
        raise KeyboardInterrupt("crash")
    monkeypatch.setattr(Controller, "_sync_file", crash)
    with pytest.raises(KeyboardInterrupt):
        run(ctl.submit(ctl.champion().make_delta("add", Item("cadence.olx", "cadence", "olx", 15))))
    monkeypatch.undo()
    assert Playbook.from_file(root).get("cadence.olx") is None  # committed, not yet written
    fresh = Controller(Store(d / "dh.db"), root=root, clock=clock)
    run(step(fresh, clock, 1 / 6, rid=21))
    s = saved(store)
    assert s["phase"] == "probation" and s["file_pending"] is None
    assert Playbook.from_file(root).get("cadence.olx") is not None
    assert store.conn.execute("select count(*) from playbook_versions where note like 'owner%'").fetchone()[0] == 0


def test_q6_crash_before_the_commit_applies_nothing(env):
    store, root, clock, d = env
    ctl = Controller(store, root=root, clock=clock)
    pb = ctl.champion()
    old = pb.get("prompt.triage")
    run(ctl.submit(pb.make_delta("update", Item(old.id, "prompt", old.target, old.value.replace(
        "Judge only what the listing shows;", "Judge only what this listing shows;"), extra=old.extra))))
    pid = store.conn.execute("select max(id) from proposals").fetchone()[0]
    enqueue(store, "maintenance", on=True)
    enqueue(store, "apply", proposal_id=pid)
    versions_before = vstat(store)
    b = Controller(Store(d / "dh.db"), root=root, clock=clock, llm=TriageLLM())

    def crash():
        raise KeyboardInterrupt("crash")
    b._commit = crash
    with pytest.raises(KeyboardInterrupt):
        run(b.process_commands())
    b.store.close()
    st = Store(d / "dh.db")
    assert [r["applied_at"] for r in st.conn.execute("select applied_at from improve_commands")] == [None, None]
    assert saved(st)["maintenance"] is False and vstat(st) == versions_before
    run(Controller(st, root=root, clock=clock, llm=TriageLLM()).process_commands())
    s = saved(st)
    assert s["maintenance"] is True and s["phase"] == "probation"  # both applied, once
    assert "this listing shows" in Playbook.from_file(root).get("prompt.triage").value


# ------------------------------------------------------------------ the agent lock


HOLDER = textwrap.dedent("""
    import sys, time
    sys.path.insert(0, {repo!r})
    from pathlib import Path
    from dh.runtime import AgentLock
    lock = AgentLock(Path({root!r}))
    print("held" if lock.acquire() else "busy", flush=True)
    sys.stdin.readline()
""")


def _holder(root):
    p = subprocess.Popen([sys.executable, "-c", HOLDER.format(repo=str(REPO), root=str(root))],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "held"
    return p


def test_two_processes_cannot_hold_the_agent_lock(env):
    store, root, clock, d = env
    p = _holder(root)
    try:
        assert runtime.agent_lock_held(root)
        assert runtime.AgentLock(root).acquire() is False
        q = subprocess.run([sys.executable, "-c", HOLDER.format(repo=str(REPO), root=str(root))], input="\n",
                           capture_output=True, text=True, timeout=30)
        assert q.stdout.strip() == "busy"
    finally:
        p.communicate("\n", timeout=30)
    assert not runtime.agent_lock_held(root)  # released when the holder exits
    lock = runtime.AgentLock(root)
    assert lock.acquire()
    lock.release()


def test_cli_applies_queued_commands_only_under_the_lock(env):
    store, root, clock, d = env
    p = _holder(root)  # another process is the agent
    try:
        text, code = cli_maintenance(True, root=root, store=store, running=False)
        assert "queued" in text
        assert store.conn.execute("select applied_at from improve_commands").fetchone()[0] is None
    finally:
        p.communicate("\n", timeout=30)
    text, code = cli_rollback(1, root=root, store=store, running=False)  # no agent: applied under the lock
    assert code == 0 and json.loads(text)["ok"]
    assert [r["applied_at"] is not None for r in store.conn.execute("select applied_at from improve_commands")] == [
        True, True]  # the earlier queued command was applied too, in order
    assert saved(store)["maintenance"] is True and not runtime.agent_lock_held(root)


def test_the_observe_step_is_one_transaction(env):
    """A crash before the observe step's commit leaves no trace of it (round counts, failures, shadow rows)."""
    store, root, clock, d = env
    ctl = Controller(store, root=root, clock=clock)
    run(warm(ctl, clock, 2))
    run(ctl.submit(ctl.champion().make_delta("add", alias_fe())))
    calls = {"n": 0}
    real = ctl._commit

    def crash_second():
        calls["n"] += 1
        if calls["n"] == 2:  # 1 = commands step, 2 = observe step
            raise KeyboardInterrupt("crash")
        return real()
    ctl._commit = crash_second
    with pytest.raises(KeyboardInterrupt):
        run(ctl.on_round({}, champion_n=1, sentinel_n=1, candidates=[1], round_id=99))
    st = Store(d / "dh.db")
    assert st.conn.execute("select count(*) from improve_rounds where round_id=99").fetchone()[0] == 0
    assert st.conn.execute("select count(*) from shadow_results where round_id=99").fetchone()[0] == 0
