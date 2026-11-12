"""M3 review round 1: prompt changes are owner-review-only, crash-safe promotion, queued owner commands with a
compare-and-swap state, human edits of items.yaml, health rollback in probation, no-proposal cooldown, weekly review
kept through a cooldown, health consumed per state, MDE on deals, frozen evals while running, the weekly report."""
import asyncio
import json
from datetime import timedelta
from decimal import Decimal

import pytest

from dh.improve import cli_apply, cli_pending, gates, report
from dh.improve import playbook as pbm
from dh.improve.controller import Controller, StateConflict, enqueue
from dh.improve.playbook import Item, Playbook
from dh.store import Store
from improve_fix import NOW, alias_fe, archive, seed_playbook
from test_improve_controller import Clock, FakeLLM, _shadow_to_promotion, health_alarm, step


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


def started(env, llm=None):
    """A controller whose state exists (construction is read-only; the first command/round initialises it)."""
    ctl = make(env, llm)
    run(ctl.process_commands())
    return ctl


class LiveLLM:
    """A live extraction model stand-in: 'safe' answers 'no key' for everything, 'wrong' maps everything to a 4090."""

    def __init__(self, mode="safe"):
        self.mode, self.calls = mode, 0

    async def chat_json(self, role, system, user, schema, max_tokens=600):
        self.calls += 1
        props = schema.get("properties", {})
        if "same" in props:
            return {"same": self.mode == "wrong", "key": None, "certainty": 0.9}
        key = "gpu:rtx-4090:24" if self.mode == "wrong" else None
        return {"key": key, "certainty": 0.8 if key else 0.0, "condition": "used", "defects": [], "box_only": False,
                "for_parts": False, "wanted_ad": False, "bundle_items": []}


def prompt_delta(pb):
    old = pb.get("prompt.extract")
    return pb.make_delta("update", Item(old.id, "prompt", old.target,
                                        old.value.replace("A wrong key is far worse than null.",
                                                          "A wrong key is much worse than null."),
                                        extra=old.extra), hypothesis="clearer", predicted_effect="fewer wrong keys")


# ------------------------------------------------------------------ ruling 5: prompt changes are owner-review-only


def test_prompt_change_stops_at_owner_review(env):
    store, root, clock = env
    ctl = make(env)
    before = (root / "playbook" / "items.yaml").read_text()
    run(ctl.submit(prompt_delta(ctl.champion())))
    p = store.conn.execute("select id, status, stage from proposals").fetchone()
    assert (p["status"], p["stage"]) == ("owner_review", "owner_review")
    assert ctl.phase == "idle" and ctl.s["consecutive_rejections"] == 0  # not a rejection, not in flight
    assert (root / "playbook" / "items.yaml").read_text() == before  # never promoted by the loop
    text, _ = cli_pending(root=root, store=store)
    assert f"proposal {p['id']} (prompt, owner review)" in text and "much worse than null" in text


def test_prompts_are_only_updated(env):
    store, root, clock = env
    pb = seed_playbook()
    add = pb.apply(pb.make_delta("add", Item("prompt.extra", "prompt", "llm.triage", "untrusted <listing> data")))
    res = run(gates.evaluate(pb, add, store, now=NOW))
    assert not res.passed and res.stage == "schema" and "only updated" in " ".join(res.detail["errors"])
    res = run(gates.evaluate(pb, pb.apply(prompt_delta(pb)), store, now=NOW))
    assert res.passed and res.stage == "owner_review"


def test_owner_apply_runs_must_pass_with_the_live_model(env):
    store, root, clock = env
    ctl = make(env)
    run(ctl.submit(prompt_delta(ctl.champion())))
    pid = store.conn.execute("select id from proposals").fetchone()[0]
    wrong = LiveLLM("wrong")
    text, code = cli_apply(pid, root=root, store=store, running=False, llm=wrong)
    assert code == 1 and "must-pass cases failed" in text and wrong.calls > 0
    assert store.conn.execute("select status from proposals where id=?", (pid,)).fetchone()[0] == "rejected"
    run(make(env).submit(prompt_delta(make(env).champion())))
    pid2 = store.conn.execute("select max(id) from proposals").fetchone()[0]
    safe = LiveLLM("safe")
    text, code = cli_apply(pid2, root=root, store=store, running=False, llm=safe)
    assert code == 0 and safe.calls > 0, text
    ctl = make(env)
    assert ctl.phase == "probation"  # an owner-applied prompt is still watched and can still roll back
    assert "much worse than null" in Playbook.from_file(root).get("prompt.extract").value


