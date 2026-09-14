"""Shared fixtures for card, verify and dedupe tests (no network, no LLM)."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from dh import cards, economics
from dh.models import CompRef, Deal, Listing
from dh.risk import Risk
from dh.valuation import ExitEstimate
from test_economics import D0, FakeCustoms, FakeFx, P, inp, make_cfg

NOW = datetime(2026, 10, 29, 12, tzinfo=timezone.utc)


def listing(source="kleinanzeigen", sid="1", seller="s1", price="2000", url="https://www.kleinanzeigen.de/s-anzeige/x/1"):
    return Listing(source=source, source_id=sid, url=url, title="RTX 4090 Gaming", description="d",
                   price=Decimal(price), currency="PLN", country="PL", city="Krakow", lat=None, lon=None,
                   seller_hash=seller, seller_age_days=400, seller_ratings=50, seller_score=4.9, ships=True,
                   pickup_only=False, is_business=False, photos=[], posted_at=None, seen_at=NOW)


def estimate():
    comps = [CompRef("owner_sale", "allegro", P("2650"), NOW - timedelta(days=d), 1.0, f"https://allegro.pl/c/{d}")
             for d in (1, 2, 3)]
    return ExitEstimate("allegro", "RTX 4090", "used", P("2600"), P("2800"), P("2900"), 3.0, 3, 1.0, 6.0, 0.02,
                        P("3100"), comps)


def risk():
    return Risk(0.02, False, ["new_account"], ["Pay only through the platform's protected checkout.",
                                                "Seller account is new or unrated: test the item first."])


def net_inputs(price="2000"):
    return inp(buy_price=P(price), exit_price_cons_pln=P("2600"), exit_price_exp_pln=P("2800"),
               inbound_ship_pln=P("20"), outbound_ship_pln=P("25"), p_scam=0.02)


def make_deal(st, price="2000", seller="s1", sid="1", source="kleinanzeigen", created=NOW, key="RTX 4090"):
    cfg, fx = make_cfg(), FakeFx()
    inp_ = net_inputs(price)
    res = asyncio.run(economics.net(inp_, cfg, fx, FakeCustoms()))
    lst = listing(source, sid, seller, price)
    ref = st.upsert_listing(lst)
    ev = cards.build_evidence(inp_, lst, estimate(), risk(), fx_rate=P("1"), fx_date=D0, buy_pln=P(price) + 0)
    d = Deal(id=None, listing_ref=ref, key=key, tier=2, star=False, effort="easy", confidence="medium",
             cons_net_pln=res.cons_net_pln, exp_net_pln=res.exp_net_pln, cons_net_usd=P("22.40"),
             exp_net_usd=P("51.53"), capital_usd=P("553"), breakdown_json=cards.breakdown_json(res),
             evidence_json=ev, reasons=["clears the easy floor"], created_at=created)
    d.id = st.save_deal(d)
    return d, res, lst
