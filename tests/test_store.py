from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from dh.models import Deal, Extraction, Listing
from dh.store import Store

T0 = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)


def mk_listing(**kw) -> Listing:
    base = dict(
        source="olx", source_id="123", url="https://olx.pl/123", title="RTX 4090 FE",
        description="d", price=Decimal("5000.00"), currency="PLN", country="PL", city="Krakow",
        lat=50.06, lon=19.94, seller_hash="h" * 8, seller_age_days=400, seller_ratings=12,
        seller_score=4.9, ships=True, pickup_only=False, is_business=False, photos=["a.jpg"],
        posted_at=T0, seen_at=T0, raw_path=None,
    )
    base.update(kw)
    return Listing(**base)


def mk_deal(**kw) -> Deal:
    base = dict(
        id=None, listing_ref=1, key="gpu:rtx4090", tier=1, star=True, effort="easy",
        confidence="high", cons_net_pln=Decimal("1200.50"), exp_net_pln=Decimal("2000.00"),
        cons_net_usd=Decimal("320.10"), exp_net_usd=Decimal("540.00"), capital_usd=Decimal("1300"),
        breakdown_json="{}", evidence_json="{}", reasons=["cheap"], created_at=T0, verified_at=None,
        status="new",
    )
    base.update(kw)
    return Deal(**base)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "t.db")


def test_upsert_is_idempotent_and_updates_price(store):
    a = store.upsert_listing(mk_listing())
    b = store.upsert_listing(mk_listing(price=Decimal("4500.00"), seen_at=T0 + timedelta(minutes=5)))
    assert a == b
    rows = store.conn.execute("select price, currency, photos from listings").fetchall()
    assert len(rows) == 1
    assert Decimal(rows[0]["price"]) == Decimal("4500.00")
    c = store.upsert_listing(mk_listing(source_id="999"))
    assert c != a
    assert store.conn.execute("pragma journal_mode").fetchone()[0] == "wal"


def test_observations_and_windows_roundtrip(store):
    lid = store.upsert_listing(mk_listing())
    store.record_observation(lid, Decimal("5000.00"), T0)
    store.record_observation(lid, Decimal("4800.00"), T0 + timedelta(minutes=3))
    obs = store.conn.execute("select price from observations where listing_id=? order by seen_at", (lid,)).fetchall()
    assert [Decimal(o["price"]) for o in obs] == [Decimal("5000.00"), Decimal("4800.00")]

    w = store.open_window("olx", T0)
    assert [x["id"] for x in store.open_windows()] == [w]
    store.close_window(w, T0 + timedelta(minutes=10))
    assert store.open_windows() == []
    row = store.conn.execute("select closed_at from windows where id=?", (w,)).fetchone()
    assert row["closed_at"] is not None


def test_listings_missing_since(store):
    seen_late = store.upsert_listing(mk_listing(source_id="a", seen_at=T0 + timedelta(minutes=20)))
    seen_early = store.upsert_listing(mk_listing(source_id="b", seen_at=T0))
    other_src = store.upsert_listing(mk_listing(source="ebay", source_id="c", seen_at=T0))
    missing = store.listings_missing_since("olx", T0 + timedelta(minutes=10))
    assert missing == [seen_early]
    assert seen_late not in missing and other_src not in missing


def test_save_extraction_roundtrip(store):
    lid = store.upsert_listing(mk_listing())
    e = Extraction(key="gpu:rtx4090", category="gpu", certainty=0.95, condition="used", defects=["fan"],
                   box_only=False, for_parts=False, wanted_ad=False, bundle_items=[], method="rules")
    store.save_extraction(lid, e)
    store.save_extraction(lid, e)  # replace, not duplicate
    rows = store.conn.execute("select * from extractions where listing_id=?", (lid,)).fetchall()
    assert len(rows) == 1 and rows[0]["key"] == "gpu:rtx4090"


def test_deal_save_and_filter_by_tier(store):
    d1 = store.save_deal(mk_deal(tier=1))
    d2 = store.save_deal(mk_deal(tier=3, star=False, reasons=["x", "y"]))
    assert [d.id for d in store.deals({"tier": 1})] == [d1]
    assert {d.id for d in store.deals({})} == {d1, d2}
    got = store.get_deal(d2)
    assert got.tier == 3 and got.star is False and got.reasons == ["x", "y"]
    assert got.cons_net_pln == Decimal("1200.50") and isinstance(got.cons_net_pln, Decimal)
    assert got.created_at == T0
    assert store.get_deal(9999) is None
    with pytest.raises(ValueError):
        store.deals({"bogus": 1})


