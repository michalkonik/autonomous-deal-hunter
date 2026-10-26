"""Task 28: frozen eval sets, replay archive and the offline gate cascade."""
import asyncio
import json
from datetime import timedelta

import pytest

from dh.improve import gates, replay
from dh.improve.playbook import Delta, Item, Playbook
from dh.match import apply_aliases, rules_extract
from dh.store import Store
from improve_fix import KEY_3090, NOW, REPO, alias_fe, archive, seed_playbook


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "dh.db")
    yield s
    s.close()


# ------------------------------------------------------------------ eval assets


def test_eval_sets_shape_and_champion_passes_must_pass():
    mp = gates.load_cases(gates.MUST_PASS)
    gold = gates.load_cases(gates.GOLDEN)
    assert len(mp) >= 30 and len(gold) >= 100
    for cases in (mp, gold):
        ids = [c["id"] for c in cases]
        assert len(ids) == len(set(ids))
        assert all(c.get("expect") for c in cases)
    whys = " ".join(c["why"] for c in mp).lower()
    for kind in ("box-only", "wanted", "for parts", "accessory", "phone", "true deal", "comparison", "stripped"):
        assert kind in whys, kind
    ok, bad = run(gates.must_pass_ok(seed_playbook()))
    assert ok, bad


def test_golden_records_known_misses():
    res = {r.id: r.ok for r in run(gates.score_cases(seed_playbook(), gates.load_cases(gates.GOLDEN)))}
    assert res["g-miss-4090fe"] is False  # the champion misses it today: the golden set records the truth
    assert sum(res.values()) >= 0.9 * len(res)


def test_must_pass_scores_economics_with_the_real_pipeline():
    mp = {c["id"]: c for c in gates.load_cases(gates.MUST_PASS)}
    res = {r.id: r for r in run(gates.score_cases(seed_playbook(), list(mp.values())))}
    assert res["mp-deal-4090-verified"].outcome.tier == 1
    assert res["mp-deal-4090-unverified"].outcome.tier == 2
    assert res["mp-box-pl"].outcome.reason == "bait"
    assert res["mp-scam-phone"].outcome.reason == "hard_exclude"
    assert "scam_kw.box_sell" in res["mp-box-desc-payfirst"].outcome.flags


# ------------------------------------------------------------------ aliases (production path)


def test_alias_rewrites_title_before_matching(tmp_path):
    pb = seed_playbook()
    ch = pb.apply(pb.make_delta("add", alias_fe()))
    root = ch.materialise(tmp_path / "pb")
    assert rules_extract("RTX 4090FE 24GB", "", "pl").key is None
    ex = rules_extract("RTX 4090FE 24GB", "", "pl", root=root)
    assert ex.key == "gpu:rtx-4090:24" and "alias:alias.fe_glued_4090" in ex.notes
    assert apply_aliases("Gigabyte RTX 4090FEX", root)[1] == []  # whole words only
    assert rules_extract("Kupię RTX 4090FE", "", "pl", root=root).wanted_ad  # bait flags still win


# ------------------------------------------------------------------ statistics


def test_exact_tests():
    assert gates.binom_sf(9, 10) == pytest.approx(11 / 1024)
    assert gates.mcnemar_exact(0, 6) == pytest.approx(2 / 64)
    assert gates.mcnemar_exact(3, 3) == 1.0
    assert gates.mcnemar_exact(0, 0) == 1.0
    # research §3.2 table: doubling needs ~58 total deals at alpha 0.05, power 0.8 (one-sided, exact)
    assert 50 <= gates.required_deals(2.0) <= 66
    assert gates.mde_ratio(10) > gates.PLAUSIBLE_GAIN and gates.mde_ratio(10_000) <= 1.1


# ------------------------------------------------------------------ the cascade (acceptance 1 and 2)


def test_bad_delta_rejected(store):
    """Acceptance 1: removing the 'pudełko po' scam keyword fails the must-pass set."""
    pb = seed_playbook()
    bad = pb.apply(pb.make_delta("remove", pb.get("scam_kw.box_sell"), hypothesis="fewer false scam flags"))
    res = run(gates.evaluate(pb, bad, store, now=NOW))
    assert not res.passed and res.stage == "must_pass"
    assert "mp-box-desc-payfirst" in res.detail["must_pass"]["failed"]


