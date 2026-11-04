"""Task 30: the controller state machine, stop rules, reflection/proposals and the weekly report.

Acceptance tests here: 2 (a good delta goes through shadow and is promoted), 3 and 4 end to end (market shift vs
breakage during probation), 5 (cooldown doubles), 6 (maintenance after 3 rejections), 7 (one change in flight).
"""
import asyncio
import json
from datetime import timedelta

import pytest

from dh.improve import Controller, InFlight, cli_eval, cli_playbook_show, cli_rollback, cli_status, gates
from dh.improve.controller import StateConflict, enqueue
from dh.improve import playbook as pbm
from dh.improve import reflect, report, shadow
from dh.improve.playbook import Item, Playbook
from dh.store import Store
from improve_fix import NOW, alias_fe, archive, listing, seed_playbook


def run(coro):
    return asyncio.run(coro)


class Clock:
    def __init__(self, wh=0.0):
        self.wh = wh

    def __call__(self):
        return self.wh


class FakeLLM:
    """Scripted senior/advisor replies; records every call (role, system, user)."""

    def __init__(self, proposals=()):
        self.proposals = list(proposals)
        self.calls = []

    async def chat_json(self, role, system, user, schema, max_tokens=600):
        self.calls.append((role, system, user))
        if role == "advisor":
            out = self.proposals.pop(0) if self.proposals else {"op": "none"}
            return {"item_id": "", "kind": "alias", "target": "match", "value": "", "hypothesis": "h",
                    "evidence": [], "predicted_effect": "e", **out}
        refs = [line.split(" ")[0] for line in user.splitlines() if line.startswith(("mark:", "health:", "round:"))]
        return {"failures": [{"ref": r, "cause": "false_match"} for r in refs], "summary": "classified"}


BAD = [  # each one fails a gate, and none repeats a rejected idea
    {"op": "remove", "item_id": "scam_kw.box_sell", "kind": "scam_kw", "target": "risk", "value": "sprzedam pudelko po"},
    {"op": "add", "item_id": "alias.xtx_typo", "kind": "alias", "target": "match", "value": "7900xtx => 7900 xt"},
    {"op": "add", "item_id": "scam_kw.never_seen", "kind": "scam_kw", "target": "risk", "value": "zzz qqq"},
    {"op": "add", "item_id": "alias.r3090_r3080", "kind": "alias", "target": "match", "value": "rtx 3090 => rtx 3080"},
    {"op": "add", "item_id": "scam_kw.never_seen_2", "kind": "scam_kw", "target": "risk", "value": "yyy www"},
    {"op": "add", "item_id": "scam_kw.never_seen_3", "kind": "scam_kw", "target": "risk", "value": "vvv uuu"},
]


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "repo"
    (root / "playbook").mkdir(parents=True)
    seed_playbook().activate(root)
    store = Store(tmp_path / "dh.db")
    clock = Clock()
    yield store, root, clock
    store.close()


def make(env, llm=None):
    store, root, clock = env
    return Controller(store, root=root, llm=llm, clock=clock)


def health_alarm(store, n=3):
    for _ in range(n):
        store.add_health("olx", "blocked", "403")


async def step(ctl, clock, dwh=1.0, c=1, s=1, candidates=None, rid=None):
    clock.wh += dwh
    return await ctl.on_round({}, champion_n=c, sentinel_n=s, candidates=candidates or [], round_id=rid)


# ------------------------------------------------------------------ acceptance 5: cooldown doubles


def test_cooldown_doubles(env):
    store, root, clock = env
    ctl = make(env, FakeLLM(BAD))
    seen = []

    async def go():
        for _ in range(5):
            health_alarm(store)
            await step(ctl, clock, 0.5)  # the alarm triggers a proposal, the gates reject it
            ev = [e for e in ctl.s["events"] if e["event"] == "rejected"][-1]
            seen.append(ev["cooldown_wh"])
            left = ctl.s["cooldown_until_wh"] - clock.wh
            assert left == pytest.approx(ev["cooldown_wh"])
            # inside the cooldown a new alarm does not start a proposal
            health_alarm(store)
            n = store.conn.execute("select count(*) from proposals").fetchone()[0]
            await step(ctl, clock, left / 2)
            assert store.conn.execute("select count(*) from proposals").fetchone()[0] == n
            assert ctl.s["events"][-1]["event"] == "trigger_in_cooldown"
            clock.wh += left / 2 + 0.01  # the cooldown is over
    run(go())
    assert seen == [8.0, 16.0, 32.0, 64.0, 64.0]