def test_marks_comps_and_logs(store):
    d = store.save_deal(mk_deal())
    store.add_mark(d, "bought", Decimal("4900"), "note", "owner")
    assert store.conn.execute("select count(*) from marks").fetchone()[0] == 1
    store.add_comp("gpu:rtx4090", "used", "olx", Decimal("5200"), T0 - timedelta(days=40), "sold", "r1")
    store.add_comp("gpu:rtx4090", "used", "olx", Decimal("5400"), T0 - timedelta(days=2), "sold", "r2")
    store.add_comp("gpu:rtx4090", "new", "olx", Decimal("7000"), T0 - timedelta(days=2), "ask", "r3")
    recent = store.comps("gpu:rtx4090", "used", "olx", T0 - timedelta(days=30))
    assert len(recent) == 1 and Decimal(recent[0]["price_pln"]) == Decimal("5400")
    assert len(store.comps("gpu:rtx4090", "used", "olx", None)) == 2
    r = store.log_round(started_at=T0)
    store.add_cost(r, "llm", Decimal("1.5"), 100)
    store.add_health("olx", "http_429", "backoff")
    for t in ("costs", "health", "rounds"):
        assert store.conn.execute(f"select count(*) from {t}").fetchone()[0] == 1


def test_ensure_table_idempotent_and_all_tables_exist(store):
    store.ensure_table("create table if not exists pending_pushes (id integer primary key, deal_id integer)")
    store.ensure_table("create table if not exists pending_pushes (id integer primary key, deal_id integer)")
    names = {r[0] for r in store.conn.execute("select name from sqlite_master where type='table'")}
    for t in "listings observations windows extractions deals marks comps rounds arm_stats costs health fx playbook_versions proposals shadow_results pending_pushes".split():
        assert t in names


def test_naive_datetime_rejected(store):
    with pytest.raises(ValueError):
        store.upsert_listing(mk_listing(seen_at=datetime(2026, 10, 29, 12, 0)))


def test_iso_parses_strings_and_normalises():
    from dh.store import _iso
    assert _iso("2026-10-29T14:00:00+02:00") == "2026-10-29T12:00:00+00:00"
    assert _iso("2026-10-29T12:00:00+00:00") == "2026-10-29T12:00:00+00:00"
    assert _iso(None) is None
    with pytest.raises(ValueError):
        _iso("2026-10-29T12:00:00")
    with pytest.raises(ValueError):
        _iso("garbage")


def test_non_utc_normalised_to_utc(store):
    from datetime import timezone as tz
    plus2 = tz(timedelta(hours=2))
    lid = store.upsert_listing(mk_listing(seen_at=datetime(2026, 10, 29, 14, 0, tzinfo=plus2)))
    row = store.conn.execute("select last_seen_at from listings where id=?", (lid,)).fetchone()
    assert row["last_seen_at"] == T0.isoformat()


def test_older_seen_at_does_not_move_last_seen_back(store):
    lid = store.upsert_listing(mk_listing(seen_at=T0 + timedelta(minutes=30)))
    store.upsert_listing(mk_listing(seen_at=T0))
    row = store.conn.execute("select last_seen_at from listings where id=?", (lid,)).fetchone()
    assert row["last_seen_at"] == (T0 + timedelta(minutes=30)).isoformat()


def test_contact_in_text_roundtrip_and_sticky(store):
    lid = store.upsert_listing(mk_listing(contact_in_text=True))
    row = lambda: store.conn.execute("select contact_in_text from listings where id=?", (lid,)).fetchone()[0]  # noqa: E731
    assert row() == 1
    store.upsert_listing(mk_listing(contact_in_text=False, seen_at=T0 + timedelta(minutes=5)))
    assert row() == 1  # once true it stays true
    other = store.upsert_listing(mk_listing(source_id="clean"))
    assert store.conn.execute("select contact_in_text from listings where id=?", (other,)).fetchone()[0] == 0
    from dh.verify import load_listing
    assert load_listing(store, lid).contact_in_text is True and load_listing(store, other).contact_in_text is False


def test_contact_in_text_column_added_to_existing_db(tmp_path):
    import sqlite3
    from dh.store import Store
    db = tmp_path / "old.db"
    c = sqlite3.connect(db)
    c.execute("create table listings (id integer primary key, source text not null, source_id text not null, unique(source, source_id))")
    c.commit(); c.close()
    s = Store(db)
    assert "contact_in_text" in {r["name"] for r in s.conn.execute("pragma table_info(listings)")}
    s.close()
    Store(db).close()  # second open: guarded, no error


def test_risk_phone_re_public_alias():
    from dh import risk
    assert risk.PHONE_RE is risk._PHONE
