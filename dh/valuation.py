"""Exit valuation: a weighted price distribution per product key, condition and exit channel (spec §6.2).

Sources of comparables (all in PLN; non-PLN rows are converted at the cached NBP rate of the comp date):
- `comps` table (Store.comps): owner sales, eBay sold, eBay active asks (via `ingest_comps`), seeded ask history,
  and `retail_new` prices, which are not comparables but set the new-retail ceiling;
- the clears-at ledger: `ledger.clears_at` (probably sold) and `ledger.active_asks` (tracked live listings).

All parameters come from config/valuation.yaml (no duplicated defaults in code). Weights: owner_sale 1.0,
ebay_sold 1.0, probably_sold 0.5, active_ask 0.3 (price × the ask-to-sale ratio, 0.92), ask_history 0.2.

Channel tiers, decided AFTER FX conversion (the first tier with a comp from the last `recent_days` wins):
- PL exit channel: own channel → all PL comps × 0.7 ("fallback_pl") → all EU comps incl. PL × 0.5 ("fallback_eu");
- EU exit channel: own channel → all EU comps incl. PL × 0.7 ("fallback_eu");
- any other or unknown market: own channel only.

Only comps from the last `recent_days` (30) make the quantiles, `n_effective`, `n_sold`, `n_sold_effective` and
`comps`; with none the estimate is None. `n_sold_effective` = Σ effective weight of sold-type comps (owner sale,
eBay sold, probably sold, fallback multiplier included): the confidence count used by tiering. Older comps feed only
`trend_30d` = weighted median(recent) / weighted median(older) − 1, with older = the `recent_days` before that,
limited to `lookback_days`. The quantiles are capped at the new-retail ceiling: lowest `retail_new` price of the
last `recent_days` (or `retail_new_pln`) × 0.95.

FX: `estimate` is sync and reads the `fx` cache (filled by money.Fx; `warm_fx` fills it ahead). A rate up to
`fx_max_back_days` older than the comp date is accepted. The notes cover the comps that make the quantiles (the last
`recent_days`) only: "fx_stale" when one of them was converted at an NBP rate more than `fx_stale_days` older than
its date (a weekend or holiday gap is normal NBP behaviour, not staleness), "fx_missing:CCY" when one had no rate
(it is skipped). Older comps (trend only) never add a note.

Quantiles use midpoint interpolation: each value sits at the middle of its weight on the cumulative-weight axis.
With equal weights this reproduces the ordinary median.
"""
from __future__ import annotations

import bisect
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
from .store import Store

ROOT = Path(__file__).resolve().parents[1]
SOLD_KINDS = frozenset({"owner_sale", "ebay_sold", "probably_sold"})
EU_MARKETS = frozenset({"AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT",
                        "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE"})