def test_accepted_change_resets_the_backoff(env):
    store, root, clock = env
    ctl = make(env)
    ctl.s.update(consecutive_rejections=2, cooldown_until_wh=5.0, proposal_id=None, phase="probation",
                 probation={"started_wh": 0.0})
    ctl._accept(clock.wh)
    assert ctl.s["consecutive_rejections"] == 0 and ctl.phase == "idle"


# ------------------------------------------------------------------ acceptance 6: maintenance after 3 rejections


def test_maintenance_after_three_rejections(env):
    store, root, clock = env
    ctl = make(env, FakeLLM(BAD))

    async def go():
        for i in range(3):
            health_alarm(store)
            await step(ctl, clock, 0.5)
            clock.wh += 70  # past any cooldown
            assert ctl.s["consecutive_rejections"] == i + 1
        assert ctl.phase == "maintenance" and ctl.s["maintenance"]
        assert "3 consecutive rejections" in ctl.s["maintenance_reason"]
        # in maintenance a cluster of same-cause failures does NOT reopen the playbook ...
        ctl.s["last_review_wh"] = clock.wh  # (the weekly review is not due)
        ctl._commit()
        reflect.migrate(store)
        for k in range(3):
            store.conn.execute("insert into improve_failures (ref, cause) values (?, 'false_match')", (f"x{k}",))
        store.conn.commit()
        n = store.conn.execute("select count(*) from proposals").fetchone()[0]
        await step(ctl, clock, 1)
        assert store.conn.execute("select count(*) from proposals").fetchone()[0] == n
        # ... an alarm does (one targeted change), and a rejection there leaves it in maintenance
        health_alarm(store)
        await step(ctl, clock, 1)
        assert store.conn.execute("select count(*) from proposals").fetchone()[0] == n + 1
        assert ctl.phase == "maintenance"
        # the weekly report says so
        assert report.MAINTENANCE_LINE in await report.weekly_async(store, NOW)
        enqueue(store, "maintenance", on=False)
        await ctl.process_commands()
        assert ctl.phase == "idle" and not ctl.s["maintenance"]
    run(go())


def test_maintenance_when_two_weeks_cannot_detect_a_plausible_gain(env):
    store, root, clock = env
    ctl = make(env)

    async def go():
        for i in range(30):  # 2+ weeks of watched hours with only 3 deals: nothing a playbook item does is detectable
            await step(ctl, clock, 12.0, c=1 if i % 10 == 0 else 0, s=0, rid=i + 1)
    run(go())
    assert ctl.s["maintenance"] and "can only detect" in ctl.s["maintenance_reason"]


# ------------------------------------------------------------------ acceptance 7: one change in flight


def test_only_one_change_in_flight(env):
    store, root, clock = env
    ctl = make(env, FakeLLM([BAD[2]]))
    pb = ctl.champion()

    async def go():
        await ctl.submit(pb.make_delta("add", alias_fe(), hypothesis="glued FE"))
        assert ctl.phase == "shadow"
        with pytest.raises(InFlight):
            await ctl.submit(pb.make_delta("add", Item("alias.other", "alias", "match", "3090fe => 3090 fe")))
        n = store.conn.execute("select count(*) from proposals").fetchone()[0]
        health_alarm(store)
        clock.wh += 200  # even the weekly review is due
        await ctl.on_round({}, champion_n=1, sentinel_n=1, candidates=[])
        assert store.conn.execute("select count(*) from proposals").fetchone()[0] == n  # no second proposal
        assert ctl.phase == "shadow"
    run(go())


# ------------------------------------------------------------------ acceptance 1 and 2 through the controller


def test_bad_delta_is_rejected_by_the_controller(env):
    store, root, clock = env
    ctl = make(env)
    pb = ctl.champion()
    before = (root / "playbook" / "items.yaml").read_text()
    run(ctl.submit(pb.make_delta("remove", pb.get("scam_kw.box_sell"), hypothesis="fewer flags")))
    p = store.conn.execute("select status, stage from proposals").fetchone()
    assert (p["status"], p["stage"]) == ("rejected", "must_pass") and ctl.phase == "idle"
    assert (root / "playbook" / "items.yaml").read_text() == before  # nothing changed on disk


