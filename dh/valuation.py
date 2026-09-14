"""Exit valuation: a weighted price distribution per product key, condition and exit channel (spec §6.2).

Sources of comparables (all in PLN; non-PLN rows are converted at the cached NBP rate of the comp date):
- `comps` table (Store.comps): owner sales, eBay sold, eBay active asks (via `ingest_comps`), seeded ask history,
  and `retail_new` prices, which are not comparables but set the new-retail ceiling;
- the clears-at ledger: `ledger.clears_at` (probably sold) and `ledger.active_asks` (tracked live listings).

Weights (config/valuation.yaml): owner_sale 1.0, ebay_sold 1.0, probably_sold 0.5, active_ask 0.3 (price × the
ask-to-sale ratio, 0.92), ask_history 0.2.

Channel: the exit channel's own comps when it has any from the last `recent_days`; otherwise a fallback pool with
weights × `fallback_factor` and the note "fallback": all PL comps for a PL channel, all other-EU comps for an EU
channel. A channel with an unknown market has no fallback.

Only comps from the last `recent_days` (30) make the quantiles, `n_effective`, `n_sold` and `comps`; with none the
estimate is None. Comps 30–60 days old are used only for `trend_30d` = weighted median(recent) / weighted
median(30–60 d) − 1. The quantiles are capped at the new-retail ceiling: lowest `retail_new` price of the last
`recent_days` (or `retail_new_pln`) × 0.95.

Quantiles use midpoint interpolation: each value sits at the middle of its weight on the cumulative-weight axis.
With equal weights this reproduces the ordinary median.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from statistics import median
from typing import Iterable

from . import config as dhconfig
from . import ledger
from .models import CompRef, CompRow
from .money import q2
from .store import Store, _iso

ROOT = Path(__file__).resolve().parents[1]
SOLD_KINDS = frozenset({"owner_sale", "ebay_sold", "probably_sold"})
EU_MARKETS = frozenset({"AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT",
                        "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE"})


@dataclass(frozen=True)
class Params:
    ask_to_sale_ratio: Decimal = Decimal("0.92")
    weights: dict = field(default_factory=lambda: {"owner_sale": 1.0, "ebay_sold": 1.0, "probably_sold": 0.5,
                                                   "active_ask": 0.3, "ask_history": 0.2})
    fallback_factor: float = 0.7
    ceiling_factor: Decimal = Decimal("0.95")
    recent_days: int = 30
    lookback_days: int = 90
    active_ask_max_age_days: int = 3
    fx_max_back_days: int = 10

    @classmethod
    def from_config(cls, cfg: dhconfig.Config) -> "Params":
        v = cfg.get("valuation")
        if v is None:
            return cls()
        d = v.to_dict()
        base = cls()
        return cls(
            ask_to_sale_ratio=Decimal(str(d.get("ask_to_sale_ratio", base.ask_to_sale_ratio))),
            weights={**base.weights, **{k: float(x) for k, x in (d.get("weights") or {}).items()}},
            fallback_factor=float(d.get("fallback_factor", base.fallback_factor)),
            ceiling_factor=Decimal(str(d.get("ceiling_factor", base.ceiling_factor))),
            recent_days=int(d.get("recent_days", base.recent_days)),
            lookback_days=int(d.get("lookback_days", base.lookback_days)),
            active_ask_max_age_days=int(d.get("active_ask_max_age_days", base.active_ask_max_age_days)),
            fx_max_back_days=int(d.get("fx_max_back_days", base.fx_max_back_days)),
        )


class Channels:
    """Source → comparable channel, and channel → market (ISO country)."""

    def __init__(self, sources: dict[str, str], markets: dict[str, str]):
        self.sources, self.markets = dict(sources), {k: v.upper() for k, v in markets.items()}

    @classmethod
    def from_config(cls, cfg: dhconfig.Config) -> "Channels":
        v = cfg.get("valuation")
        entries = (v.to_dict().get("channels") or {}) if v is not None else {}
        sources = {s: e.get("channel", s) for s, e in entries.items()}
        markets = {}
        for s, e in entries.items():
            if e.get("market"):
                markets[s] = e["market"]
                markets[sources[s]] = e["market"]
        src_cfg = cfg.get("sources")
        adapters = (src_cfg.to_dict().get("adapters") or {}) if src_cfg is not None else {}
        for s, a in adapters.items():
            if isinstance(a, dict) and a.get("channel"):
                sources[s] = a["channel"]
                if s in markets and a["channel"] not in markets:
                    markets[a["channel"]] = markets[s]
        return cls(sources, markets)

    def channel_of(self, source: str) -> str:
        return self.sources.get(source, source)

    def market_of(self, name: str) -> str | None:
        return self.markets.get(name) or self.markets.get(self.channel_of(name))


@lru_cache(maxsize=1)
def _repo_config() -> dhconfig.Config:
    return dhconfig.load(ROOT)


def default_params() -> Params:
    return Params.from_config(_repo_config())


def default_channels() -> Channels:
    return Channels.from_config(_repo_config())


@dataclass
class ExitEstimate:
    channel: str
    key: str
    condition: str | None
    p25_pln: Decimal
    median_pln: Decimal
    p75_pln: Decimal
    n_effective: float
    n_sold: int
    newest_days: float
    days_to_sell: float | None
    trend_30d: float | None
    ceiling_pln: Decimal | None
    comps: list[CompRef]
    notes: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ quantiles


def weighted_quantile(values: list[Decimal], weights: list[float], q: float) -> Decimal:
    """Weighted quantile, midpoint interpolation (each value at the centre of its weight). q in [0, 1]."""
    if not values or len(values) != len(weights):
        raise ValueError("weighted_quantile needs equally long, non-empty values and weights")
    pairs = sorted((Decimal(v), Decimal(str(w))) for v, w in zip(values, weights))
    if any(w <= 0 for _, w in pairs):
        raise ValueError("weights must be positive")
    total = sum(w for _, w in pairs)
    qd = Decimal(str(q))
    pos, cum = [], Decimal(0)
    for _, w in pairs:
        pos.append((cum + w / 2) / total)
        cum += w
    if qd <= pos[0]:
        return pairs[0][0]
    if qd >= pos[-1]:
        return pairs[-1][0]
    for i in range(len(pairs) - 1):
        if pos[i] <= qd <= pos[i + 1]:
            lo, hi = pairs[i][0], pairs[i + 1][0]
            return lo + (hi - lo) * (qd - pos[i]) / (pos[i + 1] - pos[i])
    raise AssertionError("unreachable")


# ------------------------------------------------------------------ FX (cached rates only; sync)


def cached_rate(store: Store, ccy: str, day: date, max_back_days: int = 10) -> Decimal | None:
    """PLN per unit from the `fx` cache (filled by money.Fx), for `day` or up to `max_back_days` before it."""
    ccy = ccy.upper()
    if ccy == "PLN":
        return Decimal(1)
    r = store.conn.execute("select rate_pln from fx where currency=? and date<=? and date>=? order by date desc limit 1",
                           (ccy, day.isoformat(), (day - timedelta(days=max_back_days)).isoformat())).fetchone()
    return Decimal(r["rate_pln"]) if r else None


async def warm_fx(store: Store, fx, key: str, condition: str | None, day: date | datetime,
                  channels: Channels | None = None) -> int:
    """Fetch (via money.Fx, which caches) every rate an `estimate` for `key` will need. Returns rates fetched."""
    now = _now(day)
    p = default_params()
    rows = _ledger_rows(store, key, condition, now, p, channels or default_channels(), include_listing_asks=True)
    need = {(r.currency.upper(), r.at.date()) for r in rows if r.price_pln is None and r.currency.upper() != "PLN"}
    for ccy, d in sorted(need):
        await fx.rate_with_date(ccy, d)
    return len(need)


async def ingest_comps(store: Store, rows: Iterable[CompRow], fx) -> int:
    """Persist CompRows (e.g. `EbayBrowse.active_comps(key)`) into `comps`, in PLN at the comp-date rate.

    Asks are stored at the asking price; the ask-to-sale ratio is applied at estimate time. Idempotent by
    (kind, ref). Returns the number of rows added.
    """
    added = 0
    for r in rows:
        if store.conn.execute("select 1 from comps where kind=? and ref=?", (r.kind, r.ref)).fetchone():
            continue
        if r.price_pln is not None:
            pln = q2(r.price_pln)
        elif r.currency.upper() == "PLN":
            pln = q2(r.price)
        else:
            pln = q2(r.price * await fx.rate(r.currency.upper(), r.at.date()))
        store.add_comp(r.key, r.condition, r.channel, pln, r.at, r.kind, r.ref)
        added += 1
    return added


# ------------------------------------------------------------------ estimate


def _now(day: date | datetime) -> datetime:
    if isinstance(day, datetime):
        _iso(day)  # rejects naive datetimes
        return day.astimezone(timezone.utc)
    return datetime.combine(day, time(23, 59, 59), tzinfo=timezone.utc)


def _ledger_rows(store: Store, key: str, condition: str | None, now: datetime, p: Params, channels: Channels,
                 include_listing_asks: bool) -> list[CompRow]:
    since = now - timedelta(days=p.lookback_days)
    rows = [r for r in ledger.clears_at(store, key, condition, since, channel_of=channels.channel_of) if r.at <= now]
    if include_listing_asks:
        rows += ledger.active_asks(store, key, condition, now, channel_of=channels.channel_of,
                                   max_age=timedelta(days=p.active_ask_max_age_days))
    return rows


def _cond_ok(have: str | None, want: str | None) -> bool:
    if want is None:
        return have != "for_parts"
    if want in ledger.USED_LIKE:
        return have is None or have in ledger.USED_LIKE
    return have == want


def _same_region(market: str | None, target: str) -> bool:
    if market is None:
        return False
    if target == "PL":
        return market == "PL"
    if target in EU_MARKETS:
        return market in EU_MARKETS and market != "PL"
    return market == target


def _wmedian(rows: list[tuple[Decimal, float]]) -> Decimal:
    return weighted_quantile([v for v, _ in rows], [w for _, w in rows], 0.5)


def estimate(store: Store, key: str, condition: str | None, channel: str, day: date | datetime, *,
             params: Params | None = None, channels: Channels | None = None,
             exclude_listing_ids: Iterable[int] = (), retail_new_pln: Decimal | None = None,
             include_listing_asks: bool = True) -> ExitEstimate | None:
    """Exit-price distribution for `key` in `condition` (None = any) on exit `channel`, as of `day`.

    `day` is a date (valued as of its end, UTC) or an aware datetime. None when no comparable from the last
    `recent_days` exists for the channel or its fallback pool.
    """
    p = params or default_params()
    ch = channels or default_channels()
    now = _now(day)
    since = now - timedelta(days=p.lookback_days)
    recent_from = now - timedelta(days=p.recent_days)
    trend_from = now - timedelta(days=2 * p.recent_days)
    notes: list[str] = []

    rows: list[CompRow] = []
    retail: list[Decimal] = []
    for r in store.comps(key, since=since):
        at = datetime.fromisoformat(r["sold_at"])
        if at > now:
            continue
        if r["kind"] == "retail_new":
            if at >= recent_from:
                retail.append(Decimal(r["price_pln"]))
            continue
        if not _cond_ok(r["condition"], condition):
            continue
        if r["kind"] not in p.weights:
            continue
        price = Decimal(r["price_pln"])
        rows.append(CompRow(key=key, condition=r["condition"], channel=r["channel"], kind=r["kind"], price=price,
                            currency="PLN", at=at, weight=p.weights[r["kind"]], ref=r["ref"] or f"comp:{r['id']}",
                            price_pln=price))
    rows += _ledger_rows(store, key, condition, now, p, ch, include_listing_asks)
    excluded = set(exclude_listing_ids)
    seen: set[tuple[str, str]] = set()
    uniq = []
    for r in rows:
        if r.listing_id is not None and r.listing_id in excluded:
            continue
        if (r.kind, r.ref) in seen:
            continue
        seen.add((r.kind, r.ref))
        uniq.append(r)
    rows = uniq

    # channel: own recent comps, else the regional fallback pool
    factor, fallback = 1.0, False
    pool = [r for r in rows if r.channel == channel]
    if not any(r.at >= recent_from for r in pool):
        target = ch.market_of(channel)
        pool = [] if target is None else [r for r in rows if _same_region(ch.market_of(r.channel), target)]
        factor, fallback = p.fallback_factor, True

    # PLN, ask discount, effective weight
    used: list[tuple[CompRow, Decimal, float]] = []
    missing: set[str] = set()
    for r in pool:
        pln = r.price_pln
        if pln is None:
            rate = cached_rate(store, r.currency, r.at.date(), p.fx_max_back_days)
            if rate is None:
                missing.add(r.currency.upper())
                continue
            pln = q2(r.price * rate)
        if r.kind == "active_ask":
            pln = q2(pln * p.ask_to_sale_ratio)
        used.append((r, pln, round(p.weights.get(r.kind, r.weight) * factor, 4)))

    recent = [u for u in used if u[0].at >= recent_from]
    if not recent:
        return None
    if fallback:
        notes.append("fallback")
    notes += [f"fx_missing:{c}" for c in sorted(missing)]

    vals = [pln for _, pln, _ in recent]
    ws = [w for _, _, w in recent]
    p25, p50, p75 = (q2(weighted_quantile(vals, ws, q)) for q in (0.25, 0.5, 0.75))

    if retail_new_pln is not None:
        retail.append(Decimal(retail_new_pln))
    ceiling = q2(min(retail) * p.ceiling_factor) if retail else None
    if ceiling is not None and p75 > ceiling:
        notes.append("capped_at_ceiling")
        p25, p50, p75 = (min(x, ceiling) for x in (p25, p50, p75))

    older = [(pln, w) for r, pln, w in used if trend_from <= r.at < recent_from]
    trend = None
    if older:
        trend = round(float(_wmedian(list(zip(vals, ws))) / _wmedian(older) - 1), 4)

    dts = [r.days_to_sell for r, _, _ in recent if r.kind == "probably_sold" and r.days_to_sell is not None]
    newest = max(r.at for r, _, _ in recent)
    comps = [CompRef(kind=r.kind, channel=r.channel, price_pln=pln, at=r.at, weight=w, ref=r.ref, fallback=fallback)
             for r, pln, w in sorted(recent, key=lambda u: u[0].at, reverse=True)]
    return ExitEstimate(
        channel=channel, key=key, condition=condition, p25_pln=p25, median_pln=p50, p75_pln=p75,
        n_effective=round(sum(ws), 4), n_sold=sum(1 for r, _, _ in recent if r.kind in SOLD_KINDS),
        newest_days=round((now - newest).total_seconds() / 86400, 4),
        days_to_sell=median(dts) if dts else None, trend_30d=trend, ceiling_pln=ceiling, comps=comps, notes=notes)
