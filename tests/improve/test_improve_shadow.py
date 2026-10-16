"""Task 29: Poisson CUSUM, sentinel attribution, probation and rollback, shadow scoring and labels."""
import asyncio
import math
import random
from datetime import datetime, timedelta, timezone

import pytest

from dh import ledger
from dh.improve import cusum, gates, monitor, probation, shadow
from dh.improve import playbook as pbm
from dh.improve.playbook import Item, Playbook
from dh.store import Store
from improve_fix import KEY_3090, NOW, alias_fe, archive, listing, seed_playbook


def run(coro):
    return asyncio.run(coro)


def poisson(rng: random.Random, lam: float) -> int:
    l, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= l:
            return k
        k += 1


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "dh.db")
    yield s
    s.close()


# ------------------------------------------------------------------ CUSUM


def test_cusum_alarms_on_a_halving_not_on_noise():
    rng = random.Random(7)
    c = cusum.PoissonCusum(0.3)
    assert not any(c.update(poisson(rng, 0.3)) for _ in range(300))  # ARL0 ~1300 rounds at h=4
    c.reset()
    n = next(i for i in range(1, 2000) if c.update(poisson(rng, 0.15)))
    assert 10 <= n <= 400
    c2 = cusum.PoissonCusum(0.3)
    assert next(i for i in range(1, 100) if c2.update(0)) == math.floor(4 / 0.15) + 1  # each empty round adds 0.15
    assert cusum.PoissonCusum.from_dict(c2.to_dict()).s == pytest.approx(c2.s)
    with pytest.raises(ValueError):
        cusum.PoissonCusum(0.3, 0.5)


def test_baseline_needs_rounds_and_events():
    assert cusum.baseline([1] * 10) is None
    assert cusum.baseline([0] * 50) is None
    assert cusum.baseline([0, 1] * 20) == 0.5


def _alarm(lam=1.0, s=5.0):
    c = cusum.PoissonCusum(lam)
    c.s = s
    return c


def test_attribution_table():
    quiet = cusum.PoissonCusum(1.0)
    base = dict(c_base=100, s_base=100, rounds_base=100, rounds_after=30)
    assert cusum.attribute(quiet, quiet, c_after=30, s_after=30, **base) == "quiet"
    assert cusum.attribute(quiet, _alarm(), c_after=30, s_after=10, **base) == "sentinel_drift"
    # both fell by the same factor: the market
    assert cusum.attribute(_alarm(), _alarm(), c_after=10, s_after=10, **base) == "market_shift"
    assert cusum.attribute(_alarm(), quiet, c_after=10, s_after=9, **base) == "market_shift"
    # only the champion fell: breakage, even when the sentinel's own chart alarms by chance
    assert cusum.attribute(_alarm(), quiet, c_after=8, s_after=30, **base) == "breakage"
    assert cusum.attribute(_alarm(), _alarm(), c_after=8, s_after=30, **base) == "breakage"
    # neither test decides yet
    assert cusum.attribute(_alarm(), quiet, c_after=22, s_after=30, **base) == "hold"
    assert cusum.attribute(_alarm(), None, c_after=22, s_after=0, c_base=100, s_base=0, rounds_base=100,
                           rounds_after=30) == "breakage"
    # the exact tests behind it
    assert gates.hypergeom_cdf(0, 10, 5, 5) == pytest.approx(1 / 252)
    assert gates.binom_cdf(1, 3, 0.5) == pytest.approx(0.5)


# ------------------------------------------------------------------ probation + rollback (acceptance 3 and 4)


def _promoted(store, root):
    """v1 baseline (the seed), v2 = seed + alias, promoted and active."""
    (root / "playbook").mkdir(parents=True, exist_ok=True)
    base = seed_playbook()
    base.activate(root)
    v1 = pbm.ensure_baseline(store, root)
    d = base.make_delta("add", alias_fe())
    v2 = base.apply(d).save_version(store, status="shadow", parent=v1, delta=d)
    probation.promote(store, v2, root)
    return v1, v2


def _simulate(store, root, champ_after: float, sent_after: float, seed: int, rounds: int = 300):
    v1, v2 = _promoted(store, root)
    rng = random.Random(seed)
    base_c = [poisson(rng, 1.0) for _ in range(100)]
    base_s = [poisson(rng, 1.0) for _ in range(100)]
    p = probation.Probation.start(v2, v1, started_wh=0.0, champion_counts=base_c, sentinel_counts=base_s)
    outcome = None
    for i in range(rounds):
        r = p.observe(poisson(rng, champ_after), poisson(rng, sent_after), watched_h=i / 6)  # 10-minute rounds
        if r in ("rollback", "passed"):
            outcome = r
            break
    return v1, v2, p, outcome