def _shadow_to_promotion(env, ctl):
    """Drive a judgment delta through 40+ shadow rounds with owner-labelled disagreements."""
    store, root, clock = env
    ids = archive(store)

    async def go():
        await ctl.submit(ctl.champion().make_delta("add", alias_fe(), hypothesis="glued FE suffix hides the chip",
                                                   predicted_effect="+1 key per 4090FE listing"))
        assert ctl.phase == "shadow"
        for r in range(1, 61):
            cands = list(ids.values())
            if r <= 12:  # a new glued-FE listing in some rounds: the challenger keys it, the champion does not
                l = listing(f"fe{r}", "RTX 4090FE 24GB używana", "6200", seen=NOW - timedelta(hours=1))
                cands.append(store.upsert_listing(l))
            cands += [store.upsert_listing(listing(f"n{r}-{k}", "RTX 3090 Zotac Trinity 24GB używana", "3150",
                                                   seen=NOW - timedelta(hours=1))) for k in range(2)]
            await step(ctl, clock, 1 / 6, c=1, s=1, candidates=cands, rid=r)
            for d in shadow.pending(store, ctl.s["proposal_id"]):  # the owner adjudicates from the digest
                shadow.label(store, d.id, "key:gpu:rtx-4090:24")
            if ctl.phase != "shadow":
                return r
        return None
    return run(go())


def test_good_delta_goes_through_shadow_and_is_promoted(env):
    """Acceptance 2: an alias that fixes a golden miss passes the gates, wins shadow, is promoted, passes probation."""
    store, root, clock = env
    ctl = make(env)
    r = _shadow_to_promotion(env, ctl)
    assert r == gates.SHADOW_MIN_ROUNDS and ctl.phase == "probation"
    promoted = [e for e in ctl.s["events"] if e["event"] == "promoted"][-1]
    assert Playbook.from_file(root).get("alias.fe_glued_4090") is not None  # active in the pipeline's file
    assert pbm.active_version(store) == promoted["version"]

    async def probation():
        while ctl.phase == "probation":
            await step(ctl, clock, 1 / 6, c=1, s=1)
    run(probation())
    assert ctl.phase == "idle" and ctl.s["consecutive_rejections"] == 0
    p = store.conn.execute("select status, stage from proposals order by id desc").fetchone()
    assert (p["status"], p["stage"]) == ("accepted", "probation_passed")


def _promote_then(env, c_after, s_after, rounds=400):
    store, root, clock = env
    ctl = make(env)
    _shadow_to_promotion(env, ctl)
    assert ctl.phase == "probation"
    v_new, v_old = ctl.s["champion"], ctl.s["previous"]
    import random

    from test_improve_shadow import poisson
    rng = random.Random(5)

    async def go():
        for _ in range(rounds):
            if ctl.phase != "probation":
                break
            await step(ctl, clock, 1 / 6, c=poisson(rng, c_after), s=poisson(rng, s_after))
    run(go())
    return ctl, v_new, v_old


def test_market_shift_no_rollback_end_to_end(env):
    """Acceptance 3 through the controller: both the sentinel and the champion drop -> no rollback."""
    store, root, clock = env
    ctl, v_new, v_old = _promote_then(env, 0.3, 0.3)
    assert not [e for e in ctl.s["events"] if e["event"] == "rolled_back"]
    assert pbm.active_version(store) == v_new and ctl.phase == "idle"


def test_breakage_rollback_end_to_end(env):
    """Acceptance 4 through the controller: only the champion drops -> rollback to the previous version."""
    store, root, clock = env
    ctl, v_new, v_old = _promote_then(env, 0.2, 1.0)
    ev = [e for e in ctl.s["events"] if e["event"] == "rolled_back"]
    assert ev and ev[-1]["failed"] == v_new
    assert pbm.active_version(store) == v_old
    assert Playbook.from_file(root).get("alias.fe_glued_4090") is None  # the file is back to the previous version
    p = store.conn.execute("select status from proposals order by id desc").fetchone()
    assert p["status"] == "failed_in_prod" and ctl.s["consecutive_rejections"] == 1
    # the failed idea is remembered: the reflector sees it and may not re-propose it for 30 days
    rej = reflect.rejected_ideas(store)
    assert rej and rej[-1]["item_id"] == "alias.fe_glued_4090"
    d = ctl.champion().make_delta("add", alias_fe())
    assert reflect.is_repeat(d, rej)


# ------------------------------------------------------------------ reflection and proposals


