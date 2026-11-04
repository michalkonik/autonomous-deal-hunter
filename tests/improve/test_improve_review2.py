"""M3 review round 2: the reviewer's probes P1-P4 (scratchpad probe27.py / probe27b.py) as regression tests, plus
the hand revert (m5) and the live owner-review checks (N4)."""
import asyncio
import json

import pytest

from dh.improve import cli_apply, gates
from dh.improve import playbook as pbm
from dh.improve.controller import Controller, enqueue
from dh.improve.playbook import Item, Playbook
from dh.llm import LLMError
from dh.store import Store
from improve_fix import seed_playbook
from test_improve_controller import Clock, FakeLLM, health_alarm, step


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "repo"
    (root / "playbook").mkdir(parents=True)
    seed_playbook().activate(root)
    store = Store(tmp_path / "dh.db")
    yield store, root, Clock()
    store.close()


def make(env, llm=None):
    store, root, clock = env
    return Controller(store, root=root, llm=llm, clock=clock)


def saved(store):
    r = store.conn.execute("select value, rev from improve_state where key='controller'").fetchone()
    return None if r is None else (r["rev"], r["value"])


class StatusMidRound(FakeLLM):
    """`dh status` in another process while the round waits for the advisor."""

    def __init__(self, env, seen=None):
        super().__init__()
        self.env, self.seen = env, seen if seen is not None else []

    async def chat_json(self, role, system, user, schema, max_tokens=600):
        if role == "advisor":
            store, root, clock = self.env
            before = saved(store)
            Controller(store, root=root, clock=clock).status()
            self.seen.append(before == saved(store))
        return await super().chat_json(role, system, user, schema, max_tokens)


class Boom(FakeLLM):
    async def chat_json(self, role, system, user, schema, max_tokens=600):
        if role == "advisor":
            raise RuntimeError("boom")
        return await super().chat_json(role, system, user, schema, max_tokens)


# ------------------------------------------------------------------ N2: commands and state are one commit


def test_p1_command_applied_with_its_effect_and_status_is_read_only(env):
    store, root, clock = env
    seen = []
    ctl = make(env, StatusMidRound(env, seen))
    enqueue(store, "maintenance", on=True)
    health_alarm(store)
    run(step(ctl, clock, 0.5))
    res = store.conn.execute("select applied_at, result from improve_commands").fetchone()
    fresh = make(env)
    assert res["applied_at"] is not None and json.loads(res["result"])["ok"]
    assert fresh.s["maintenance"] is True and fresh.phase == "maintenance"
    assert seen == [True]  # `dh status` mid-round changed nothing in the saved state


def test_p1b_a_round_that_fails_later_keeps_the_applied_command(env):
    store, root, clock = env
    ctl = make(env, Boom())
    enqueue(store, "maintenance", on=True)
    health_alarm(store)
    with pytest.raises(RuntimeError):
        run(step(ctl, clock, 0.5))
    res = store.conn.execute("select applied_at from improve_commands").fetchone()
    assert res["applied_at"] is not None and make(env).s["maintenance"] is True


def test_construction_and_status_never_write(env):
    store, root, clock = env
    Controller(store, root=root, clock=clock).status()
    assert saved(store) is None and store.conn.execute("select count(*) from playbook_versions").fetchone()[0] == 0
    run(make(env).process_commands())
    before = saved(store)
    (root / "playbook" / "items.yaml").write_text(seed_playbook().apply(
        seed_playbook().make_delta("add", Item("query.x_status", "query", "all", "x status"))).to_yaml())
    make(env).status()  # even with an edited file waiting to be reconciled
    assert saved(store) == before


# ------------------------------------------------------------------ N3: health rise on the delta's own source


def _cadence(env, target, sick):
    store, root, clock = env
    ctl = make(env)

    async def go():
        for r in range(1, 21):
            await step(ctl, clock, 1 / 6, rid=r)
        await ctl.submit(ctl.champion().make_delta("add", Item(f"cadence.{target}", "cadence", target, 15)))
        assert ctl.phase == "probation"
        for r in range(21, 41):
            for _ in range(2):
                store.add_health(sick, "http_429", "429")
            await step(ctl, clock, 1 / 6, rid=r)
            if ctl.phase != "probation":
                break
    run(go())
    return ctl


