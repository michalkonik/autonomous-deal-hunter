"""Clears-at ledger: observation windows, disappearance inference, probably-sold comps (Review Focus 1)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from dh import ledger
from dh.models import Extraction, Listing
from dh.store import Store

T0 = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)
KEY = "gpu:rtx-4090:24"
SRC = "allegro_lokalnie"
ARM = ledger.arm_id(SRC, "rtx 4090")


def h(hours: float) -> datetime:
    return T0 + timedelta(hours=hours)


def lst(sid: str, price: str, *, source: str = SRC, currency: str = "PLN", posted: datetime | None = None,
        seen: datetime = T0) -> Listing:
    return Listing(source=source, source_id=sid, url=f"https://example.test/{source}/{sid}", title=f"RTX 4090 {sid}",
                   description="", price=Decimal(price), currency=currency, country="PL", city=None, lat=None,
                   lon=None, seller_hash=None, seller_age_days=None, seller_ratings=None, seller_score=None,
                   ships=True, pickup_only=False, is_business=False, photos=[], posted_at=posted, seen_at=seen)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "dh.db")
    yield s
    s.close()


def lid(store: Store, sid: str, source: str = SRC) -> int:
    return store.conn.execute("select id from listings where source=? and source_id=?", (source, sid)).fetchone()["id"]


def disappeared(store: Store, sid: str, source: str = SRC) -> str | None:
    ledger.migrate(store)
    return store.conn.execute("select disappeared_at from listings where source=? and source_id=?",
                              (source, sid)).fetchone()["disappeared_at"]


def sweep(store: Store, ts: datetime, listings: list[Listing], arm: str = ARM, source: str = SRC) -> None:
    ledger.observe(store, source, listings, ts, arm=arm)
    ledger.sweep_disappeared(store, source, ts)


def extract_all(store: Store, condition: str = "used") -> None:
    for r in store.conn.execute("select id from listings").fetchall():
        store.save_extraction(r["id"], Extraction(key=KEY, category="gpu", certainty=0.95, condition=condition))


# ------------------------------------------------------------------ disappearance inference


def test_disappearance_needs_two_missed_successful_sweeps(store):
    a, b = lst("a", "3000"), lst("b", "3100")
    sweep(store, h(0), [a, b])
    sweep(store, h(0.5), [a, b])
    sweep(store, h(1), [b])  # first miss
    assert disappeared(store, "a") is None
    sweep(store, h(1.5), [b])  # second miss
    assert disappeared(store, "a") == h(1).isoformat()  # the first sweep that missed it
    assert disappeared(store, "b") is None


def test_no_disappearance_across_stop(store):
    """Review Focus 1: the listing vanished while the agent was stopped -> its disappearance is unknown."""
    a, b = lst("a", "3000"), lst("b", "3100")
    sweep(store, h(0), [a, b])
    sweep(store, h(0.5), [a, b])
    assert ledger.close_all_windows(store, h(1)) == 1  # dh stop
    w = store.conn.execute("select * from windows").fetchone()
    assert w["closed_at"] == h(0.5).isoformat()  # closed at the last observation, not at the stop time

    # next start, a day later: the listing is gone from every sweep
    ledger.open_window(store, SRC, h(24))
    for t in (24, 24.5, 25, 25.5, 26):
        sweep(store, h(t), [b])
    assert disappeared(store, "a") is None
    assert ledger.clears_at(store, KEY, None, h(-1)) == []


def test_no_disappearance_across_a_short_stop(store):
    """A stop shorter than the gap guard: only the window boundary protects the inference."""
    a, b = lst("a", "3000"), lst("b", "3100")
    sweep(store, h(0), [a, b])
    sweep(store, h(0.5), [a, b])
    ledger.close_all_windows(store, h(0.75))  # dh stop
    ledger.open_window(store, SRC, h(1.5))  # dh start 45 minutes later
    for t in (1.5, 2, 2.5, 3):
        sweep(store, h(t), [b])
    assert disappeared(store, "a") is None
    ledger.close_all_windows(store, h(4))
    assert disappeared(store, "a") is None


def test_missed_sweeps_split_across_a_stop_do_not_add_up(store):
    a, b = lst("a", "3000"), lst("b", "3100")
    sweep(store, h(0), [a, b])
    sweep(store, h(0.5), [b])  # one miss in window 1
    ledger.close_all_windows(store, h(0.6))
    sweep(store, h(1), [b])  # one miss in window 2 (auto-opened by observe), well inside the gap guard
    sweep(store, h(1.5), [b])
    assert disappeared(store, "a") is None


def test_crash_recovery_closes_windows(store):
    a, b = lst("a", "3000"), lst("b", "3100")
    sweep(store, h(0), [a, b])
    sweep(store, h(0.5), [a, b])
    # crash: nobody closed the window. Startup recovery runs ten hours later.
    assert len(store.open_windows()) == 1
    assert ledger.close_all_windows(store, h(10)) == 1
    assert store.open_windows() == []
    w = store.conn.execute("select * from windows").fetchone()
    assert w["closed_at"] == h(0.5).isoformat()
    ledger.open_window(store, SRC, h(10))
    sweep(store, h(10), [b])
    sweep(store, h(10.5), [b])
    sweep(store, h(11), [b])
    assert disappeared(store, "a") is None
    assert len(store.open_windows()) == 1


def test_crash_recovery_still_counts_evidence_gathered_inside_the_window(store):
    a, b = lst("a", "3000"), lst("b", "3100")
    sweep(store, h(0), [a, b])
    ledger.observe(store, SRC, [b], h(0.5), arm=ARM)
    ledger.observe(store, SRC, [b], h(1), arm=ARM)  # two misses observed, sweep_disappeared never ran: crash
    ledger.close_all_windows(store, h(9))
    assert disappeared(store, "a") == h(0.5).isoformat()


def test_stale_open_window_without_recovery_is_not_trusted(store):
    """Belt and braces: if startup recovery was skipped, a long silence inside one window breaks continuity."""
    a, b = lst("a", "3000"), lst("b", "3100")
    sweep(store, h(0), [a, b])
    sweep(store, h(0.5), [a, b])
    # crash, then restart WITHOUT close_all_windows: observe() reuses the stale open window
    sweep(store, h(9), [b])
    sweep(store, h(9.5), [b])
    sweep(store, h(10), [b])
    assert disappeared(store, "a") is None


def test_open_window_closes_a_stale_window_of_the_same_source(store):
    sweep(store, h(0), [lst("a", "3000")])
    ledger.open_window(store, SRC, h(5))
    rows = store.conn.execute("select * from windows order by id").fetchall()
    assert len(rows) == 2 and rows[0]["closed_at"] == h(0).isoformat() and rows[1]["closed_at"] is None


def test_failed_and_empty_sweeps_do_not_count(store):
    a, b = lst("a", "3000"), lst("b", "3100")
    sweep(store, h(0), [a, b])
    ledger.record_sweep(store, SRC, ARM, h(0.5), note="403")
    ledger.observe(store, SRC, [], h(1), arm=ARM)  # parser broke: zero results is not evidence
    ledger.sweep_disappeared(store, SRC, h(1))
    assert disappeared(store, "a") is None
    sweep(store, h(1.5), [b])
    assert disappeared(store, "a") is None  # only one successful non-empty miss so far
    sweep(store, h(2), [b])
    assert disappeared(store, "a") == h(1.5).isoformat()


def test_other_arms_sweeps_do_not_count(store):
    a = lst("a", "3000")
    other = ledger.arm_id(SRC, "rtx 4080")
    sweep(store, h(0), [a])
    sweep(store, h(0.5), [lst("x", "2000")], arm=other)
    sweep(store, h(1), [lst("x", "2000")], arm=other)
    assert disappeared(store, "a") is None


def test_observation_without_arm_records_no_sweep(store):
    """A mailbox alert or a detail fetch is a sighting, not a sweep: it must not make other listings vanish."""
    a = lst("a", "3000")
    sweep(store, h(0), [a])
    ledger.observe(store, SRC, [lst("m1", "2500")], h(0.5))
    ledger.observe(store, SRC, [lst("m2", "2600")], h(1))
    ledger.sweep_disappeared(store, SRC, h(1))
    assert disappeared(store, "a") is None
    assert store.conn.execute("select count(*) c from sweeps").fetchone()["c"] == 1


def test_reappearance_clears_disappeared_at(store):
    a, b = lst("a", "3000"), lst("b", "3100")
    sweep(store, h(0), [a, b])
    sweep(store, h(0.5), [b])
    sweep(store, h(1), [b])
    assert disappeared(store, "a") is not None
    sweep(store, h(1.5), [a, b])  # it was only pushed off the results page
    assert disappeared(store, "a") is None


def test_observations_record_first_price_and_cuts_only(store):
    sweep(store, h(0), [lst("a", "3000")])
    sweep(store, h(0.5), [lst("a", "3000")])
    sweep(store, h(1), [lst("a", "2800")])
    rows = store.conn.execute("select price, seen_at from observations order by id").fetchall()
    assert [(r["price"], r["seen_at"]) for r in rows] == [("3000", h(0).isoformat()), ("2800", h(1).isoformat())]
    assert store.conn.execute("select last_seen_at from listings").fetchone()["last_seen_at"] == h(1).isoformat()


def test_listing_from_another_source_is_rejected(store):
    with pytest.raises(ValueError):
        ledger.observe(store, SRC, [lst("a", "3000", source="kleinanzeigen")], h(0), arm=ARM)


def test_naive_timestamp_rejected(store):
    with pytest.raises(ValueError):
        ledger.observe(store, SRC, [lst("a", "3000")], datetime(2026, 10, 1, 8, 0), arm=ARM)


# ------------------------------------------------------------------ probably sold


def _market(store: Store) -> None:
    """Window opens at h(0); four asks already listed (their age is unknown), then sweeps every 4 h."""
    base = [lst("m1", "3000"), lst("m2", "3200"), lst("m3", "3400"), lst("m4", "3600")]
    sweep(store, h(0), base)
    return base


def test_quick_disappearance_counts_as_probably_sold(store):
    base = _market(store)
    new = lst("new", "3100")
    sweep(store, h(4), base + [new])  # appeared between h(0) and h(4): watched appearance
    sweep(store, h(8), base + [new])
    sweep(store, h(12), base)  # gone
    sweep(store, h(16), base)
    extract_all(store)
    rows = ledger.clears_at(store, KEY, "used", h(-24))
    assert len(rows) == 1
    r = rows[0]
    assert r.kind == "probably_sold" and r.weight == 0.5
    assert r.price == Decimal("3100") and r.currency == "PLN" and r.price_pln == Decimal("3100.00")
    assert r.at == h(12) and r.key == KEY and r.condition == "used" and r.channel == SRC
    assert r.days_to_sell == pytest.approx(0.5)  # earliest possible appearance h(0) -> first miss h(12)
    assert ledger.clears_at(store, KEY, "like_new", h(-24)) == []
    assert ledger.clears_at(store, KEY, "used", h(13)) == []  # since is on the disappearance time


def test_slow_disappearance_not_counted(store):
    base = _market(store)
    new = lst("new", "3100")
    t = 4
    while t <= 76:
        sweep(store, h(t), base + [new])
        t += 4
    sweep(store, h(80), base)
    sweep(store, h(84), base)
    assert disappeared(store, "new") == h(80).isoformat()
    extract_all(store)
    assert ledger.clears_at(store, KEY, "used", h(-24)) == []


def test_disappearance_above_median_ask_not_counted(store):
    base = _market(store)
    new = lst("new", "3900")  # above the median ask: probably withdrawn, not sold
    sweep(store, h(4), base + [new])
    sweep(store, h(8), base)
    sweep(store, h(12), base)
    extract_all(store)
    assert disappeared(store, "new") is not None
    assert ledger.clears_at(store, KEY, "used", h(-24)) == []


def test_listing_present_when_watching_started_has_unknown_age(store):
    """Seen in the first sweep of a window and no posted date: it may have been listed for weeks."""
    old = lst("old", "3000")
    base = [lst("m2", "3200"), lst("m3", "3400"), lst("m4", "3600")]
    sweep(store, h(0), base + [old])
    sweep(store, h(4), base)
    sweep(store, h(8), base)
    extract_all(store)
    assert disappeared(store, "old") is not None
    assert ledger.clears_at(store, KEY, "used", h(-24)) == []


def test_posted_date_makes_age_known(store):
    old = lst("old", "3000", posted=h(-10))
    base = [lst("m2", "3200"), lst("m3", "3400"), lst("m4", "3600")]
    sweep(store, h(0), base + [old])
    sweep(store, h(4), base)
    sweep(store, h(8), base)
    extract_all(store)
    rows = ledger.clears_at(store, KEY, "used", h(-24))
    assert [r.ref for r in rows] == [old.url]
    assert rows[0].days_to_sell == pytest.approx(14 / 24)


def test_posted_long_ago_is_a_slow_sale(store):
    old = lst("old", "3000", posted=h(-24 * 10))
    base = [lst("m2", "3200"), lst("m3", "3400"), lst("m4", "3600")]
    sweep(store, h(0), base)
    sweep(store, h(4), base + [old])  # newly in the results, but posted ten days ago
    sweep(store, h(8), base)
    sweep(store, h(12), base)
    extract_all(store)
    assert ledger.clears_at(store, KEY, "used", h(-24)) == []


def test_appearance_and_disappearance_in_different_windows(store):
    """Both ends were watched, the stop in between does not matter because the listing was seen again after it."""
    base = _market(store)
    new = lst("new", "3100")
    sweep(store, h(4), base + [new])
    ledger.close_all_windows(store, h(5))
    ledger.open_window(store, SRC, h(20))
    sweep(store, h(20), base + [new])
    sweep(store, h(24), base)
    sweep(store, h(28), base)
    extract_all(store)
    rows = ledger.clears_at(store, KEY, "used", h(-24))
    assert len(rows) == 1 and rows[0].at == h(24)


def test_too_few_asks_for_a_median(store):
    sweep(store, h(0), [lst("m1", "3000")])
    new = lst("new", "2000")
    sweep(store, h(4), [lst("m1", "3000"), new])
    sweep(store, h(8), [lst("m1", "3000")])
    sweep(store, h(12), [lst("m1", "3000")])
    extract_all(store)
    assert disappeared(store, "new") is not None
    assert ledger.clears_at(store, KEY, "used", h(-24)) == []


def test_bait_and_wanted_extractions_never_become_comps(store):
    base = _market(store)
    new = lst("new", "3100")
    sweep(store, h(4), base + [new])
    sweep(store, h(8), base)
    sweep(store, h(12), base)
    extract_all(store)
    store.save_extraction(lid(store, "new"), Extraction(key=KEY, category="gpu", certainty=0.95, condition="used",
                                                         box_only=True))
    assert ledger.clears_at(store, KEY, "used", h(-24)) == []


def test_median_is_per_currency_market(store):
    """A CZK listing is compared with CZK asks, never with PLN asks."""
    src = "bazos_cz"
    arm = ledger.arm_id(src, "rtx 4090")
    cz = [lst(f"c{i}", p, source=src, currency="CZK") for i, p in enumerate(("40000", "42000", "44000"))]
    sweep(store, h(0), cz, arm=arm, source=src)
    _market(store)
    new = lst("cnew", "41000", source=src, currency="CZK")
    sweep(store, h(4), cz + [new], arm=arm, source=src)
    sweep(store, h(8), cz, arm=arm, source=src)
    sweep(store, h(12), cz, arm=arm, source=src)
    extract_all(store)
    rows = ledger.clears_at(store, KEY, "used", h(-24))
    assert [(r.ref, r.currency, r.price_pln) for r in rows] == [(new.url, "CZK", None)]


def test_active_asks(store):
    base = _market(store)
    sweep(store, h(4), base[:3])
    sweep(store, h(8), base[:3])
    extract_all(store)
    rows = ledger.active_asks(store, KEY, "used", h(9))
    assert sorted(r.price for r in rows) == [Decimal("3000"), Decimal("3200"), Decimal("3400")]
    assert {r.kind for r in rows} == {"active_ask"} and {r.weight for r in rows} == {0.3}
    assert ledger.active_asks(store, KEY, "used", h(9 + 24 * 4)) == []  # not seen for 4 days


def test_bundles_and_uncertain_matches_are_never_comparables(store):
    """Live round 2026-10-29: a 29 440 PLN sim-racing PC "... PC RTX 4080 Super" (rules key at certainty 0.5 with
    bundle_items) became the only active ask of the GPU key and produced a fake +$3 197 deal."""
    base = _market(store)
    sweep(store, h(4), base[:3])
    extract_all(store)
    pc, lot = lid(store, base[0].source_id), lid(store, base[1].source_id)
    store.save_extraction(pc, Extraction(key=KEY, category="gpu", certainty=0.5, condition="used",
                                         bundle_items=["system"]))
    store.save_extraction(lot, Extraction(key=KEY, category="gpu", certainty=0.95, condition="used",
                                          bundle_items=["ram"]))
    rows = ledger.active_asks(store, KEY, "used", h(5))
    assert [r.listing_id for r in rows] == [lid(store, base[2].source_id), lid(store, base[3].source_id)]


def test_watched_hours(store):
    sweep(store, h(0), [lst("a", "3000")])
    sweep(store, h(2), [lst("a", "3000")])
    ledger.close_all_windows(store, h(5))
    sweep(store, h(10), [lst("a", "3000")])
    sweep(store, h(11), [lst("a", "3000")])
    assert ledger.watched_hours(store) == pytest.approx(3.0)
    assert ledger.watched_hours(store, "kleinanzeigen") == 0.0


def test_watched_hours_is_the_union_of_windows_across_sources(store):
    """Three sources watched over the same two hours are two watched hours, not six (status, books, web and the
    improvement loop's clock all show the same number)."""
    from dh.improve import monitor
    t0 = h(0)
    for src in ("olx", "bazos", "kleinanzeigen"):
        w = ledger.open_window(store, src, t0)
        store.close_window(w, t0 + timedelta(hours=2))
    w = ledger.open_window(store, "olx", t0 + timedelta(hours=5))
    store.close_window(w, t0 + timedelta(hours=6))
    assert ledger.watched_hours(store) == pytest.approx(3.0)
    assert ledger.watched_hours(store, "olx") == pytest.approx(3.0)
    assert ledger.watched_hours(store, "bazos") == pytest.approx(2.0)
    assert ledger.watched_hours(store, until=t0 + timedelta(hours=1)) == pytest.approx(1.0)
    assert monitor.watched_hours(store) == ledger.watched_hours(store)


def test_unknown_condition_is_valued_as_used(store):
    base = _market(store)
    new = lst("new", "3100")
    sweep(store, h(4), base + [new])
    sweep(store, h(8), base)
    sweep(store, h(12), base)
    extract_all(store, condition="unknown")
    assert len(ledger.clears_at(store, KEY, "used", h(-24))) == 1
    assert len(ledger.clears_at(store, KEY, "unknown", h(-24))) == 1
    assert ledger.clears_at(store, KEY, "new", h(-24)) == []
    store.save_extraction(lid(store, "new"), Extraction(key=KEY, category="gpu", certainty=0.95,
                                                         condition="for_parts"))
    assert ledger.clears_at(store, KEY, None, h(-24)) == []


def test_record_sweep_cannot_fabricate_successful_sweeps(store):
    """Review counter-example: two 'ok' sweeps recorded without results must not make a listing disappear."""
    a = lst("a", "3000")
    sweep(store, h(0), [a])
    with pytest.raises(TypeError):
        ledger.record_sweep(store, SRC, ARM, h(0.5), ok=True, n_results=40)
    ledger.record_sweep(store, SRC, ARM, h(0.5))
    ledger.record_sweep(store, SRC, ARM, h(1), note="timeout")
    ledger.sweep_disappeared(store, SRC, h(1))
    ledger.close_all_windows(store, h(2))
    assert disappeared(store, "a") is None
    rows = store.conn.execute("select ok, n_results from sweeps where ts>?", (h(0).isoformat(),)).fetchall()
    assert [(r["ok"], r["n_results"]) for r in rows] == [(0, 0), (0, 0)]


def test_unpriced_sighting_still_counts_as_seen(store):
    """'do negocjacji' (price 0) is a sighting: it keeps the listing alive but adds no price observation."""
    a, b = lst("a", "3000"), lst("b", "3100")
    sweep(store, h(0), [a, b])
    sweep(store, h(0.5), [lst("a", "0"), b])
    sweep(store, h(1), [lst("a", "0"), b])
    sweep(store, h(1.5), [lst("a", "0"), b])
    assert disappeared(store, "a") is None
    row = store.conn.execute("select price, last_seen_at from listings where source_id='a'").fetchone()
    assert row["price"] == "3000" and row["last_seen_at"] == h(1.5).isoformat()
    assert store.conn.execute("select count(*) c from observations o join listings l on l.id=o.listing_id "
                              "where l.source_id='a'").fetchone()["c"] == 1
    # an untracked unpriced listing is not stored, but the sweep still counts it as a result
    sweep(store, h(2), [lst("z", "0")])
    assert store.conn.execute("select count(*) c from listings where source_id='z'").fetchone()["c"] == 0
    assert store.conn.execute("select n_results from sweeps order by id desc limit 1").fetchone()["n_results"] == 1


def test_unpriced_sighting_clears_disappearance(store):
    a, b = lst("a", "3000"), lst("b", "3100")
    sweep(store, h(0), [a, b])
    sweep(store, h(0.5), [b])
    sweep(store, h(1), [b])
    assert disappeared(store, "a") is not None
    sweep(store, h(1.5), [lst("a", "0"), b])
    assert disappeared(store, "a") is None


def test_max_gap_is_honoured_when_windows_close(store):
    a, b = lst("a", "3000"), lst("b", "3100")
    sweep(store, h(0), [a, b])
    ledger.observe(store, SRC, [b], h(2), arm=ARM)
    ledger.observe(store, SRC, [b], h(2.5), arm=ARM)
    ledger.close_all_windows(store, h(3), max_gap=timedelta(hours=1))  # 2 h silence > 1 h guard
    assert disappeared(store, "a") is None


def test_open_window_honours_max_gap(store):
    a, b = lst("a", "3000"), lst("b", "3100")
    sweep(store, h(0), [a, b])
    ledger.observe(store, SRC, [b], h(2), arm=ARM)
    ledger.observe(store, SRC, [b], h(2.5), arm=ARM)
    ledger.open_window(store, SRC, h(3), max_gap=timedelta(hours=1))
    assert disappeared(store, "a") is None
    # with the default guard the same evidence is accepted
    s2 = Store(":memory:")
    try:
        sweep(s2, h(0), [a, b])
        ledger.observe(s2, SRC, [b], h(2), arm=ARM)
        ledger.observe(s2, SRC, [b], h(2.5), arm=ARM)
        ledger.open_window(s2, SRC, h(3))
        assert disappeared(s2, "a") == h(2).isoformat()
    finally:
        s2.close()


def test_migration_tracked_per_store_object(tmp_path):
    for i in range(3):
        s = Store(tmp_path / f"m{i}.db")
        ledger.observe(s, SRC, [lst("a", "3000")], h(0), arm=ARM)
        assert s.conn.execute("select count(*) c from sweeps").fetchone()["c"] == 1
        s.close()
        del s


def test_watched_hours_union_partial_nested_and_open_windows(store):
    t0 = h(0)
    a = ledger.open_window(store, "olx", t0)            # 0..4
    store.close_window(a, t0 + timedelta(hours=4))
    b = ledger.open_window(store, "bazos", t0 + timedelta(hours=3))   # partial overlap: 3..6 -> union 0..6
    store.close_window(b, t0 + timedelta(hours=6))
    c = ledger.open_window(store, "kleinanzeigen", t0 + timedelta(hours=1))  # nested in 0..6
    store.close_window(c, t0 + timedelta(hours=2))
    assert ledger.watched_hours(store) == pytest.approx(6.0)
    d = ledger.open_window(store, "hardverapro", t0 + timedelta(hours=10))  # open window: counts to last_obs_at
    store.conn.execute("update windows set last_obs_at=? where id=?", ((t0 + timedelta(hours=12)).isoformat(), d))
    store.conn.commit()
    assert ledger.watched_hours(store) == pytest.approx(8.0)
    assert ledger.watched_hours(store, until=t0 + timedelta(hours=11)) == pytest.approx(7.0)
    assert ledger.watched_hours(store, "hardverapro") == pytest.approx(2.0)
    e = ledger.open_window(store, "olx", t0 + timedelta(hours=20))  # open, never observed: zero
    assert ledger.watched_hours(store) == pytest.approx(8.0)


# ------------------------------------------------------------------ performance: one commit per sweep


class _CountingConn:
    """sqlite3.Connection stand-in that counts commits (the C type's methods cannot be patched)."""

    def __init__(self, conn):
        self._c, self.commits = conn, 0

    def commit(self):
        self.commits += 1
        return self._c.commit()

    def __getattr__(self, name):
        return getattr(self._c, name)


def test_observe_commits_once_per_sweep_not_per_listing(store, tmp_path):
    """Profiling (docs/VALIDATION.md): one fsync per upserted listing cost ~3.6 s of a round's event loop."""
    ledger.observe(store, SRC, [lst("w", "100")], h(0), arm="a")  # window and schema exist already
    store.conn = counting = _CountingConn(store.conn)
    ls = [lst(f"s{i}", str(100 + i)) for i in range(50)]
    ids = ledger.observe(store, SRC, ls, h(1), arm="a")
    assert len(ids) == 50 and counting.commits <= 2, counting.commits
    other = Store(tmp_path / "dh.db")  # durable and visible to another connection when observe returns
    try:
        n = other.conn.execute("select count(*) from listings where source=?", (SRC,)).fetchone()[0]
        obs = other.conn.execute("select count(*) from observations").fetchone()[0]
    finally:
        other.close()
    assert n == 51 and obs == 51
