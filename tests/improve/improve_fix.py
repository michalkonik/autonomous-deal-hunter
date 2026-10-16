"""Shared fixtures for the improvement-loop tests: a store with an archived market (listings, comps, FX, marks)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from dh.improve.playbook import Item, Playbook
from dh.models import Deal, Listing

REPO = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
KEY_3090 = "gpu:rtx-3090:24"


def seed_playbook() -> Playbook:
    return Playbook.from_file(REPO)


def alias_fe() -> Item:
    return Item("alias.fe_glued_4090", "alias", "match", "4090fe => 4090 fe", provenance="test")


def listing(sid: str, title: str, price: str, *, desc: str = "", ratings: int = 40, seen: datetime = NOW) -> Listing:
    return Listing(source="olx", source_id=sid, url=f"https://www.olx.pl/d/oferta/{sid}", title=title, description=desc,
                   price=Decimal(price), currency="PLN", country="PL", city="Krakow", lat=None, lon=None,
                   seller_hash=f"h{sid}", seller_age_days=900, seller_ratings=ratings, seller_score=4.9, ships=True,
                   pickup_only=False, is_business=False, photos=["https://x/p.jpg"], posted_at=None, seen_at=seen)


def archive(store, *, now: datetime = NOW) -> dict[str, int]:
    """A 3090 market: 16 sold comps on allegro and olx, a USD rate, and three archived listings:
    `deal` (a real deal the owner bought), `fp` (a deal the owner skipped as a scam), `fair` (market price)."""
    for i in range(16):
        for ch in ("allegro", "olx"):
            store.add_comp(KEY_3090, "used", ch, Decimal(3100 + 10 * i), now - timedelta(days=1 + i), "owner_sale",
                           f"seed:{ch}:{i}")
    for d in range(0, 30):
        day = (now - timedelta(days=d)).date().isoformat()
        store.conn.execute("insert or replace into fx (date, currency, rate_pln, effective_date) values (?,?,?,?)",
                           (day, "USD", "3.90", day))
        store.conn.execute("insert or replace into fx (date, currency, rate_pln, effective_date) values (?,?,?,?)",
                           (day, "EUR", "4.25", day))
    store.conn.commit()
    ids = {}
    for name, title, price, desc in (
            ("deal", "RTX 3090 Gigabyte Gaming OC 24GB używana", "2000", "Stan bardzo dobry."),
            ("fp", "RTX 3090 MSI Ventus 24GB używana", "2050", "Szybka sprzedaż."),
            ("fair", "RTX 3090 Zotac Trinity 24GB używana", "3150", "")):
        l = listing(name, title, price, desc=desc, seen=now - timedelta(days=2))
        lid = store.upsert_listing(l)
        store.record_observation(lid, l.price, now - timedelta(days=2))
        ids[name] = lid
    for name, status, note in (("deal", "bought", "great"), ("fp", "skipped", "scam: fake listing")):
        did = store.save_deal(Deal(id=None, listing_ref=ids[name], key=KEY_3090, tier=2, star=False, effort="easy",
                                   confidence="medium", cons_net_pln=Decimal(400), exp_net_pln=Decimal(500),
                                   cons_net_usd=Decimal(100), exp_net_usd=Decimal(125), capital_usd=Decimal(520),
                                   breakdown_json="{}", evidence_json="{}", reasons=[], created_at=now - timedelta(days=2)))
        store.add_mark(did, status, None, note, "web")
    return ids