def test_round_report_classifies_in_a_fresh_context(env):
    store, root, clock = env
    pb = seed_playbook()
    llm = FakeLLM()
    failures = [{"ref": "health:1", "kind": "block", "detail": "olx 403", "cause": "block"},
                {"ref": "mark:7", "kind": "owner_skip", "detail": "tier 2: to tylko pudełko", "cause": None}]
    rep = run(reflect.round_report({"round_id": 3}, failures, llm=llm, playbook=pb))
    assert {f["ref"]: f["cause"] for f in rep["failures"]} == {"health:1": "block", "mark:7": "false_match"}
    role, system, user = llm.calls[0]
    assert role == "senior" and system == pb.get("prompt.reflect").value  # the sentinel reviewer prompt
    assert "<listing>" in user and "health:1" not in user  # only the open item, wrapped as data
    rep2 = run(reflect.round_report({}, failures[1:], llm=None, playbook=pb))
    assert rep2["failures"][0]["cause"] == "false_match" and rep2["failures"][0]["source"] == "fallback"


def test_proposal_is_validated_by_code():
    pb = seed_playbook()
    ok = reflect.build_delta({"op": "add", "item_id": "alias.fe_glued_4090", "kind": "alias", "target": "match",
                              "value": "4090fe => 4090 fe", "hypothesis": "h", "evidence": ["mark:1"],
                              "predicted_effect": "e"}, pb, "failures:false_match")
    assert ok is not None and ok.inverse.op == "remove" and ok.item.provenance == "proposal:failures:false_match"
    for bad in ({"op": "remove", "item_id": "sentinel.rtx_4090", "kind": "query", "target": "x", "value": ""},
                {"op": "update", "item_id": "prompt.confirm", "kind": "prompt", "target": "llm.confirm", "value": "x"},
                {"op": "update", "item_id": "prompt.reflect", "kind": "prompt", "target": "llm.extract",
                 "value": "untrusted <listing>"},
                {"op": "add", "item_id": "alias.bad", "kind": "alias", "target": "match", "value": "no arrow"},
                {"op": "none", "item_id": "", "kind": "alias", "target": "", "value": ""}):
        assert reflect.build_delta({"hypothesis": "", "evidence": [], "predicted_effect": "", **bad}, pb, "t") is None


def test_propose_uses_the_sentinel_prompt_and_never_sees_eval_labels(env):
    store, root, clock = env
    llm = FakeLLM([{"op": "add", "item_id": "alias.fe_glued_4090", "kind": "alias", "target": "match",
                    "value": "4090fe => 4090 fe"}])
    pb = seed_playbook()
    d = run(reflect.propose("weekly", [{"ref": "mark:1", "cause": "false_match", "detail": "RTX 4090FE missed"}],
                            pb, llm=llm, rejected=[]))
    assert d is not None and d.item.id == "alias.fe_glued_4090"
    role, system, user = llm.calls[0]
    assert role == "advisor" and system == pb.get("prompt.propose").value
    for leak in ("must_pass", "golden", "mp-", "g-miss", "expect"):
        assert leak not in user
    assert "sentinel.rtx_4090" not in user  # sentinel items are not offered for editing


# ------------------------------------------------------------------ owner commands, persistence, report


def test_state_survives_a_restart_and_owner_rollback(env):
    store, root, clock = env
    ctl = make(env)
    run(ctl.submit(ctl.champion().make_delta("add", alias_fe())))
    assert ctl.phase == "shadow"
    again = make(env)
    assert again.phase == "shadow" and again.s["proposal_id"] == ctl.s["proposal_id"]
    base = again.s["champion"]
    text, code = cli_rollback(base, root=root, store=store, running=False)
    assert code == 0 and make(env).phase == "idle"
    assert Playbook.from_file(root) == Playbook.load(base, store=store)
    out, _ = cli_status(root=root, store=store)
    assert json.loads(out)["phase"] == "idle"
    show, _ = cli_playbook_show(root=root, store=store)
    assert "sentinel.rtx_4090" in show and "v1" in show


def test_cli_eval_reports_must_pass_and_golden(env):
    store, root, clock = env
    text, code = cli_eval(root=root, store=store)
    assert code == 0 and "must-pass: 39/39" in text and "golden:" in text


def test_weekly_report_scoreboard(env):
    store, root, clock = env
    archive(store)
    ctl = make(env)
    run(ctl.on_round({}, champion_n=1, sentinel_n=1))
    md = run(report.weekly_async(store, NOW))
    for part in ("Scoreboard", "| Tier |", "Precision on owner-marked deals", "False tier-1", "Missed deals",
                 "fetches", "LLM tokens", "kWh", "realised net", "Adapter health", "Self-improvement",
                 "Prediction ledger"):
        assert part in md, part
    assert report.MAINTENANCE_LINE not in md
