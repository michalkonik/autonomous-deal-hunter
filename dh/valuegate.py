"""Value gate: the evidence-and-edge screen between pricing and saving (owner directive 2026-11-10).

The overnight batch taught the lesson this module encodes: when a key has no sold comparables the exit
estimate falls back to the retail ceiling, and a plain market-price listing then tiers as a deal. The gate
keeps a tier only when BOTH sides hold:

  evidence  - at least `min_sold` sold comparables (newest within `sold_recent_days`), or a buy price at
              most `retail_discount` of the cheapest recent new-retail price; and
  edge      - at most `max_cheaper` genuinely-cheaper live asks (asking within `cheaper_tolerance` of the
              buy counts as equal, not cheaper), or a buy at most `deep_discount` of the cleaned ask median.

The ask pool is the comps table plus the live listing asks (`ledger.active_asks`). A comps table that holds
only retail rows is not an empty market.

A keep also has to be worth the capital. The conservative net must be at least `min_return_on_capital` of
the capital at risk (owner directive 2026-11-12: the gap has to be real and large enough to justify buying
and reselling). A thinner gap is a watch. `min_return_on_capital` of 0 turns that check off. The absolute
floors in config/rules.yaml still apply on their own.

A new item with no receipt and no guarantee has a further bar (`paperless_new_below_shop`): the buy must be
strictly under the same model's shop price. At or above that price, or with no shop price on file, it is a
watch. A stated receipt or a stated guarantee is enough to skip that bar.

Everything else goes to the watchlist (tier 5): stored and shown, never pushed, never re-fetched. An ask at
or above `market_ask_ratio` of the cleaned ask median with `market_min_cheaper` listings cheaper (and, when
the key has one, at or above `market_retail_ratio` of the retail floor) is market-price noise on a liquid
model and is dropped outright. A buy below `junk_fraction` of the pool reference is a variation trap
(brackets and fans keyed to a flagship card) and is dropped the same way. Asking prices below
`junk_fraction` of the pool reference are excluded from every statistic first - they are what made liquid
models look scarce.

Config lives in config/valuation.yaml under `gate:` (not part of the frozen layer)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from statistics import median
from typing import Any

from . import ledger
from .money import q2
from .valuation import _cond_ok, cached_rate, default_params, sellable_asks

SOLD_KINDS = ("owner_sale", "ebay_sold", "probably_sold")
_EBAY_ITEM = re.compile(r"(?:/itm/|ebay:)(\d+)")
# Where a Polish reseller can actually list the same item. A German repair quote is not that price.
PL_BENCH_CHANNELS = frozenset({"allegro", "allegro_lokalnie", "olx"})


def _ask_key(ref: str) -> str:
    """One benchmark listing is one ask, whether the ref is an item URL or an `ebay:<id>` comp."""
    m = _EBAY_ITEM.search(ref or "")
    return f"ebay:{m.group(1)}" if m else (ref or "")


def _remember_ask(asks: dict[str, tuple[Decimal, str]], ref: str, price: Decimal, channel: str) -> None:
    key = _ask_key(ref)
    prev = asks.get(key)
    if prev is None or price < prev[0]:
        asks[key] = (price, channel or "")


@dataclass(frozen=True)
class GateParams:
    enabled: bool = True
    min_sold: int = 3
    sold_recent_days: int = 180
    retail_discount: Decimal = Decimal("0.85")
    max_cheaper: int = 2
    cheaper_tolerance: Decimal = Decimal("0.05")
    deep_discount: Decimal = Decimal("0.75")
    junk_fraction: Decimal = Decimal("0.25")
    market_ask_ratio: Decimal = Decimal("0.95")
    market_retail_ratio: Decimal = Decimal("0.90")
    market_min_cheaper: int = 10
    min_real_asks: int = 5
    lookback_days: int = 90
    min_return_on_capital: Decimal = Decimal("0.10")
    min_net_usd: Decimal = Decimal("200")
    paperless_new_below_shop: bool = True
    min_under_low_ask: Decimal = Decimal("0.10")

    @classmethod
    def from_valuation(cls, valuation_cfg: Any) -> "GateParams":
        """`valuation_cfg`: the Config node or plain dict of config/valuation.yaml; no `gate:` section = defaults."""
        node = valuation_cfg.get("gate") if hasattr(valuation_cfg, "get") else None
        if node is None:
            return cls()
        g = dict(node.to_dict() if hasattr(node, "to_dict") else node)
        for k in ("enabled", "paperless_new_below_shop", "min_sold", "sold_recent_days", "max_cheaper",
                  "market_min_cheaper", "min_real_asks", "lookback_days"):
            if k in g:
                g[k] = bool(g[k]) if k in ("enabled", "paperless_new_below_shop") else int(g[k])
        for k in ("retail_discount", "cheaper_tolerance", "deep_discount", "junk_fraction",
                  "market_ask_ratio", "market_retail_ratio", "min_return_on_capital", "min_under_low_ask",
                  "min_net_usd"):
            if k in g:
                g[k] = Decimal(str(g[k]))
        return cls(**g)


@dataclass
class MarketData:
    asks: list[Decimal] = field(default_factory=list)  # junk-cleaned, deduped by ref, condition-matched
    retail_floor: Decimal | None = None
    n_sold: int = 0
    sold_recent: bool = False
    n_junk: int = 0

    @property
    def ask_median(self) -> Decimal | None:
        return median(self.asks) if self.asks else None


def _ref_ok(store, ref: str, key: str, condition: str | None) -> bool:
    """A comp with no stored title is kept. One whose listing is a repair, the wrong model, or for-parts is not."""
    if not ref:
        return True
    row = store.conn.execute(
        "select title, description from listings where url=? or source_id=? limit 1", (ref, ref)).fetchone()
    if row is None:
        return True
    return ledger._benchmark_ok(row["title"] or "", row["description"] or "", key, condition)


def collect(store, key: str, condition: str | None, now: datetime, p: GateParams) -> MarketData:
    """Raw key-scoped comparables, cleaned the way the gate needs them. The estimate's pool fallback is
    deliberately not used: a cross-model pool cannot show whether THIS model is scarce."""
    since = now - timedelta(days=max(p.lookback_days, p.sold_recent_days))
    ask_since = now - timedelta(days=p.lookback_days)
    sold_from = now - timedelta(days=p.sold_recent_days)
    asks: dict[str, Decimal] = {}
    retail: Decimal | None = None
    sold_refs: set[str] = set()
    sold_recent = False
    for r in store.comps(key, since=since):
        try:
            at = datetime.fromisoformat(r["sold_at"])
            price = Decimal(r["price_pln"])
        except (TypeError, ValueError):
            continue
        if r["kind"] == "retail_new":  # the retail floor ignores condition, like the estimator's ceiling does
            if at >= ask_since and (retail is None or price < retail):
                retail = price
            continue
        if not _cond_ok(r["condition"], condition):
            continue
        if r["kind"] == "active_ask":
            if at >= ask_since and _ref_ok(store, r["ref"], key, condition):
                _remember_ask(asks, r["ref"], price, r["channel"] or "")
            continue
        if r["kind"] in SOLD_KINDS:
            sold_refs.add(r["ref"])
            if at >= sold_from:
                sold_recent = True
    fx_back = default_params().fx_max_back_days
    for row in ledger.active_asks(store, key, condition, now):
        price = _listing_ask_pln(store, row, fx_back)
        if price is not None:
            _remember_ask(asks, row.ref, price, row.channel or "")
    md = MarketData(retail_floor=retail, n_sold=len(sold_refs), sold_recent=sold_recent)
    pl = [price for price, channel in asks.values() if channel in PL_BENCH_CHANNELS]
    rest = [price for price, _channel in asks.values()]
    # A Polish ask is the resale benchmark. Other countries are used only when Poland has none.
    raw = pl if pl else rest
    if raw:
        vp = default_params()
        kept = sellable_asks(raw, band=vp.sellable_band, junk=vp.sellable_junk)
        # a lone accessory still has to clear the old junk floor when the cluster rule kept it
        # (two prices: sellable_asks returns both, and a 500 PLN cable must not price a GPU)
        if len(raw) >= 3:
            md.asks = kept
        else:
            pool_ref = max(retail or Decimal(0), median(raw))
            floor = pool_ref * p.junk_fraction
            md.asks = sorted(a for a in raw if a >= floor)
        md.n_junk = len(raw) - len(md.asks)
    return md


@dataclass(frozen=True)
class Verdict:
    action: str  # "keep" | "watch" | "drop"
    code: str  # stats counter / card tag
    note: str  # shown in the card's "why this tier"


def _listing_ask_pln(store, row, max_back_days: int) -> Decimal | None:
    """The asking price in PLN. A foreign ask with no cached rate is left out rather than compared raw."""
    ccy = (row.currency or "PLN").upper()
    if ccy == "PLN":
        return row.price
    if row.price_pln is not None:
        return row.price_pln
    got = cached_rate(store, ccy, row.at.date(), max_back_days)
    if got is None:
        return None
    return q2(row.price * got[0])


def buy_pln(inp, fxr=None) -> Decimal:
    """The buy in PLN. A foreign price uses a rate the appraisal already fetched; without one the native
    amount is returned and the caller is comparing like with like only when the listing is already in PLN."""
    price = Decimal(inp.buy_price)
    ccy = str(getattr(inp, "buy_ccy", None) or "PLN").upper()
    if ccy == "PLN":
        return price
    served = getattr(fxr, "served", None) or {}
    day = getattr(inp, "day", None)
    hit = served.get((ccy, day))
    if hit is None:
        hit = next((v for (c, _), v in served.items() if c == ccy), None)
    if hit is None:
        return price
    return q2(price * Decimal(hit[0]))


def paperless_new_watch(buy: Decimal, retail: Decimal | None, has_papers: bool, p: GateParams) -> Verdict | None:
    """A watch when a new item has no receipt and no guarantee and is not strictly under the shop price.
    None when the listing has papers, the buy is cheaper than the shops, or the check is off."""
    if not p.enabled or not p.paperless_new_below_shop or has_papers:
        return None
    if retail is None:
        return Verdict("watch", "no_shop_price",
                       "new without receipt or guarantee: no shop price for this model")
    if buy >= retail:
        return Verdict("watch", "paperless_above_shop",
                       f"new without receipt or guarantee: {buy} is not below the shop price {retail}")
    return None


def small_deal(cons_usd, p: GateParams) -> Verdict | None:
    """Drop when the conservative net is known and under `min_net_usd`. None when the floor is off,
    the net is unknown, or the net clears it. The owner does not want these stored as watches."""
    if not p.enabled or p.min_net_usd <= 0 or cons_usd is None:
        return None
    net = Decimal(str(cons_usd))
    if net >= p.min_net_usd:
        return None
    return Verdict("drop", "small_deal",
                   f"small deal: conservative net {net} USD is under the {p.min_net_usd} USD floor")


def thin_margin(cons_usd, capital_usd, p: GateParams) -> Verdict | None:
    """A watch when the conservative net is below `min_return_on_capital` of the capital. None when the gap
    is large enough, the check is off, or the caller did not pass both figures."""
    if not p.enabled or p.min_return_on_capital <= 0 or cons_usd is None or capital_usd is None:
        return None
    cap = Decimal(str(capital_usd))
    net = Decimal(str(cons_usd))
    if cap <= 0 or net >= p.min_return_on_capital * cap:
        return None
    pct = (net / cap * Decimal(100)).quantize(Decimal("0.1"))
    need = (p.min_return_on_capital * Decimal(100)).quantize(Decimal("0.1"))
    return Verdict("watch", "thin_margin",
                   f"thin margin: conservative net {net} USD is {pct}% of {cap} USD capital; need {need}%")


def verdict(buy_pln: Decimal, md: MarketData, p: GateParams, *,
            cons_usd: Decimal | None = None, capital_usd: Decimal | None = None) -> Verdict:
    """Pure decision on collected market data; the scheduler maps actions to tiers and counters."""
    if not p.enabled:
        return Verdict("keep", "gate_off", "value gate off")
    if not md.asks and md.n_sold == 0 and md.retail_floor is None:
        # Nothing to compare with. Do not invent a tier; the caller keeps the appraisal.
        return Verdict("keep", "gate_no_data", "value gate: no market data")
    med = md.ask_median
    pool_ref = max(md.retail_floor or Decimal(0), med or Decimal(0))
    if pool_ref and buy_pln < p.junk_fraction * pool_ref:
        return Verdict("drop", "junk_ask",
                       f"variation trap: {buy_pln} vs pool reference {pool_ref}")
    n_cheap = sum(1 for a in md.asks if a < buy_pln * (1 - p.cheaper_tolerance))
    # One ask within tolerance of the buy is this listing, not a competing offer.
    others = list(md.asks)
    if buy_pln > 0:
        for i, ask in enumerate(others):
            if abs(ask - buy_pln) <= buy_pln * p.cheaper_tolerance:
                del others[i]
                break
    bench = others[0] if others else None
    evidence_sold = md.n_sold >= p.min_sold and md.sold_recent
    evidence_retail = md.retail_floor is not None and buy_pln <= p.retail_discount * md.retail_floor
    # With no sales to price the exit, the buy has to be under the cheapest other ask. Sales already
    # in hand are the exit; a couple of cheaper listings do not by themselves kill that.
    if (not evidence_sold and bench is not None and p.min_under_low_ask > 0
            and buy_pln > bench * (1 - p.min_under_low_ask)):
        if (med is not None and len(md.asks) >= p.min_real_asks and n_cheap >= p.market_min_cheaper
                and buy_pln >= p.market_ask_ratio * med
                and (md.retail_floor is None or buy_pln >= p.market_retail_ratio * md.retail_floor)):
            return Verdict("drop", "market_price",
                           f"market price: {buy_pln} vs ask median {med}, {n_cheap} cheaper asks")
        return Verdict("drop", "at_market",
                       f"at market: {buy_pln} is not {p.min_under_low_ask:.0%} under the low ask {bench}"
                       f" ({n_cheap} cheaper of {len(md.asks)})")
    if bench is None and md.asks and not evidence_sold and not evidence_retail:
        return Verdict("drop", "no_benchmark",
                       f"no competing ask for {buy_pln}; not priced against a real offer")
    edge_scarce = n_cheap <= p.max_cheaper
    edge_deep = med is not None and buy_pln <= p.deep_discount * med
    if (evidence_sold or evidence_retail) and (edge_scarce or edge_deep):
        small = small_deal(cons_usd, p)
        if small is not None:
            return small
        thin = thin_margin(cons_usd, capital_usd, p)
        if thin is not None:
            return thin
        why = " + ".join(w for w in (f"{md.n_sold} sold comps" if evidence_sold else None,
                                     f"{buy_pln} vs new at {md.retail_floor}" if evidence_retail else None,
                                     "scarce supply" if edge_scarce else "deep discount") if w)
        return Verdict("keep", "gate_keep", f"value gate: {why} ({n_cheap} cheaper of {len(md.asks)} asks)")
    if (med is not None and len(md.asks) >= p.min_real_asks and n_cheap >= p.market_min_cheaper
            and buy_pln >= p.market_ask_ratio * med
            and (md.retail_floor is None or buy_pln >= p.market_retail_ratio * md.retail_floor)):
        return Verdict("drop", "market_price",
                       f"market price: {buy_pln} vs ask median {med}, {n_cheap} cheaper asks")
    return Verdict("watch", "gate_watch",
                   f"value gate watch: {md.n_sold} sold, {n_cheap} cheaper of {len(md.asks)} asks"
                   f", retail floor {md.retail_floor}")