def test_good_delta_passes(store):
    """Acceptance 2 (offline half): an alias that fixes a golden miss passes every offline gate."""
    archive(store)
    pb = seed_playbook()
    good = pb.apply(pb.make_delta("add", alias_fe(), hypothesis="glued FE suffix hides the chip"))
    res = run(gates.evaluate(pb, good, store, now=NOW))
    assert res.passed and res.stage == "offline", res.detail
    g = res.detail["golden"]
    assert "g-miss-4090fe" in g["fixes"] and g["regressions"] == []
    assert res.detail["replay"]["champion"]["n_listings"] == 3


def test_golden_regression_rejected(store):
    pb = seed_playbook()
    bad = pb.apply(pb.make_delta("add", Item("alias.xtx_typo", "alias", "match", "7900xtx => 7900 xt")))
    res = run(gates.evaluate(pb, bad, store, now=NOW))
    assert not res.passed and res.stage == "golden"
    assert res.detail["golden"]["regressions"] and not res.detail["golden"]["fixes"]


def test_no_effect_is_rejected(store):
    """'Fix more than you break': a judgment delta that fixes nothing is not worth its risk."""
    pb = seed_playbook()
    ch = pb.apply(pb.make_delta("add", Item("scam_kw.zzz", "scam_kw", "risk", "zzz qqq never seen")))
    res = run(gates.evaluate(pb, ch, store, now=NOW))
    assert not res.passed and res.stage == "golden"


@pytest.mark.parametrize("mutate,msg", [
    (lambda pb: pb.apply(Delta("add", Item("alias.a", "alias", "match", "aa => bb"))).apply(
        Delta("add", Item("alias.b", "alias", "match", "cc => dd"))), "exactly one item"),
    (lambda pb: pb.apply(pb.make_delta("update", Item("prompt.confirm", "prompt", "llm.confirm",
                                                       pb.get("prompt.confirm").value + "\nBe brief."))), "not editable"),
    (lambda pb: pb.apply(pb.make_delta("remove", pb.get("query.rtx_3060"))), "bandit"),
    (lambda pb: pb, "exactly one item"),
])
def test_schema_stage(store, mutate, msg):
    pb = seed_playbook()
    res = run(gates.evaluate(pb, mutate(pb), store, now=NOW))
    assert not res.passed and res.stage == "schema" and msg in " ".join(res.detail["errors"])


def test_sentinel_change_rejected_even_if_built_by_hand(store):
    pb = seed_playbook()
    items = [Item(i.id, i.kind, i.target, "rtx 4090 ti" if i.id == "sentinel.rtx_4090" else i.value, i.provenance,
                  i.helpful, i.harmful, i.sentinel, i.extra) for i in pb.items]
    res = run(gates.evaluate(pb, Playbook(items), store, now=NOW))
    assert not res.passed and res.stage == "schema" and "immutable" in " ".join(res.detail["errors"])


def test_arm_and_llm_doors_skip_offline_judgment(store):
    pb = seed_playbook()
    q = pb.apply(pb.make_delta("add", Item("query.arc_b580_test", "query", "all", "arc b580 12gb", extra={"min_pln": 400})))
    res = run(gates.evaluate(pb, q, store, now=NOW))
    assert res.passed and res.detail["door"] == "arm" and "not applicable" in res.detail["golden"]
    old = pb.get("prompt.extract")
    p = pb.apply(pb.make_delta("update", Item(old.id, "prompt", old.target, old.value.replace("A wrong key", "A wrong\nkey"))))
    res = run(gates.evaluate(pb, p, store, now=NOW))
    assert res.passed and res.detail["door"] == "llm"


# ------------------------------------------------------------------ replay


