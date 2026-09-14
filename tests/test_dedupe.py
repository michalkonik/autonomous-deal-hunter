from datetime import timedelta

from dealfix import NOW, make_deal
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
