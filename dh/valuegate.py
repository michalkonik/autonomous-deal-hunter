"""Value gate: the evidence-and-edge screen between pricing and saving (owner directive 2026-11-10).

The overnight batch taught the lesson this module encodes: when a key has no sold comparables the exit
estimate falls back to the retail ceiling, and a plain market-price listing then tiers as a deal. The gate
keeps a tier only when BOTH sides hold:

  evidence  - at least `min_sold` sold comparables (newest within `sold_recent_days`), or a buy price at
              most `retail_discount` of the cheapest recent new-retail price; and
  edge      - at most `max_cheaper` genuinely-cheaper live asks (asking within `cheaper_tolerance` of the
              buy counts as equal, not cheaper), or a buy at most `deep_discount` of the cleaned ask median.

Everything else goes to the watchlist (tier 5): stored and shown, never pushed, never re-fetched. An ask at
or above `market_ask_ratio` of the cleaned ask median with `market_min_cheaper` listings cheaper (and, when
the key has one, at or above `market_retail_ratio` of the retail floor) is market-price noise on a liquid
model and is dropped outright. A buy below `junk_fraction` of the pool reference is a variation trap
(brackets and fans keyed to a flagship card) and is dropped the same way. Asking prices below
`junk_fraction` of the pool reference are excluded from every statistic first - they are what made liquid
models look scarce.

Config lives in config/valuation.yaml under `gate:` (not part of the frozen layer)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from statistics import median
from typing import Any

from .valuation import _cond_ok

SOLD_KINDS = ("owner_sale", "ebay_sold", "probably_sold")


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

    @classmethod
    def from_valuation(cls, valuation_cfg: Any) -> "GateParams":
        """`valuation_cfg`: the Config node or plain dict of config/valuation.yaml; no `gate:` section = defaults."""
        node = valuation_cfg.get("gate") if hasattr(valuation_cfg, "get") else None
        if node is None:
            return cls()
        g = dict(node.to_dict() if hasattr(node, "to_dict") else node)
        for k in ("enabled", "min_sold", "sold_recent_days", "max_cheaper", "market_min_cheaper",
                  "min_real_asks", "lookback_days"):
            if k in g:
                g[k] = bool(g[k]) if k == "enabled" else int(g[k])
        for k in ("retail_discount", "cheaper_tolerance", "deep_discount", "junk_fraction",
                  "market_ask_ratio", "market_retail_ratio"):
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
            if at >= ask_since:
                prev = asks.get(r["ref"])
                if prev is None or price < prev:
                    asks[r["ref"]] = price
            continue
        if r["kind"] in SOLD_KINDS:
            sold_refs.add(r["ref"])
            if at >= sold_from:
                sold_recent = True
    md = MarketData(retail_floor=retail, n_sold=len(sold_refs), sold_recent=sold_recent)
    raw = list(asks.values())
    if raw:
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


def verdict(buy_pln: Decimal, md: MarketData, p: GateParams) -> Verdict:
    """Pure decision on collected market data; the scheduler maps actions to tiers and counters."""
    if not p.enabled:
        return Verdict("keep", "gate_off", "value gate off")
    med = md.ask_median
    pool_ref = max(md.retail_floor or Decimal(0), med or Decimal(0))
    if pool_ref and buy_pln < p.junk_fraction * pool_ref:
        return Verdict("drop", "junk_ask",
                       f"variation trap: {buy_pln} vs pool reference {pool_ref}")
    n_cheap = sum(1 for a in md.asks if a < buy_pln * (1 - p.cheaper_tolerance))
    evidence_sold = md.n_sold >= p.min_sold and md.sold_recent
    evidence_retail = md.retail_floor is not None and buy_pln <= p.retail_discount * md.retail_floor
    edge_scarce = n_cheap <= p.max_cheaper
    edge_deep = med is not None and buy_pln <= p.deep_discount * med
    if (evidence_sold or evidence_retail) and (edge_scarce or edge_deep):
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