def test_replay_counts_known_outcomes(store):
    ids = archive(store)
    st = run(replay.run(seed_playbook(), NOW - timedelta(days=21), NOW, store))
    assert st.n_listings == 3
    assert st.outcomes[ids["deal"]].deal and st.outcomes[ids["fp"]].deal
    assert not st.outcomes[ids["fair"]].deal
    assert st.true_pos_known == 1 and st.false_pos_known == 1
    assert st.exp_net_t13_usd > 0 and st.fetches >= 2
    old = run(replay.run(seed_playbook(), NOW - timedelta(days=60), NOW - timedelta(days=30), store))
    assert old.n_listings == 0


def test_replay_gate_rejects_a_lost_true_deal(store):
    ids = archive(store)
    pb = seed_playbook()
    # an alias that garbles the Gigabyte 3090 title "fixes" a (contrived) golden case but loses the real deal
    ch = pb.apply(pb.make_delta("add", Item("alias.bad_rename", "alias", "match", "gigabyte gaming oc 24gb => 3080 10gb")))
    golden = [{"id": "g-contrived", "listing": {"title": "RTX 3090 Gigabyte Gaming OC 24GB"},
               "expect": {"key": None}}]
    mp = [{"id": "mp-one", "listing": {"title": "Pudełko po RTX 4090"}, "expect": {"excluded": True}}]
    res = run(gates.evaluate(pb, ch, store, now=NOW, golden=golden, must_pass=mp))
    assert not res.passed and res.stage == "replay"
    assert "a known true deal was lost" in res.detail["replay"]["why"]
    assert res.detail["replay"]["challenger"]["true_pos_known"] == 0


def test_offline_judge_uses_cached_llm(store):
    from decimal import Decimal

    from dh.models import Extraction
    from improve_fix import listing
    archive(store)
    l = listing("llm1", "Gigabyte RTX 3060 Gaming", "900", seen=NOW - timedelta(days=1))
    lid = store.upsert_listing(l)
    store.record_observation(lid, Decimal(900), NOW - timedelta(days=1))
    rules = rules_extract(l.title, "", "pl")
    assert rules.key and rules.certainty < 0.9  # memory size inferred: the pipeline would ask the LLM
    st = run(replay.run(seed_playbook(), NOW - timedelta(days=21), NOW, store))
    assert st.outcomes[lid].needs_llm and st.llm_tokens == replay.DEFAULT_TOKENS_PER_LLM_ITEM
    store.save_extraction(lid, Extraction(None, None, 0.0, "used", method="llm"))  # the cached LLM said: no key
    st = run(replay.run(seed_playbook(), NOW - timedelta(days=21), NOW, store))
    assert st.outcomes[lid].needs_llm  # no evaluation row proves the cache belongs to this title: not used
    from dh import scheduler
    store.ensure_table(scheduler.EVAL_SQL)
    store.conn.execute("insert into evaluations (listing_id, price, title_hash, at, tier, note) values (?,?,?,?,?,?)",
                       (lid, "900", replay.title_hash(l.title), NOW.isoformat(), None, ""))
    store.conn.commit()
    st = run(replay.run(seed_playbook(), NOW - timedelta(days=21), NOW, store))
    o = st.outcomes[lid]
    assert o.method == "cached_llm" and o.key is None and o.excluded and st.llm_tokens == 0
    # the seller changed the title since: the cached output is stale (the scheduler's own rule)
    store.conn.execute("update listings set title='Gigabyte RTX 3060 Gaming OC 12GB' where id=?", (lid,))
    store.conn.commit()
    st = run(replay.run(seed_playbook(), NOW - timedelta(days=21), NOW, store))
    assert st.outcomes[lid].method == "rules"


def test_title_hash_matches_the_scheduler():
    from dh import scheduler
    from improve_fix import listing
    l = listing("x", "RTX 4090 Founders Edition", "1")
    assert replay.title_hash(l.title) == scheduler._Round._title_hash(l)