@dataclass(frozen=True)
class Params:
    """Valuation parameters. Built from config only (`from_config`); there are no defaults in code."""
    ask_to_sale_ratio: Decimal
    weights: dict
    fallback_pl_pool: float
    fallback_eu_pool_for_pl: float
    fallback_eu_pool_for_eu: float
    ceiling_factor: Decimal
    recent_days: int
    lookback_days: int
    active_ask_max_age_days: int
    fx_max_back_days: int
    fx_stale_days: int

    @classmethod
    def from_config(cls, cfg: dhconfig.Config) -> "Params":
        """KeyError if config/valuation.yaml lacks a key: a silent default would hide a config mistake."""
        d = cfg["valuation"].to_dict()
        fb = d["fallback"]
        return cls(
            ask_to_sale_ratio=Decimal(str(d["ask_to_sale_ratio"])),
            weights={k: float(x) for k, x in d["weights"].items()},
            fallback_pl_pool=float(fb["pl_pool"]),
            fallback_eu_pool_for_pl=float(fb["eu_pool_for_pl"]),
            fallback_eu_pool_for_eu=float(fb["eu_pool_for_eu"]),
            ceiling_factor=Decimal(str(d["ceiling_factor"])),
            recent_days=int(d["recent_days"]),
            lookback_days=int(d["lookback_days"]),
            active_ask_max_age_days=int(d["active_ask_max_age_days"]),
            fx_max_back_days=int(d["fx_max_back_days"]),
            fx_stale_days=int(d["fx_stale_days"]),
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
    n_sold: int  # count, for display; confidence uses n_sold_effective
    newest_days: float
    days_to_sell: float | None
    trend_30d: float | None
    ceiling_pln: Decimal | None
    comps: list[CompRef]
    n_sold_effective: float = 0.0
    notes: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ quantiles


def weighted_quantile(values: list[Decimal], weights: list[float], q: float) -> Decimal:
    """Weighted quantile, midpoint interpolation (each value at the centre of its weight). q in [0, 1]."""
    return weighted_quantiles(values, weights, (q,))[0]


def weighted_quantiles(values: list[Decimal], weights: list[float], qs: Iterable[float]) -> list[Decimal]:
    """`weighted_quantile` for several q at once: one sort and one cumulative pass (I5)."""
    if not values or len(values) != len(weights):
        raise ValueError("weighted_quantile needs equally long, non-empty values and weights")
    pairs = sorted((Decimal(v), Decimal(str(w))) for v, w in zip(values, weights))
    if any(w <= 0 for _, w in pairs):
        raise ValueError("weights must be positive")
    total = sum(w for _, w in pairs)
    pos, cum = [], Decimal(0)
    for _, w in pairs:
        pos.append((cum + w / 2) / total)
        cum += w
    out = []
    for q in qs:
        qd = Decimal(str(q))
        if qd <= pos[0]:
            out.append(pairs[0][0])
            continue
        if qd >= pos[-1]:
            out.append(pairs[-1][0])
            continue
        i = bisect.bisect_right(pos, qd) - 1  # pos[i] <= qd < pos[i + 1]
        lo, hi = pairs[i][0], pairs[i + 1][0]
        out.append(lo + (hi - lo) * (qd - pos[i]) / (pos[i + 1] - pos[i]))
    return out


# ------------------------------------------------------------------ FX (cached rates only; sync)


def cached_rate(store: Store, ccy: str, day: date, max_back_days: int) -> tuple[Decimal, date] | None:
    """(PLN per unit, NBP effective date) from the `fx` cache for `day`, or up to `max_back_days` before it."""
    ccy = ccy.upper()
    if ccy == "PLN":
        return Decimal(1), day
    r = store.conn.execute(
        "select date, rate_pln, effective_date from fx where currency=? and date<=? and date>=? "
        "order by date desc limit 1", (ccy, day.isoformat(), (day - timedelta(days=max_back_days)).isoformat())
    ).fetchone()
    if r is None:
        return None
    return Decimal(r["rate_pln"]), date.fromisoformat(r["effective_date"] or r["date"])


async def warm_fx(store: Store, fx, key: str, condition: str | None, day: date | datetime, *,
                  params: Params | None = None, channels: Channels | None = None,
                  rows: list[CompRow] | None = None) -> int:
    """Fetch (via money.Fx, which caches) every rate an `estimate` with the same arguments will need.

    Pass the same `params`/`channels` as to `estimate` (and the same precomputed `rows`, if any). Returns the number
    of (currency, date) pairs fetched.
    """
    p = params or default_params()
    now = _now(day)
    if rows is None:
        rows = _ledger_rows(store, key, condition, now, p, channels or default_channels(), include_listing_asks=True)
    need = {(r.currency.upper(), r.at.date()) for r in rows if r.price_pln is None and r.currency.upper() != "PLN"}
    for ccy, d in sorted(need):
        await fx.rate_with_date(ccy, d)
    return len(need)


async def ingest_comps(store: Store, rows: Iterable[CompRow], fx) -> int:
    """Persist CompRows (e.g. `EbayBrowse.active_comps(key)`) into `comps`, in PLN at the comp-date rate.

    Asks are stored at the asking price; the ask-to-sale ratio is applied at estimate time. An `active_ask` already
    stored under the same ref is refreshed (price and time); other kinds are idempotent by (kind, ref).
    Returns the number of rows inserted or refreshed.
    """
    written = 0
    for r in rows:
        old = store.conn.execute("select id from comps where kind=? and ref=?", (r.kind, r.ref)).fetchone()
        if old is not None and r.kind != "active_ask":
            continue
        if r.price_pln is not None:
            pln = q2(r.price_pln)
        elif r.currency.upper() == "PLN":
            pln = q2(r.price)
        else:
            pln = q2(r.price * await fx.rate(r.currency.upper(), r.at.date()))
        if old is None:
            store.add_comp(r.key, r.condition, r.channel, pln, r.at, r.kind, r.ref)
        else:
            store.conn.execute("update comps set price_pln=?, sold_at=?, condition=?, channel=? where id=?",
                               (str(pln), ledger.iso_utc(r.at), r.condition, r.channel, old["id"]))
            store.conn.commit()
        written += 1
    return written


# ------------------------------------------------------------------ estimate


def _now(day: date | datetime) -> datetime:
    if isinstance(day, datetime):
        ledger.iso_utc(day)  # rejects naive datetimes
        return day.astimezone(timezone.utc)
    return datetime.combine(day, time(23, 59, 59), tzinfo=timezone.utc)


def ledger_rows(store: Store, key: str, condition: str | None, day: date | datetime, *, params: Params | None = None,
                channels: Channels | None = None, include_listing_asks: bool = True,
                ask_cache: dict | None = None) -> list[CompRow]:
    """The clears-at ledger's comparables for `key`/`condition` as of `day` (probably sold + live asks), for ALL
    listings: compute them once per (key, condition) per round and pass them to every `estimate` of that round
    (`ledger_rows=`); each estimate drops its own candidate (`exclude_listing_ids`) afterwards."""
    return _ledger_rows(store, key, condition, _now(day), params or default_params(), channels or default_channels(),
                        include_listing_asks, ask_cache)


def _ledger_rows(store: Store, key: str, condition: str | None, now: datetime, p: Params, channels: Channels,
                 include_listing_asks: bool, ask_cache: dict | None = None) -> list[CompRow]:
    since = now - timedelta(days=p.lookback_days)
    rows = [r for r in ledger.clears_at(store, key, condition, since, channel_of=channels.channel_of,
                                        ask_cache=ask_cache) if r.at <= now]
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


def _tiers(target: str | None, channel: str, p: Params, market_of) -> list[tuple[str | None, float, object]]:
    """[(note, weight factor, row predicate)] in order of preference."""
    own = (None, 1.0, lambda r: r.channel == channel)
    in_eu = (lambda r: market_of(r.channel) in EU_MARKETS)
    if target == "PL":
        return [own, ("fallback_pl", p.fallback_pl_pool, lambda r: market_of(r.channel) == "PL"),
                ("fallback_eu", p.fallback_eu_pool_for_pl, in_eu)]
    if target in EU_MARKETS:
        return [own, ("fallback_eu", p.fallback_eu_pool_for_eu, in_eu)]
    return [own]


@dataclass
class _Conv:
    row: CompRow
    pln: Decimal | None
    gap_days: int  # comp date - NBP effective date of the rate used (0 for PLN or a same-day rate)


def _wmedian(rows: list[tuple[Decimal, float]]) -> Decimal:
    return weighted_quantile([v for v, _ in rows], [w for _, w in rows], 0.5)


def estimate(store: Store, key: str, condition: str | None, channel: str, day: date | datetime, *,
             params: Params | None = None, channels: Channels | None = None,
             exclude_listing_ids: Iterable[int] = (), retail_new_pln: Decimal | None = None,
             include_listing_asks: bool = True, ledger_rows: list[CompRow] | None = None) -> ExitEstimate | None:
    """Exit-price distribution for `key` in `condition` (None = any) on exit `channel`, as of `day`.

    `day` is a date (valued as of its end, UTC) or an aware datetime. None when no channel tier has a comparable
    from the last `recent_days`. `ledger_rows`: the ledger comparables precomputed by `ledger_rows()` for the same
    key, condition and day (the round's cache); None computes them here.
    """
    p = params or default_params()
    ch = channels or default_channels()
    now = _now(day)
    since = now - timedelta(days=p.lookback_days)
    recent_from = now - timedelta(days=p.recent_days)
    trend_from = now - timedelta(days=min(p.lookback_days, 2 * p.recent_days))

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
        if not _cond_ok(r["condition"], condition) or r["kind"] not in p.weights:
            continue
        price = Decimal(r["price_pln"])
        rows.append(CompRow(key=key, condition=r["condition"], channel=r["channel"], kind=r["kind"], price=price,
                            currency="PLN", at=at, weight=p.weights[r["kind"]], ref=r["ref"] or f"comp:{r['id']}",
                            price_pln=price))
    rows += (_ledger_rows(store, key, condition, now, p, ch, include_listing_asks) if ledger_rows is None
             else [r for r in ledger_rows if r.at <= now and (include_listing_asks or r.kind != "active_ask")])
    excluded = set(exclude_listing_ids)
    seen: set[tuple[str, str]] = set()
    conv: list[_Conv] = []
    for r in rows:
        if (r.listing_id is not None and r.listing_id in excluded) or (r.kind, r.ref) in seen:
            continue
        seen.add((r.kind, r.ref))
        pln, gap = r.price_pln, 0
        if pln is None:
            got = cached_rate(store, r.currency, r.at.date(), p.fx_max_back_days)
            if got is not None:
                rate, eff = got
                pln, gap = q2(r.price * rate), (r.at.date() - eff).days
        if pln is not None and r.kind == "active_ask":
            pln = q2(pln * p.ask_to_sale_ratio)
        conv.append(_Conv(r, pln, gap))

    # channel tier: the first with a converted comp from the last recent_days
    chosen = None
    for note, factor, pred in _tiers(ch.market_of(channel), channel, p, ch.market_of):
        pool = [c for c in conv if pred(c.row)]
        if any(c.pln is not None and c.row.at >= recent_from for c in pool):
            chosen = (note, factor, pool)
            break
    if chosen is None:
        return None
    note, factor, pool = chosen
    notes: list[str] = [note] if note else []
    recent_pool = [c for c in pool if c.row.at >= recent_from]  # the comps the quantiles use: the FX notes' scope
    missing = sorted({c.row.currency.upper() for c in recent_pool if c.pln is None})
    notes += [f"fx_missing:{m}" for m in missing]
    used = [(c.row, c.pln, round(p.weights.get(c.row.kind, c.row.weight) * factor, 4))
            for c in pool if c.pln is not None]
    if any(c.gap_days > p.fx_stale_days for c in recent_pool if c.pln is not None):
        notes.append("fx_stale")

    recent = [u for u in used if u[0].at >= recent_from]
    vals = [pln for _, pln, _ in recent]
    ws = [w for _, _, w in recent]
    p25, p50, p75 = (q2(x) for x in weighted_quantiles(vals, ws, (0.25, 0.5, 0.75)))

    if retail_new_pln is not None:
        retail.append(Decimal(retail_new_pln))
    ceiling = q2(min(retail) * p.ceiling_factor) if retail else None
    if ceiling is not None and p75 > ceiling:
        notes.append("capped_at_ceiling")
        p25, p50, p75 = (min(x, ceiling) for x in (p25, p50, p75))

    older = [(pln, w) for r, pln, w in used if trend_from <= r.at < recent_from]
    trend = round(float(_wmedian(list(zip(vals, ws))) / _wmedian(older) - 1), 4) if older else None

    dts = [r.days_to_sell for r, _, _ in recent if r.kind == "probably_sold" and r.days_to_sell is not None]
    newest = max(r.at for r, _, _ in recent)
    fb = note is not None
    comps = [CompRef(kind=r.kind, channel=r.channel, price_pln=pln, at=r.at, weight=w, ref=r.ref, fallback=fb)
             for r, pln, w in sorted(recent, key=lambda u: u[0].at, reverse=True)]
    sold = [(r, w) for r, _, w in recent if r.kind in SOLD_KINDS]
    return ExitEstimate(
        channel=channel, key=key, condition=condition, p25_pln=p25, median_pln=p50, p75_pln=p75,
        n_effective=round(sum(ws), 4), n_sold=len(sold), n_sold_effective=round(sum(w for _, w in sold), 4),
        newest_days=round((now - newest).total_seconds() / 86400, 4),
        days_to_sell=median(dts) if dts else None, trend_30d=trend, ceiling_pln=ceiling, comps=comps, notes=notes)