def test_p2_cadence_on_a_sentinel_source_that_brings_429s_rolls_back(env):
    store, root, clock = env
    ctl = _cadence(env, "kleinanzeigen", "kleinanzeigen")
    ev = [e for e in ctl.s["events"] if e["event"] == "rolled_back"]
    assert ev and "kleinanzeigen|http_429" in ev[-1]["why"][0]["keys"]
    assert Playbook.from_file(root).get("cadence.kleinanzeigen") is None


def test_other_sentinel_sources_are_the_control(env):
    store, root, clock = env
    ctl = _cadence(env, "kleinanzeigen", "hardverapro")  # another sentinel source is sick: the sites, not us
    assert ctl.phase == "probation" and not [e for e in ctl.s["events"] if e["event"] == "rolled_back"]


# ------------------------------------------------------------------ N4: owner apply needs a working live model


class DownLLM:
    def __init__(self):
        self.calls = 0

    async def chat_json(self, *a, **k):
        self.calls += 1
        raise LLMError("unreachable")


class TriageLLM:
    """Extraction: no key. Triage: `scam_verdict` for off-platform scams, `deal_verdict` for the rest."""

    def __init__(self, scam_verdict="scam", deal_verdict="ok"):
        self.scam_verdict, self.deal_verdict, self.systems = scam_verdict, deal_verdict, []

    async def chat_json(self, role, system, user, schema, max_tokens=600):
        self.systems.append(system)
        props = schema.get("properties", {})
        if "verdict" in props:
            scammy = any(w in user.lower() for w in ("whatsapp", "telegram", "600 123", "friends and family", "przelew"))
            return {"p_scam": 0.9 if scammy else 0.05, "verdict": self.scam_verdict if scammy else self.deal_verdict,
                    "reasons": []}
        if "same" in props:
            return {"same": False, "key": None, "certainty": 0.5}
        return {"key": None, "certainty": 0.0, "condition": "used", "defects": [], "box_only": False,
                "for_parts": False, "wanted_ad": False, "bundle_items": []}


def _prompt_proposal(env, item_id="prompt.extract", old_text="A wrong key is far worse than null.",
                     new_text="A wrong key is much worse than null."):
    store, root, clock = env
    ctl = make(env)
    pb = ctl.champion()
    old = pb.get(item_id)
    if item_id == "prompt.triage":
        old_text, new_text = "Judge only what the listing shows;", "Judge only what this listing shows;"
    run(ctl.submit(pb.make_delta("update", Item(old.id, "prompt", old.target, old.value.replace(old_text, new_text),
                                                extra=old.extra))))
    return store.conn.execute("select max(id) from proposals").fetchone()[0]


def test_p3_owner_apply_with_the_model_down_promotes_nothing(env):
    store, root, clock = env
    pid = _prompt_proposal(env)
    down = DownLLM()
    text, code = cli_apply(pid, root=root, store=store, running=False, llm=down)
    assert code != 0 and down.calls > 0 and "unavailable" in text
    row = store.conn.execute("select status, detail from proposals where id=?", (pid,)).fetchone()
    detail = json.loads(row["detail"])
    assert row["status"] == "rejected" and detail["live_must_pass"]["unavailable"] > 0
    assert "much worse" not in Playbook.from_file(root).get("prompt.extract").value


def test_live_must_pass_needs_a_case_that_reaches_the_changed_prompt():
    pb = seed_playbook()
    old = pb.get("prompt.extract")
    ch = pb.apply(pb.make_delta("update", Item(old.id, "prompt", old.target, old.value + " ", extra=old.extra)))
    only_bait = [c for c in gates.load_cases(gates.MUST_PASS) if c["id"] == "mp-box-pl"]
    res = run(gates.must_pass_live(ch, TriageLLM(), "prompt.extract", cases=only_bait))
    assert not res["ok"] and res["reached_changed_prompt"] == 0 and "reached" in " ".join(res["why"])