def test_cached_extraction_is_keyed_by_prompt_epoch(store):
    from datetime import datetime, timezone

    from dh import scheduler
    from dh.models import Extraction
    from improve_fix import listing
    store.ensure_table(scheduler.EVAL_SQL)
    lid = store.upsert_listing(listing("e1", "Gigabyte RTX 3060 Gaming", "900"))
    store.save_extraction(lid, Extraction("gpu:rtx-3060:12", "gpu", 0.85, "used", method="llm"))
    t0, t1, t2 = (datetime(2026, 10, d, tzinfo=timezone.utc) for d in (1, 10, 20))
    replay.record_prompt_epoch(store, "shaA", t0)
    replay.record_prompt_epoch(store, "shaB", t2)
    store.conn.execute("insert into evaluations (listing_id, price, title_hash, at, tier, note) values (?,?,?,?,?,?)",
                       (lid, "900", replay.title_hash("Gigabyte RTX 3060 Gaming"), t1.isoformat(), None, ""))
    store.conn.commit()
    assert replay.cached_extraction(store, lid, "Gigabyte RTX 3060 Gaming", "shaA") is not None
    assert replay.cached_extraction(store, lid, "Gigabyte RTX 3060 Gaming", "shaB") is None  # other prompt's output


def test_alias_never_changes_bait_flags(tmp_path):
    pb = seed_playbook()
    unbait = pb.apply(pb.make_delta("add", Item("alias.unbait", "alias", "match", "pudelko po => karta")))
    root = unbait.materialise(tmp_path / "u")
    ex = rules_extract("Pudelko po RTX 4090", "", "pl", root=root)
    assert ex.box_only and ex.key is None and "alias:alias.unbait" in ex.notes
    bait = pb.apply(pb.make_delta("add", Item("alias.makebox", "alias", "match", "gaming oc => karton")))
    root2 = bait.materialise(tmp_path / "b")
    ex2 = rules_extract("RTX 3090 Gigabyte Gaming OC 24GB", "", "pl", root=root2)
    assert not ex2.box_only  # flags come from the seller's title; the alias only fed key matching


def test_exact_tests_never_overflow():
    assert gates.binom_sf(2500, 5000) == pytest.approx(0.5056, abs=1e-3)
    assert gates.binom_cdf(2499, 5000, 0.5) + gates.binom_sf(2500, 5000) == pytest.approx(1.0)
    assert 0 < gates.binom_sf(2700, 5000) < 1e-7
    assert gates.binom_sf(1100, 1030 * 2, 0.5) < 0.01
    assert gates.mcnemar_exact(500, 600) < 0.01
    assert gates.hypergeom_cdf(100, 5000, 2500, 400) < 1e-20


@pytest.mark.parametrize("note,fp", [("scam", True), ("to tylko box", True), ("Xbox pad w zestawie, nie chciałem", False),
                                     ("kupię: ogłoszenie kupna", True), ("kupiłem gdzie indziej", False),
                                     ("pudełko po karcie zamiast karty", True), ("no time", False), ("Fake", True)])
def test_fp_reason_words(note, fp):
    from evals.labels import is_false_positive
    assert is_false_positive("skipped", note) is fp


def test_box_keywords_are_narrow():
    """An original box sold WITH the card is normal: only box-only wording is a scam signal (golden proves it)."""
    pb = seed_playbook()
    assert pb.get("scam_kw.box_after") is None
    assert {pb.get(i).value for i in ("scam_kw.box_sell", "scam_kw.box_only_po", "scam_kw.box_tylko_po")} == {
        "sprzedam pudelko po", "samo pudelko po", "tylko pudelko po"}
    gold = [c for c in gates.load_cases(gates.GOLDEN) if c["id"].startswith("g-box-included")]
    assert len(gold) == 4 and all(r.ok for r in run(gates.score_cases(pb, gold)))
    broad = pb.apply(pb.make_delta("add", Item("scam_kw.old_broad", "scam_kw", "risk", "pudelko po")))
    res = {r.id: r.ok for r in run(gates.score_cases(broad, gold))}
    assert res["g-box-included-pl-verified"] is False


def test_install_baseline_records_the_manual_edits(store):
    from dh.improve import playbook as pbm
    vid = pbm.ensure_baseline(store, REPO)
    note = store.conn.execute("select note from playbook_versions where id=?", (vid,)).fetchone()["note"]
    assert "manual trim at install" in note and "sprzedam/samo/tylko pudelko po" in note