def test_queued_apply_waits_for_the_running_agent(env):
    store, root, clock = env
    ctl = make(env, llm=LiveLLM("safe"))
    run(ctl.submit(prompt_delta(ctl.champion())))
    pid = store.conn.execute("select id from proposals").fetchone()[0]
    text, code = cli_apply(pid, root=root, store=store, running=True, llm=LiveLLM("safe"))
    assert "queued" in text and ctl.phase == "idle"
    events = run(step(ctl, clock, 0.5))
    assert any(e["event"] == "promoted" for e in events) and ctl.phase == "probation"


# ------------------------------------------------------------------ atomic promotion, reconciliation, restarts


def test_crash_between_promote_and_save_is_reconciled(env):
    """(Legacy) a promotion committed without its state: the next round watches it in probation."""
    store, root, clock = env
    ctl = make(env)
    run(ctl.submit(ctl.champion().make_delta("add", alias_fe())))
    vid = ctl.s["challenger"]
    pbm.set_status(store, vid, "promoted")  # committed... then the process died: no state save, no file write
    assert Playbook.from_file(root).get("alias.fe_glued_4090") is None
    again = make(env)
    assert again.phase == "shadow"  # construction is read-only
    run(step(again, clock, 0.1))
    assert again.phase == "probation" and again.s["champion"] == vid  # never runs a version unprobated
    assert Playbook.from_file(root).get("alias.fe_glued_4090") is not None  # file brought in line
    kinds = {r["kind"] for r in store.conn.execute("select kind from health where source='improve'")}
    assert "state_mismatch" in kinds


def test_crash_between_commit_and_file_write_rewrites_the_file(env):
    store, root, clock = env
    ctl = started(env)
    base = ctl.champion()
    run(ctl.submit(ctl.champion().make_delta("add", Item("cadence.olx", "cadence", "olx", 15))))
    assert ctl.phase == "probation"
    # simulate the crash window: the commit happened, the file write did not, file_pending still recorded
    base.activate(root)
    ctl.s["file_pending"] = {"from": base.sha(), "to": ctl.champion().sha()}
    store.conn.execute("update improve_state set value=?, rev=rev+1 where key='controller'", (json.dumps(ctl.s),))
    store.conn.commit()
    again = make(env)
    run(step(again, clock, 0.1))
    assert Playbook.from_file(root).get("cadence.olx") is not None and again.phase == "probation"
    assert not [e for e in again.s["events"] if e["event"] == "owner_edit"]


def test_restart_mid_probation_continues_it(env):
    store, root, clock = env
    ctl = make(env)
    _shadow_to_promotion(env, ctl)
    assert ctl.phase == "probation"
    for _ in range(5):
        run(step(ctl, clock, 1 / 6))
    again = make(env)  # a restart
    assert again.phase == "probation" and again.s["probation"]["rounds_after"] == 5

    async def go():
        while again.phase == "probation":
            await step(again, clock, 1 / 6)
    run(go())
    assert again.phase == "idle" and store.conn.execute(
        "select status from proposals order by id desc").fetchone()[0] == "accepted"


def test_owner_edit_of_the_file_becomes_a_baseline_and_aborts_the_change(env):
    store, root, clock = env
    ctl = make(env)
    run(ctl.submit(ctl.champion().make_delta("add", alias_fe())))
    assert ctl.phase == "shadow"
    edited = seed_playbook().apply(Delta_add_query())
    (root / "playbook" / "items.yaml").write_text(edited.to_yaml())  # the owner, by hand
    events = run(step(ctl, clock, 0.5))
    assert any(e["event"] == "owner_edit" for e in events) and ctl.phase == "idle"
    v = store.conn.execute("select id, status, note from playbook_versions order by id desc").fetchone()
    assert v["status"] == "baseline" and "owner edit" in v["note"] and ctl.s["champion"] == v["id"]
    assert store.conn.execute("select status from proposals").fetchone()[0] == "aborted"
    assert Playbook.load(store=store) == edited
    assert store.conn.execute("select count(*) from health where kind='owner_edit'").fetchone()[0] == 1


def Delta_add_query():
    return seed_playbook().make_delta("add", Item("query.arc_b580", "query", "all", "arc b580", provenance="owner"))


# ------------------------------------------------------------------ owner command queue and compare-and-swap