@pytest.mark.parametrize("seed", range(40))
def test_market_shift_no_rollback(store, tmp_path, seed):
    """Acceptance 3: the sentinel and the champion both drop -> never a rollback."""
    v1, v2, p, outcome = _simulate(store, tmp_path, 0.3, 0.3, seed)
    assert outcome == "passed", p.state["events"]
    assert any(e["event"] == "market_shift" for e in p.state["events"])
    assert pbm.active_version(store) == v2


@pytest.mark.parametrize("seed", range(40))
def test_breakage_rollback(store, tmp_path, seed):
    """Acceptance 4: only the champion drops -> rollback to the previous version."""
    v1, v2, p, outcome = _simulate(store, tmp_path, 0.3, 1.0, seed)
    assert outcome == "rollback", p.state["events"]
    restored = probation.rollback(store, v2, tmp_path)
    assert pbm.active_version(store) == v1
    assert restored == Playbook.load(v1, store=store) == Playbook.from_file(tmp_path)
    assert restored.get("alias.fe_glued_4090") is None
    assert {r["id"]: r["status"] for r in map(dict, store.conn.execute("select id, status from playbook_versions"))}[v2] \
        == "failed_in_prod"


def test_steady_market_passes_probation(store, tmp_path):
    v1, v2, p, outcome = _simulate(store, tmp_path, 1.0, 1.0, seed=3)
    assert outcome == "passed" and not [e for e in p.state["events"] if e["event"] != "sentinel_drift"]


def test_probation_state_survives_a_restart(store, tmp_path):
    v1, v2 = _promoted(store, tmp_path)
    p = probation.Probation.start(v2, v1, 0.0, [1] * 60, [1] * 60)
    for i in range(5):
        p.observe(0, 1, i / 6)
    q = probation.Probation(p.to_dict())
    assert q.champion.s == pytest.approx(p.champion.s) and q.state["rounds_after"] == 5


# ------------------------------------------------------------------ watched hours


def test_watched_hours_is_the_union_of_windows(store):
    t0 = NOW
    for src in ("olx", "bazos", "kleinanzeigen"):  # three sources watched over the same two hours
        w = ledger.open_window(store, src, t0)
        store.close_window(w, t0 + timedelta(hours=2))
    w = ledger.open_window(store, "olx", t0 + timedelta(hours=5))
    store.close_window(w, t0 + timedelta(hours=6))
    assert monitor.watched_hours(store) == pytest.approx(3.0)
    assert ledger.watched_hours(store) == pytest.approx(7.0)  # the per-source sum the scoreboard shows


# ------------------------------------------------------------------ shadow


def test_score_round_finds_and_stores_disagreements(store):
    ids = archive(store)
    l = listing("fe1", "RTX 4090FE 24GB używana", "6000", seen=NOW - timedelta(hours=1))
    lid = store.upsert_listing(l)
    pb = seed_playbook()
    ch = pb.apply(pb.make_delta("add", alias_fe()))
    ds = run(shadow.score_round(pb, ch, [*ids.values(), lid], store=store, proposal_id=1, round_id=7, now=NOW))
    assert [(d.listing_id, d.kind) for d in ds] == [(lid, "key")]
    assert ds[0].champion.key is None and ds[0].challenger.key == "gpu:rtx-4090:24"
    t = shadow.tally(store, 1)
    assert t == {"wins": 0, "losses": 0, "ties": 0, "disagreements": 1, "unlabelled": 1, "rounds": 1, "scored": 4}
    assert shadow.label(store, ds[0].id, "key:gpu:rtx-4090:24") == "challenger"
    assert shadow.tally(store, 1)["wins"] == 1 and shadow.pending(store, 1) == []


def test_labels_come_only_from_verified_outcomes(store):
    ids = archive(store)  # 'deal' was bought, 'fp' skipped as a scam
    pb = seed_playbook()
    # a scam keyword that hits both descriptions: the challenger drops both deals
    ch = pb.apply(pb.make_delta("add", Item("scam_kw.sprzedaz", "scam_kw", "risk", "szybka sprzedaz")))
    store.conn.execute("update listings set description='Stan bardzo dobry, szybka sprzedaż.' where id=?", (ids["deal"],))
    store.conn.commit()
    ds = run(shadow.score_round(pb, ch, list(ids.values()), store=store, proposal_id=2, round_id=1, now=NOW))
    assert {d.listing_id for d in ds} <= {ids["deal"], ids["fp"]}
    assert shadow.auto_label(store, 2, NOW) == len(ds)
    w = {r["listing_id"]: r["winner"] for r in store.conn.execute(
        "select listing_id, winner from shadow_results where proposal_id=2 and metric='disagreement'")}
    if ids["deal"] in w:
        assert w[ids["deal"]] == "champion"  # the owner bought it: dropping it was wrong
    if ids["fp"] in w:
        assert w[ids["fp"]] == "challenger"  # the owner skipped it as a scam: dropping it was right


