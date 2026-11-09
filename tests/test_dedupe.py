from datetime import timedelta

from dealfix import NOW, make_deal
from dh.sources.base import seller_hash
from dh import dedupe
from dh.store import Store


def test_cross_platform_duplicate_suppressed(tmp_path):
    st = Store(tmp_path / "t.db")
    first, _, _ = make_deal(st, price="2000", seller="h1", sid="1", source="kleinanzeigen")
    # same seller hash + key, price +4%, other platform, 3 days later -> same physical item
    assert dedupe.is_duplicate(st, first) is False  # alone in the store: the deal itself is not its own duplicate
    again, _, _ = make_deal(st, price="2080", seller="h1", sid="9", source="marktplaats", created=NOW + timedelta(days=3))
    assert dedupe.is_duplicate(st, again) is True


def test_not_duplicate_cases(tmp_path):
    st = Store(tmp_path / "t.db")
    make_deal(st, price="2000", seller="h1", sid="1")
    other_price, _, _ = make_deal(st, price="2200", seller="h1", sid="2")           # +10%
    other_seller, _, _ = make_deal(st, price="2000", seller="h2", sid="3")
    too_late, _, _ = make_deal(st, price="2000", seller="h1", sid="4", created=NOW + timedelta(days=8))
    other_key, _, _ = make_deal(st, price="2000", seller="h1", sid="5", key="RTX 4080")
    for d in (other_price, other_seller, too_late, other_key):
        assert dedupe.is_duplicate(st, d) is False, d.listing_ref


def test_same_listing_and_missing_seller(tmp_path):
    st = Store(tmp_path / "t.db")
    a, _, lst = make_deal(st, seller=None, sid="1")
    again, _, _ = make_deal(st, seller=None, sid="1")  # re-upsert of the same listing row
    assert again.listing_ref == a.listing_ref and dedupe.is_duplicate(st, again) is True
    b, _, _ = make_deal(st, seller=None, sid="2")       # no seller hash and another listing: not provable
    assert dedupe.is_duplicate(st, b) is False


def _city(st, d, city):
    st.conn.execute("update listings set city=? where id=?", (city, d.listing_ref))
    st.conn.commit()


def test_cross_platform_by_city_when_seller_hashes_differ(tmp_path):
    st = Store(tmp_path / "t.db")
    a, _, _ = make_deal(st, price="2000", seller=seller_hash("kleinanzeigen", "u1", b"salt"), sid="1")
    b, _, _ = make_deal(st, price="2060", seller=seller_hash("marktplaats", "u1", b"salt"), sid="2",
                        source="marktplaats", created=NOW + timedelta(days=2))
    assert a.listing_ref != b.listing_ref
    _city(st, a, "Krak\u00f3w")
    _city(st, b, " krakow ")
    assert dedupe.is_duplicate(st, b) is True


def test_cross_platform_city_negative_cases(tmp_path):
    st = Store(tmp_path / "t.db")
    a, _, _ = make_deal(st, price="2000", seller="h1", sid="1")
    b, _, _ = make_deal(st, price="2000", seller="h2", sid="2", source="marktplaats")
    c, _, _ = make_deal(st, price="2000", seller="h3", sid="3", source="marktplaats")
    _city(st, a, "Krak\u00f3w")
    _city(st, b, "Warszawa")
    assert dedupe.is_duplicate(st, b) is False            # different city
    _city(st, a, None)
    _city(st, b, None)
    _city(st, c, None)
    assert dedupe.is_duplicate(st, b) is False and dedupe.is_duplicate(st, c) is False  # missing city never matches
    _city(st, a, "Krak\u00f3w")
    _city(st, c, "Krak\u00f3w")
    far, _, _ = make_deal(st, price="2600", seller="h4", sid="4", source="marktplaats")  # price +30%
    _city(st, far, "Krak\u00f3w")
    assert dedupe.is_duplicate(st, far) is False


def _title(st, d, title):
    st.conn.execute("update listings set title=? where id=?", (title, d.listing_ref))
    st.conn.commit()


def test_cross_platform_city_match_also_needs_similar_titles(tmp_path):
    """Final review minor: two different 4090s for sale in one big city are not one item. A city match counts only
    when the titles are similar too (rapidfuzz token_set_ratio >= 85); the same seller hash still needs no title."""
    st = Store(tmp_path / "t.db")
    a, _, _ = make_deal(st, price="2000", seller="h1", sid="1")
    b, _, _ = make_deal(st, price="2040", seller="h2", sid="2", source="marktplaats", created=NOW + timedelta(days=1))
    _city(st, a, "Warszawa")
    _city(st, b, "warszawa")
    _title(st, a, "Gigabyte RTX 4090 Gaming OC 24GB")
    _title(st, b, "MSI GeForce RTX 4090 Suprim Liquid X")
    assert dedupe.is_duplicate(st, b) is False
    _title(st, b, "RTX 4090 Gaming OC 24GB Gigabyte, stan idealny")  # the same card relisted, words reordered
    assert dedupe.is_duplicate(st, b) is True
    c, _, _ = make_deal(st, price="2000", seller="h1", sid="3", source="kleinanzeigen")
    _title(st, c, "Komplett anderer Titel")
    assert dedupe.is_duplicate(st, c) is True  # same seller hash: the title does not matter