def test_owner_rollback_during_a_long_round_is_applied_before_saving(env):
    store, root, clock = env
    base = started(env).s["champion"]

    class SlowAdvisor(FakeLLM):
        async def chat_json(self, role, system, user, schema, max_tokens=600):
            if role == "advisor":  # the owner types `dh playbook --rollback` while the round is thinking
                enqueue(store, "rollback", version=base)
                await asyncio.sleep(0.01)
            return await super().chat_json(role, system, user, schema, max_tokens)

    ctl = make(env, SlowAdvisor())
    health_alarm(store)
    events = run(step(ctl, clock, 0.5))
    assert any(e["event"] == "owner_rollback" for e in events)
    assert ctl.s["champion"] != base and Playbook.load(store=store) == Playbook.load(base, store=store)
    assert store.conn.execute("select applied_at from improve_commands").fetchone()[0] is not None
    assert make(env).s["champion"] == ctl.s["champion"]  # the state saved by the round includes the rollback


def test_state_conflict_is_an_assertion_not_a_merge(env):
    """Single writer (fix round 3): a CAS conflict aborts the step; nothing is merged (see test_improve_review3)."""
    from dh.improve.controller import StepAborted
    store, root, clock = env
    a = started(env)
    b = make(env)
    b.s["consecutive_rejections"] = 2
    b._commit()
    a.s["maintenance"] = True
    with pytest.raises(StepAborted):
        a._commit()
    fresh = make(env)
    assert fresh.s["consecutive_rejections"] == 2 and fresh.s["maintenance"] is False


# ------------------------------------------------------------------ health rollback during probation


def _cadence_probation(env, source):
    store, root, clock = env
    ctl = make(env)

    async def go():
        for r in range(1, 21):  # pre-promotion rounds: the health baseline (quiet)
            await step(ctl, clock, 1 / 6, rid=r)
        await ctl.submit(ctl.champion().make_delta("add", Item("cadence.olx", "cadence", "olx", 15)))
        assert ctl.phase == "probation"  # arm door: straight to probation
        for r in range(21, 41):
            store.add_health(source, "http_429", "429 Too Many Requests")
            store.add_health(source, "http_429", "429 Too Many Requests")
            await step(ctl, clock, 1 / 6, rid=r)
            if ctl.phase != "probation":
                break
    run(go())
    return ctl


def test_health_rise_rolls_back_a_cadence_change(env):
    store, root, clock = env
    ctl = _cadence_probation(env, "olx")
    ev = [e for e in ctl.s["events"] if e["event"] == "rolled_back"]
    assert ev and ev[-1]["why"][0]["event"] == "health_rise" and "olx|http_429" in ev[-1]["why"][0]["keys"]
    assert Playbook.from_file(root).get("cadence.olx") is None


def test_health_rise_on_the_sentinel_sources_is_not_the_playbook(env):
    store, root, clock = env
    ctl = _cadence_probation(env, "kleinanzeigen")  # a sentinel source is unhealthy too: the sites changed
    assert ctl.phase == "probation" and not [e for e in ctl.s["events"] if e["event"] == "rolled_back"]


def test_health_rise_keys_are_matched_by_domain_too(env):
    """A sentinel source that reports under its HOST must be as invisible to probation as one named by adapter.
    (2026-11-01, live: proposal 1 changed only sprzedajemy and was rolled back by `allegrolokalnie.pl|blocked`,
    because "allegro_lokalnie" is not a substring of "allegrolokalnie.pl".)"""
    ctl = _cadence_probation(env, "allegrolokalnie.pl")  # the same unhealthy control source, host spelling
    assert ctl.phase == "probation" and not [e for e in ctl.s["events"] if e["event"] == "rolled_back"]


def test_source_matches_maps_adapter_names_to_their_domains(env):
    ctl = make(env)
    ids = ctl._source_identities()
    assert "allegrolokalnie.pl" in ids["allegro_lokalnie"] and "www.kleinanzeigen.de" in ids["kleinanzeigen"]
    control = {"allegro_lokalnie", "hardverapro", "kleinanzeigen"}
    assert Controller._source_matches("allegrolokalnie.pl|blocked", control, ids)
    assert Controller._source_matches("www.kleinanzeigen.de|robots_disallow", control, ids)
    assert not Controller._source_matches("images.marktplaats.com|robots_error", control, ids)
    assert not Controller._source_matches("sprzedajemy.pl|blocked", control, ids)
    assert not Controller._source_matches("imap|poll_failed", control, ids)


# ------------------------------------------------------------------ cooldowns, triggers, health per state, MDE


def _failure_cluster(store, n=3, cause="false_match"):
    from dh.improve import reflect
    reflect.migrate(store)
    for k in range(n):
        store.conn.execute("insert into improve_failures (ref, cause) values (?, ?)", (f"x{k}", cause))
    store.conn.commit()