def test_unsold_for_a_week_is_not_a_deal(store):
    # at 2520 PLN the champion calls it a tier-2 deal; the extra scam keyword's risk haircut makes it tier 4
    l = listing("slow", "RTX 3090 Gigabyte Gaming OC 24GB używana", "2520", seen=NOW - timedelta(days=9))
    lid = store.upsert_listing(l)
    store.record_observation(lid, l.price, NOW - timedelta(days=9))
    store.record_observation(lid, l.price, NOW)
    archive(store)
    pb = seed_playbook()
    ch = pb.apply(pb.make_delta("add", Item("scam_kw.gigabyte_oc", "scam_kw", "risk", "gaming oc 24gb uzywana")))
    ds = run(shadow.score_round(pb, ch, [lid], store=store, proposal_id=3, round_id=1, now=NOW))
    assert ds and ds[0].champion.deal and not ds[0].challenger.deal
    assert shadow.auto_label(store, 3, NOW) == 1
    assert shadow.tally(store, 3)["wins"] == 1


def test_decide_single_look_and_twyman(store):
    shadow.migrate(store)
    for i in range(12):
        store.conn.execute("insert into shadow_results (proposal_id, metric, value, round_id) values (9,'round',10,?)", (i,))
    for i in range(10):
        store.conn.execute("insert into shadow_results (proposal_id, metric, value, listing_id, kind, winner, truth) "
                           "values (9,'disagreement',1,?,?,?,?)", (i, "key", "challenger" if i < 9 else "champion", "x"))
    store.conn.commit()
    v, d = shadow.decide(store, 9, watched_hours=5)
    assert v == "wait" and d["rounds"] == 12  # labels suffice, rounds do not (N = 40)
    for i in range(12, 40):
        store.conn.execute("insert into shadow_results (proposal_id, metric, value, round_id) values (9,'round',10,?)", (i,))
    store.conn.commit()
    v, d = shadow.decide(store, 9, watched_hours=7)
    assert v == "promote" and d["p"] < gates.SHADOW_ALPHA
    # the same tally with only 30 scored listings: 10 disagreements are a third of all judgments -> audit, no promotion
    store.conn.execute("update shadow_results set value=0 where proposal_id=9 and metric='round' and round_id >= 3")
    store.conn.commit()
    v, d = shadow.decide(store, 9, watched_hours=7)
    assert v == "reject" and "twyman" in d


def test_shadow_verdict_rules():
    assert gates.shadow_verdict(8, 2, 40, 10)[0] == "promote"  # p = 0.055
    assert gates.shadow_verdict(7, 3, 40, 10)[0] == "reject"  # single look: no second chance
    assert gates.shadow_verdict(3, 0, 39, 10)[0] == "wait"
    assert gates.shadow_verdict(0, 4, 10, 10)[0] == "reject"  # the champion clearly wins: stop early
    assert gates.shadow_verdict(2, 1, 200, gates.SHADOW_MAX_WATCHED_HOURS)[0] == "reject"  # inconclusive at the cap


def test_round_counts_split_champion_and_sentinel(store):
    from decimal import Decimal

    from dh import scheduler
    from dh.models import Deal
    ids = archive(store)
    store.ensure_table(scheduler.EVAL_SQL)
    ledger.migrate(store)
    rid = store.log_round(NOW - timedelta(minutes=10), NOW)
    at = NOW - timedelta(minutes=5)
    for name, arm in (("deal", "olx:rtx 3090"), ("fp", "kleinanzeigen:rtx 3090"), ("fair", "olx:rtx 3090")):
        store.conn.execute("insert into listing_arms (listing_id, arm) values (?,?)", (ids[name], arm))
        store.conn.execute("insert into evaluations (listing_id, price, title_hash, at, tier, note) values (?,?,?,?,?,?)",
                           (ids[name], "1", "h", at.isoformat(), None, ""))
    store.save_deal(Deal(id=None, listing_ref=ids["deal"], key=KEY_3090, tier=2, star=False, effort="easy",
                         confidence="medium", cons_net_pln=Decimal(400), exp_net_pln=Decimal(500),
                         cons_net_usd=Decimal(100), exp_net_usd=Decimal(125), capital_usd=Decimal(520),
                         breakdown_json="{}", evidence_json="{}", reasons=[], created_at=at))
    store.conn.commit()
    c, s, lids = run(monitor.round_counts(store, rid, seed_playbook(), [], now=NOW))
    assert (c, s) == (1, 1)  # the live deal from a champion arm; the sentinel arm's listing judged by the pinned playbook
    assert sorted(lids) == sorted(ids.values())
    assert monitor.sentinel_arm_ids(seed_playbook(), []) >= {"kleinanzeigen:rtx 3090", "hardverapro:rtx 4090"}
    monitor.record(store, rid, 1.5, 1, c, s, len(lids))
    assert monitor.counts(store) == ([1], [1])
