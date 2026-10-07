"""Retail shop price watch.

A list of (shop, url) pairs from `config/sources.yaml` (`retail.watch`). Each URL is a product page or a category
page whose HTML carries schema.org JSON-LD `Product` nodes with `offers.price` (a category page may hold an
ItemList of Products). Every page is fetched through the polite Fetcher (robots.txt, pacing, backoff).

For each product with a recognised key, one scan:
  * records the new-retail price as a `retail_new` comp (the ceiling used by `valuation.estimate`), and
  * emits a Listing only when the price fell >= `drop_pct` (10%) below the last seen price of that shop item, or is
    below the clears-at P25 (`valuation.estimate`) of its key.
Last seen prices live in the `retail_prices` table. The first sighting of an item never counts as a drop.
Only PLN prices are used (the configured shops are Polish).
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from decimal import Decimal

from ..models import CompRow, Listing
from .base import Adapter, AdapterError, Query, filter_query, norm, now_utc, price_of

SCHEMA = ("create table if not exists retail_prices (shop text not null, ref text not null, key text, "
          "price text not null, currency text not null, seen_at text not null, primary key (shop, ref))")
LD_RE = re.compile(r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", re.S | re.I)
P25_CHANNEL_ORDER = ("allegro", "olx", "ebay_pl")  # PL exit channels, in order of preference
SCHEMA_EMITTED = ("create table if not exists retail_emitted (shop text not null, ref text not null, "
                  "price text not null, primary key (shop, ref, price))")
OUT_OF_STOCK = ("outofstock", "discontinued", "soldout")


def pick_p25_channel(enabled) -> str | None:
    """First of allegro, olx, ebay_pl that is an enabled exit channel; None = skip the P25 check."""
    en = set(enabled or ())
    return next((c for c in P25_CHANNEL_ORDER if c in en), None)


def configured_exit_channels() -> list[str]:
    from pathlib import Path
    from ..config import load
    from .base import ROOT
    try:
        return list(load(Path(ROOT)).economics.exit_channels.enabled)
    except (AttributeError, KeyError):
        return []


def _walk(o, out: list[dict]) -> None:
    if isinstance(o, dict):
        t = o.get("@type")
        if t == "Product" or (isinstance(t, list) and "Product" in t):
            out.append(o)
        for v in o.values():
            _walk(v, out)
    elif isinstance(o, list):
        for v in o:
            _walk(v, out)


def parse_products(html: str, page_url: str) -> list[dict]:
    """Product dicts {name, url, price, currency, available, sku} from the JSON-LD of a page (price > 0 only)."""
    nodes: list[dict] = []
    for raw in LD_RE.findall(html):
        try:
            _walk(json.loads(raw.strip(), parse_float=Decimal), nodes)
        except ValueError:
            continue
    out, seen = [], set()
    for p in nodes:
        offers = p.get("offers")
        offer = next((o for o in (offers if isinstance(offers, list) else [offers]) if isinstance(o, dict)), None)
        if offer is None or not p.get("name"):
            continue
        raw_price = offer.get("price", offer.get("lowPrice"))
        pr = price_of(f"{raw_price} {offer.get('priceCurrency') or 'PLN'}", "PLN") if raw_price is not None else None
        if pr is None:
            continue
        url = p.get("url") or (page_url if len(nodes) == 1 else None)
        if not url or url in seen:
            continue
        seen.add(url)
        avail = str(offer.get("availability") or "").lower()
        out.append({"name": norm(p["name"]), "url": url, "price": pr[0], "currency": pr[1],
                    "available": not any(m in avail for m in OUT_OF_STOCK), "sku": str(p.get("sku") or p.get("productID") or "")})
    return out


class RetailWatch(Adapter):
    name = "retail"
    domain = "retail"
    country = "PL"
    currency = "PLN"

    def __init__(self, watch: list[dict] | None = None, drop_pct: float = 0.10,
                 exit_channels: list[str] | None = None):
        self.watch = [w for w in (watch or []) if w.get("shop") and w.get("url")]
        self.drop = Decimal(str(drop_pct))
        self._exit_channels = exit_channels

    @property
    def estimate_channel(self) -> str | None:
        """PL exit channel used for the clears-at P25 (economics exit_channels.enabled, no literals)."""
        if self._exit_channels is None:
            self._exit_channels = configured_exit_channels()
        return pick_p25_channel(self._exit_channels)

    async def search(self, query: Query, fetcher, to_pln=None) -> list[Listing]:
        if not self.watch:
            return []
        return filter_query(await self.scan(fetcher, fetcher.store), query, to_pln or self.to_pln, text=True)

    def parse_detail(self, listing: Listing, html: str) -> Listing:
        return listing

    async def scan(self, fetcher, store, now: datetime | None = None, estimate=None) -> list[Listing]:
        """Fetch every watched page, ingest retail_new comps, return the drop / below-P25 listings."""
        from ..match import rules_extract
        from ..valuation import estimate as default_estimate, ingest_comps
        estimate = estimate or default_estimate
        store.ensure_table(SCHEMA)
        store.ensure_table(SCHEMA_EMITTED)
        p25_channel = self.estimate_channel
        now = now or now_utc()
        listings: list[Listing] = []
        comps: list[CompRow] = []
        failures: list[str] = []
        for w in self.watch:
            shop, url = w["shop"], w["url"]
            try:
                res = await self._get(fetcher, url)
            except AdapterError as e:
                failures.append(str(e))
                continue
            if res.status != 200:
                failures.append(f"{self.name}: {shop} status {res.status}")
                continue
            for p in parse_products(res.text, url):
                if p["currency"] != "PLN" or not p["available"]:
                    continue
                ref = p["sku"] or hashlib.sha1(p["url"].encode()).hexdigest()[:12]
                ex = rules_extract(p["name"], "", "pl")
                key = ex.key if ex.key and not (ex.box_only or ex.for_parts or ex.wanted_ad or ex.bundle_items) else None
                last = store.conn.execute("select price from retail_prices where shop=? and ref=?", (shop, ref)).fetchone()
                reasons = []
                if last is not None and p["price"] <= Decimal(last["price"]) * (1 - self.drop):
                    reasons.append(f"dropped from {last['price']} PLN")
                if key and w.get("kind", "new") == "new":
                    est = estimate(store, key, None, p25_channel, now) if p25_channel else None
                    if est is not None and p["price"] < est.p25_pln:
                        reasons.append(f"below clears-at P25 {est.p25_pln} PLN")
                    comps.append(CompRow(key=key, condition="new", channel=shop, kind="retail_new", price=p["price"],
                                         currency="PLN", at=now, weight=1.0, ref=f"retail:{shop}:{ref}:{now.date()}",
                                         price_pln=p["price"], country="PL"))
                store.conn.execute("insert or replace into retail_prices (shop, ref, key, price, currency, seen_at) "
                                   "values (?,?,?,?,?,?)", (shop, ref, key, str(p["price"]), "PLN", now.isoformat()))
                store.conn.commit()
                if reasons and key and store.conn.execute(
                        "select 1 from retail_emitted where shop=? and ref=? and price=?", (shop, ref, str(p["price"]))).fetchone():
                    reasons = []  # this shop item at this price was already emitted
                if reasons and key:
                    store.conn.execute("insert into retail_emitted (shop, ref, price) values (?,?,?)", (shop, ref, str(p["price"])))
                    store.conn.commit()
                    listings.append(self.make(
                        source_id=f"{shop}:{ref}", url=p["url"], title=p["name"], price=p["price"], currency="PLN",
                        description=f"{w.get('kind', 'new')} retail price at {shop}; " + "; ".join(reasons),
                        is_business=True, ships=True, seen_at=now))
        await ingest_comps(store, comps, None)
        if failures and not listings and not comps:
            raise AdapterError("; ".join(failures[:3]))
        return listings