def advisor_calls(llm):
    return sum(1 for c in llm.calls if c[0] == "advisor")


def test_no_proposal_stamps_the_cluster_and_cools_down(env):
    store, root, clock = env
    llm = FakeLLM()  # always answers op "none"
    ctl = make(env, llm)
    _failure_cluster(store)
    run(step(ctl, clock, 0.5))
    assert advisor_calls(llm) == 1 and ctl.s["cooldown_until_wh"] == pytest.approx(clock.wh + 8)
    for _ in range(10):
        run(step(ctl, clock, 0.5))
    clock.wh += 10
    run(step(ctl, clock, 0.5))
    assert advisor_calls(llm) == 1  # not every round, and not for the same (stamped) cluster after the cooldown
    assert ctl.s["consecutive_rejections"] == 0


def test_weekly_review_due_in_a_cooldown_is_not_lost(env):
    store, root, clock = env
    llm = FakeLLM()
    ctl = started(env, llm)
    ctl.s.update(cooldown_until_wh=200.0, last_review_wh=0.0)
    ctl._commit()
    clock.wh = 170
    run(step(ctl, clock, 0))
    assert advisor_calls(llm) == 0
    clock.wh = 201
    run(step(ctl, clock, 0))
    assert advisor_calls(llm) == 1 and '"trigger": "weekly"' in llm.calls[-1][2]


def test_health_events_are_consumed_per_state(env):
    store, root, clock = env
    llm = FakeLLM()
    ctl = started(env, llm)
    base = ctl.s["champion"]
    run(ctl.submit(ctl.champion().make_delta("add", alias_fe())))
    health_alarm(store, 5)  # during shadow
    run(step(ctl, clock, 0.5))
    enqueue(store, "rollback", version=base)
    run(ctl.process_commands())
    assert ctl.phase == "idle"
    run(step(ctl, clock, 0.5))
    assert advisor_calls(llm) == 0  # shadow-time events do not trigger a reflection afterwards
    health_alarm(store)
    clock.wh += 70
    run(step(ctl, clock, 0.5))
    assert advisor_calls(llm) == 1  # new events in idle do


def test_mde_counts_deals_not_near_misses(env):
    store, root, clock = env
    ctl = make(env)

    async def go(deals):
        for i in range(30):
            clock.wh += 12
            await ctl.on_round({}, champion_n=20, sentinel_n=5, deals_n=deals, round_id=1000 + i + deals * 100)
    run(go(0))  # plenty of near-misses, no deals: nothing a playbook item does is detectable
    assert ctl.s["maintenance"] and "deals, tier 1-3" in ctl.s["maintenance_reason"]


def test_mde_with_enough_deals_keeps_improving(env):
    store, root, clock = env
    ctl = make(env)

    async def go():
        for i in range(30):
            clock.wh += 12
            await ctl.on_round({}, champion_n=60, sentinel_n=5, deals_n=40, round_id=i + 1)
    run(go())
    assert not ctl.s["maintenance"]


# ------------------------------------------------------------------ frozen evals while running; weekly report


def test_add_from_marks_refused_while_the_agent_runs(env, tmp_path):
    from evals.marks import AgentRunning, add_from_marks
    store, root, clock = env
    archive(store)
    with pytest.raises(AgentRunning):
        add_from_marks(store, tmp_path / "g.jsonl", running=True)
    assert add_from_marks(store, tmp_path / "g.jsonl", running=False) == 2
    assert add_from_marks(store, tmp_path / "g.jsonl", running=False) == 0  # append-only, once per mark


def test_weekly_report_scoreboard_numbers(env):
    store, root, clock = env
    ids = archive(store)  # two tier-2 deals: one bought, one skipped as a scam
    deal_id = store.conn.execute("select id from deals where listing_ref=?", (ids["deal"],)).fetchone()[0]
    store.add_mark(deal_id, "sold", Decimal("3200"), "", "web")  # a second mark on the same deal
    md = run(report.weekly_async(store, NOW))
    assert "| 2 | 2 | 250.00 | 200.00 |" in md
    assert "Precision on owner-marked deals: 50% of 2 marked deals" in md  # per deal, not per mark
    assert "False tier-1 deals: 0" in md and report.MAINTENANCE_LINE not in md
    ctl = make(env)
    enqueue(store, "maintenance", on=True)
    run(ctl.process_commands())
    assert report.MAINTENANCE_LINE in run(report.weekly_async(store, NOW))