def test_triage_change_is_exercised_and_its_live_result_is_kept(env):
    store, root, clock = env
    pid = _prompt_proposal(env, "prompt.triage")
    text, code = cli_apply(pid, root=root, store=store, running=False, llm=TriageLLM(scam_verdict="ok"))
    assert code != 0  # the new triage prompt let off-platform scams through
    pid2 = _prompt_proposal(env, "prompt.triage")
    good = TriageLLM()
    text, code = cli_apply(pid2, root=root, store=store, running=False, llm=good)
    assert code == 0, text
    new = Playbook.from_file(root).get("prompt.triage").value
    assert "this listing shows" in new and new in good.systems
    detail = json.loads(store.conn.execute("select detail from proposals where id=?", (pid2,)).fetchone()[0])
    lm = detail["live_must_pass"]
    assert lm["ok"] and lm["reached_changed_prompt"] > 0 and lm["unavailable"] == 0  # kept after "promoted"


# ------------------------------------------------------------------ N1: owner edit during probation


def _probation_then_owner_edit(env, llm):
    store, root, clock = env
    ctl = make(env)

    async def go():
        for r in range(1, 21):
            await step(ctl, clock, 1 / 6, rid=r)
        await ctl.submit(ctl.champion().make_delta("add", Item("cadence.olx", "cadence", "olx", 15)))
    run(go())
    cand = ctl.s["champion"]
    assert ctl.phase == "probation"
    file_pb = Playbook.from_file(root)
    edited = file_pb.apply(file_pb.make_delta("add", Item("query.arc_b580", "query", "all", "arc b580",
                                                          provenance="owner")))
    (root / "playbook" / "items.yaml").write_text(edited.to_yaml())
    ctl.llm = llm
    health_alarm(store)
    try:
        run(step(ctl, clock, 1 / 6, rid=21))
    except RuntimeError:
        pass
    return cand, edited


def test_p4_owner_edit_in_probation_wins_and_the_challenger_never_returns(env):
    store, root, clock = env
    cand, edited = _probation_then_owner_edit(env, StatusMidRound(env))
    fresh = make(env)
    owner = store.conn.execute("select id from playbook_versions where note like 'owner edit%'").fetchone()["id"]
    assert fresh.phase == "idle" and fresh.s["champion"] == owner and fresh.s["probation"] is None
    assert pbm.active_version(store) == owner and Playbook.load(store=store) == edited
    st = {r["id"]: r["status"] for r in store.conn.execute("select id, status from playbook_versions")}
    assert st[cand] == "aborted"

    async def go():  # 429s on olx after the edit: nothing is in probation, nothing rolls back
        for r in range(22, 45):
            for _ in range(2):
                store.add_health("olx", "http_429", "429")
            await step(fresh, clock, 1 / 6, rid=r)
    run(go())
    assert pbm.active_version(store) == owner and Playbook.from_file(root).get("query.arc_b580") is not None
    assert not [e for e in fresh.s["events"] if e["event"] == "rolled_back"]


def test_p4b_owner_edit_detected_by_a_round_that_then_fails(env):
    store, root, clock = env
    cand, edited = _probation_then_owner_edit(env, Boom())
    fresh = make(env)  # a restart
    assert fresh.phase == "idle" and fresh.s["probation"] is None
    assert Playbook.load(fresh.s["champion"], store=store) == edited


# ------------------------------------------------------------------ m5: a hand revert is an owner edit


def test_hand_revert_to_an_older_version_is_an_owner_revert(env):
    store, root, clock = env
    ctl = make(env)

    async def go():
        for r in range(1, 21):
            await step(ctl, clock, 1 / 6, rid=r)
        await ctl.submit(ctl.champion().make_delta("add", Item("cadence.olx", "cadence", "olx", 15)))
        await step(ctl, clock, 1 / 6, rid=21)  # the promotion's file write is long done
    run(go())
    base = store.conn.execute("select id from playbook_versions order by id").fetchone()["id"]
    Playbook.load(base, store=store).activate(root)  # the owner copies the old file back by hand
    run(step(ctl, clock, 1 / 6, rid=22))
    v = store.conn.execute("select id, status, note from playbook_versions order by id desc").fetchone()
    assert v["status"] == "baseline" and v["note"].startswith(f"owner revert to v{base}")
    assert ctl.phase == "idle" and Playbook.from_file(root).get("cadence.olx") is None
