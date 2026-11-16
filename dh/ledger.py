"""Clears-at ledger: infer which classified listings probably sold by watching them disappear (spec §6.2, §15a.1).

Only watched time counts. The agent runs when the owner starts it, so every inference is confined to an
observation window: the span between `dh start` and `dh stop` (or the last observation before a crash).

Vocabulary
- arm: one search the agent repeats, `arm_id(source, query_text)`. One `observe(..., arm=...)` call is one sweep.
- sweep: recorded in `sweeps`. A successful sweep is recorded ONLY by `observe(..., arm=...)`, together with what
  it returned; `record_sweep` records failures only. A sweep counts as evidence of absence only when it succeeded
  AND returned at least one listing; a zero-result sweep is more likely a broken parser than an empty market.
- sighting without an arm (mailbox alert, detail/verify fetch): updates the listing, records no sweep, and can never
  make other listings disappear.

Disappearance (`sweep_disappeared`, also run by `close_all_windows` before a window closes). A listing gets
`disappeared_at` when, after its last sighting t0:
1. some arm that has returned it made 2 successful non-empty sweeps after t0 without it;
2. t0 and both sweeps lie in the SAME observation window (the window of the last sighting), so a stop or a crash
   in between makes the disappearance unknown and nothing is inferred;
3. inside that window, from t0 to the second sweep, no two consecutive successful sweeps of the source are more
   than `max_gap` apart. This catches a crashed run whose window was never closed (startup recovery skipped).
`disappeared_at` = the first sweep that missed it (the latest possible disappearance time). A listing seen again
later has `disappeared_at` cleared (it was only pushed off the results page, or relisted).

Appearance. A listing first returned by an arm whose previous successful non-empty sweep lies in the same window
and within `max_gap` appeared under watch: `appeared_at` = that previous sweep (the earliest possible time).
Listings already present when watching started have an unknown age unless the site gives `posted_at`.

Probably sold (`clears_at`): disappeared, age at disappearance ≤ 72 h (age from min(posted_at, appeared_at); unknown
age → not counted), and last price ≤ median ask of the same key in the same currency over the 30 days before the
disappearance (one ask per listing; at least `min_asks` asks). Kind `probably_sold`, weight from
config/valuation.yaml (0.5).
Condition: the matcher leaves most classified titles at `unknown`; `unknown` is valued as `used`
(`condition_filter`). Box-only, for-parts and wanted ads, bundles (whole PCs, lots) and matches with certainty
< 0.7 are never comparables (`_SALEABLE`).

Observations: one row on first sighting and one on every price change (first price + every cut), not one per sweep.
"""
from __future__ import annotations

import bisect
import dataclasses
import sqlite3
import weakref
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from statistics import median
from typing import Callable

from . import config as dhconfig
from .models import CompRow, Listing
from .money import q2
from .store import Store

ROOT = Path(__file__).resolve().parents[1]

MAX_GAP = timedelta(hours=6)
PROBABLY_SOLD_MAX_AGE = timedelta(hours=72)
ASK_WINDOW = timedelta(days=30)
MIN_ASKS = 3
ACTIVE_ASK_MAX_AGE = timedelta(days=3)

_TABLES = (
    """create table if not exists sweeps (
         id integer primary key, source text not null, arm text not null, window_id integer,
         ts text not null, ok integer not null, n_results integer not null default 0, note text)""",
    "create index if not exists idx_sweeps_window on sweeps(window_id, arm, ts)",
    """create table if not exists listing_arms (
         listing_id integer not null, arm text not null, primary key (listing_id, arm))""",
)
_COLUMNS = {
    "listings": ("disappeared_at text", "appeared_at text", "last_window_id integer"),
    "windows": ("last_obs_at text",),
}


_MIGRATED: "weakref.WeakSet[Store]" = weakref.WeakSet()  # weak, so a recycled id() never skips a migration


def iso_utc(t: datetime | str | None) -> str | None:
    """Timezone-aware UTC ISO-8601 (same format as the store writes). Naive values raise ValueError."""
    if t is None:
        return None
    dt = datetime.fromisoformat(t) if isinstance(t, str) else t
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"naive datetime not allowed: {t!r}")
    return dt.astimezone(timezone.utc).isoformat()


_iso = iso_utc


@lru_cache(maxsize=1)
def comp_weights() -> dict[str, float]:
    """Comparable weights by kind, from config/valuation.yaml (single source of truth)."""
    w = dhconfig.load(ROOT).valuation.weights.to_dict()
    return {k: float(v) for k, v in w.items()}


def _t(s: str | None) -> datetime | None:
    return None if s is None else datetime.fromisoformat(s)


def migrate(store: Store) -> None:
    """Idempotent: ledger tables and columns, added without touching dh/store.py."""
    if store in _MIGRATED:
        return
    for sql in _TABLES:
        store.ensure_table(sql)
    for table, cols in _COLUMNS.items():
        have = {r["name"] for r in store.conn.execute(f"pragma table_info({table})")}
        for col in cols:
            if col.split()[0] not in have:
                store.ensure_table(f"alter table {table} add column {col}")
    store.ensure_table("create index if not exists idx_listings_window on listings(source, last_window_id)")
    _MIGRATED.add(store)


def arm_id(source: str, query_text: str) -> str:
    return f"{source}:{' '.join(query_text.lower().split())}"


# ------------------------------------------------------------------ windows


def current_window(store: Store, source: str) -> sqlite3.Row | None:
    migrate(store)
    return store.conn.execute("select * from windows where source=? and closed_at is null order by id desc limit 1",
                              (source,)).fetchone()


def open_window(store: Store, source: str, ts: datetime, *, max_gap: timedelta = MAX_GAP) -> int:
    """Open a new window for `source`; any window of the source still open is stale and is closed first."""
    migrate(store)
    _iso(ts)
    for w in store.conn.execute("select * from windows where source=? and closed_at is null order by id",
                                (source,)).fetchall():
        _close(store, w, ts, max_gap)
    wid = store.open_window(source, ts)
    store.conn.execute("update windows set last_obs_at=? where id=?", (_iso(ts), wid))
    store.conn.commit()
    return wid


def close_all_windows(store: Store, ts: datetime, *, max_gap: timedelta = MAX_GAP) -> int:
    """`dh stop` and startup recovery: close every open window at its last observation time (never later than ts).

    Evidence gathered inside a window (sweeps already made) is evaluated first; nothing after the close counts.
    Returns the number of windows closed.
    """
    migrate(store)
    _iso(ts)
    rows = store.conn.execute("select * from windows where closed_at is null order by id").fetchall()
    for w in rows:
        _close(store, w, ts, max_gap)
    return len(rows)


def _close(store: Store, w: sqlite3.Row, ts: datetime, max_gap: timedelta) -> None:
    opened = _t(w["opened_at"])
    last = _t(w["last_obs_at"]) or opened
    at = max(opened, min(last, ts))
    _infer_window(store, w, at, max_gap)
    store.close_window(w["id"], at)


# ------------------------------------------------------------------ observing


def observe(store: Store, source: str, listings: list[Listing], ts: datetime, *, arm: str | None = None,
            max_gap: timedelta = MAX_GAP) -> list[int]:
    """Upsert listings seen at `ts` and record observations. With `arm`, this call is one successful sweep of it.

    Opens a window for the source if none is open. This is the ONLY way a successful sweep is recorded.
    A listing with price ≤ 0 ("do negocjacji") is still a sighting: an already tracked listing gets its last
    sighting, window and arm refreshed (and `disappeared_at` cleared) but no price observation; an untracked one
    is not stored. All listings count towards the sweep's result count. Returns the ids of listings touched.
    """
    migrate(store)
    ts_iso = _iso(ts)
    w = current_window(store, source)
    wid = w["id"] if w is not None else open_window(store, source, ts, max_gap=max_gap)
    if w is not None and ts < _t(w["opened_at"]):
        raise ValueError(f"observation at {ts_iso} precedes its window ({w['opened_at']})")
    for l in listings:
        if l.source != source:
            raise ValueError(f"listing from {l.source!r} observed as {source!r}")
    n_results = len(listings)

    prev_sweep = None
    if arm is not None:
        prev_sweep = store.conn.execute(
            "select ts from sweeps where window_id=? and arm=? and ok=1 and n_results>0 and ts<? "
            "order by ts desc limit 1", (wid, arm, ts_iso)).fetchone()
    appeared = None
    if prev_sweep is not None and ts - _t(prev_sweep["ts"]) <= max_gap:
        appeared = prev_sweep["ts"]

    ids = []
    for l in listings:
        old = store.conn.execute("select id, price from listings where source=? and source_id=?",
                                 (source, l.source_id)).fetchone()
        if l.price is None or l.price <= 0:
            if old is None:
                continue  # nothing to track: no price
            lid = old["id"]
            store.conn.execute("update listings set last_seen_at=max(coalesce(last_seen_at,''), ?) where id=?",
                               (ts_iso, lid))
        else:
            # one commit for the whole sweep (below), not one fsync per listing: ~3.6 s of a round's event loop
            lid = store.upsert_listing(dataclasses.replace(l, seen_at=ts), commit=False)
            if old is None or Decimal(old["price"]) != l.price:
                store.record_observation(lid, l.price, ts, commit=False)
        store.conn.execute("update listings set last_window_id=?, disappeared_at=null where id=?", (wid, lid))
        if old is None and appeared is not None:
            store.conn.execute("update listings set appeared_at=? where id=?", (appeared, lid))
        if arm is not None:
            store.conn.execute("insert or ignore into listing_arms (listing_id, arm) values (?,?)", (lid, arm))
        ids.append(lid)
    if arm is not None:
        store.conn.execute("insert into sweeps (source, arm, window_id, ts, ok, n_results) values (?,?,?,?,1,?)",
                           (source, arm, wid, ts_iso, n_results))
    if ids:
        store.conn.execute("update windows set last_obs_at=? where id=? and (last_obs_at is null or last_obs_at<?)",
                           (ts_iso, wid, ts_iso))
    store.conn.commit()
    return ids


def record_sweep(store: Store, source: str, arm: str, ts: datetime, note: str = "") -> None:
    """Record a FAILED sweep (block, error, timeout) for the health record. It never counts as evidence.

    Failure-only by design: a successful sweep exists only through `observe(..., arm=...)`, which also records
    what the sweep returned. A success recorded without its results would make every listing look absent.
    """
    migrate(store)
    w = current_window(store, source)
    store.conn.execute("insert into sweeps (source, arm, window_id, ts, ok, n_results, note) values (?,?,?,?,0,0,?)",
                       (source, arm, None if w is None else w["id"], _iso(ts), note))
    store.conn.commit()


# ------------------------------------------------------------------ disappearance


def sweep_disappeared(store: Store, source: str, ts: datetime, *, max_gap: timedelta = MAX_GAP) -> list[int]:
    """Mark listings of `source` that vanished under watch (see module doc). Returns the ids marked now."""
    migrate(store)
    _iso(ts)
    out: list[int] = []
    for w in store.conn.execute("select * from windows where source=? and closed_at is null order by id",
                                (source,)).fetchall():
        out += _infer_window(store, w, ts, max_gap)
    return out


def _infer_window(store: Store, w: sqlite3.Row, until: datetime, max_gap: timedelta = MAX_GAP) -> list[int]:
    if w["closed_at"] is not None:
        until = min(until, _t(w["closed_at"]))
    sweeps = [(r["arm"], _t(r["ts"])) for r in store.conn.execute(
        "select arm, ts from sweeps where window_id=? and ok=1 and n_results>0 order by ts", (w["id"],))]
    sweeps = [(a, t) for a, t in sweeps if t <= until]
    if not sweeps:
        return []
    by_arm: dict[str, list[datetime]] = {}
    for a, t in sweeps:
        by_arm.setdefault(a, []).append(t)
    beats = [t for _, t in sweeps]

    arms_of: dict[int, list[str]] = {}
    for r in store.conn.execute(
            """select la.listing_id, la.arm from listing_arms la join listings l on l.id=la.listing_id
               where l.source=? and l.last_window_id=? and l.disappeared_at is null""", (w["source"], w["id"])):
        arms_of.setdefault(r["listing_id"], []).append(r["arm"])
    marked = []
    for c in store.conn.execute(
            "select id, last_seen_at from listings where source=? and last_window_id=? and disappeared_at is null",
            (w["source"], w["id"])).fetchall():
        t0 = _t(c["last_seen_at"])
        arms = arms_of.get(c["id"], [])
        first_miss = None
        for a in arms:
            after = [t for t in by_arm.get(a, ()) if t > t0]
            if len(after) < 2:
                continue
            s1, s2 = after[0], after[1]
            if not _continuous(t0, s2, beats, max_gap):
                continue
            if first_miss is None or s1 < first_miss:
                first_miss = s1
        if first_miss is not None:
            store.conn.execute("update listings set disappeared_at=? where id=?", (_iso(first_miss), c["id"]))
            marked.append(c["id"])
    store.conn.commit()
    return marked


def _continuous(t0: datetime, end: datetime, beats: list[datetime], max_gap: timedelta) -> bool:
    points = [t0] + [t for t in beats if t0 < t <= end]
    return all(b - a <= max_gap for a, b in zip(points, points[1:]))


# ------------------------------------------------------------------ comps


# A comparable is one unit of exactly the key: never a box, a part-out, a wanted ad, a bundle (a whole PC with
# that GPU, a GPU + RAM lot: bundle_items non-empty) or an uncertain match (certainty below COMP_MIN_CERTAINTY).
COMP_MIN_CERTAINTY = 0.7
_SALEABLE = ("coalesce(e.box_only,0)=0 and coalesce(e.for_parts,0)=0 and coalesce(e.wanted_ad,0)=0 "
             "and coalesce(e.condition,'') != 'for_parts' "
             "and coalesce(e.bundle_items,'[]') in ('[]','') "
             f"and coalesce(e.certainty,0) >= {COMP_MIN_CERTAINTY}")
USED_LIKE = ("used", "unknown")  # a classified ad with no stated condition is valued as used


def condition_filter(condition: str | None) -> tuple[str, list]:
    """SQL fragment on extractions `e`: None = any; 'used'/'unknown' = used, unknown or missing; else exact."""
    if condition is None:
        return "", []
    if condition in USED_LIKE:
        return " and (e.condition is null or e.condition in ('used','unknown'))", []
    return " and e.condition=?", [condition]


def _pln(price: Decimal, currency: str) -> Decimal | None:
    return q2(price) if currency.upper() == "PLN" else None


def _price_at(store: Store, listing_id: int, t: datetime, fallback: str) -> Decimal:
    r = store.conn.execute("select price from observations where listing_id=? and seen_at<=? "
                           "order by seen_at desc, id desc limit 1", (listing_id, _iso(t))).fetchone()
    return Decimal(r["price"] if r else fallback)


def _median_ask(store: Store, key: str, currency: str, t: datetime, window: timedelta) -> tuple[Decimal | None, int]:
    """Median of one ask per listing (its price at t) for listings of `key` in `currency` live during [t-window, t].
    The reference computation (one query per listing); `median_ask` serves the same result from an index."""
    lo = t - window
    rows = store.conn.execute(
        f"""select l.id, l.price, l.first_seen_at, l.last_seen_at from listings l
            join extractions e on e.listing_id=l.id
            where e.key=? and upper(l.currency)=? and {_SALEABLE}
            and l.first_seen_at <= ? and l.last_seen_at >= ?""",
        (key, currency.upper(), _iso(t), _iso(lo))).fetchall()
    asks = [_price_at(store, r["id"], t, r["price"]) for r in rows
            if _t(r["first_seen_at"]) <= t and _t(r["last_seen_at"]) >= lo]
    return (median(asks) if asks else None), len(asks)


class _AskIndex:
    """Every saleable listing of one key in one currency with its price history, loaded once (two queries), so the
    median ask at any time t is computed in memory (I5: the per-listing queries made clears_at quadratic)."""

    def __init__(self, store: Store, key: str, currency: str):
        args = (key, currency.upper())
        where = f"e.key=? and upper(l.currency)=? and {_SALEABLE}"
        self.listings = [(r["id"], _t(r["first_seen_at"]), _t(r["last_seen_at"]), r["price"]) for r in store.conn.execute(
            f"select l.id, l.price, l.first_seen_at, l.last_seen_at from listings l "
            f"join extractions e on e.listing_id=l.id where {where}", args)]
        self.obs: dict[int, tuple[list[datetime], list[str]]] = {}
        for r in store.conn.execute(
                f"select o.listing_id, o.seen_at, o.price from observations o join listings l on l.id=o.listing_id "
                f"join extractions e on e.listing_id=l.id where {where} order by o.listing_id, o.seen_at, o.id", args):
            times, prices = self.obs.setdefault(r["listing_id"], ([], []))
            times.append(_t(r["seen_at"]))
            prices.append(r["price"])

    def price_at(self, lid: int, t: datetime, fallback: str) -> Decimal:
        """As `_price_at`: the latest observation at or before t (ties: the later row), else the listing price."""
        times, prices = self.obs.get(lid, ((), ()))
        i = bisect.bisect_right(times, t) - 1
        return Decimal(prices[i] if i >= 0 else fallback)

    def median(self, t: datetime, window: timedelta) -> tuple[Decimal | None, int]:
        lo = t - window
        asks = [self.price_at(lid, t, price) for lid, first, last, price in self.listings
                if first is not None and last is not None and first <= t and last >= lo]
        return (median(asks) if asks else None), len(asks)


def median_ask(store: Store, key: str, currency: str, t: datetime, window: timedelta,
               cache: dict | None = None) -> tuple[Decimal | None, int]:
    """`_median_ask` from an index built once per (key, currency) and memoised per (key, currency, t) in `cache`
    (pass one dict per round to share it across calls; None = a fresh index for this call)."""
    cache = {} if cache is None else cache
    ccy = currency.upper()
    mk = ("median", key, ccy, t, window)
    if mk not in cache:
        ik = ("index", key, ccy)
        if ik not in cache:
            cache[ik] = _AskIndex(store, key, ccy)
        cache[mk] = cache[ik].median(t, window)
    return cache[mk]


def clears_at(store: Store, key: str, condition: str | None, since: datetime, *,
              channel_of: Callable[[str], str] = lambda s: s, max_age: timedelta = PROBABLY_SOLD_MAX_AGE,
              ask_window: timedelta = ASK_WINDOW, min_asks: int = MIN_ASKS,
              ask_cache: dict | None = None) -> list[CompRow]:
    """Probably-sold comparables for `key` (and `condition`, None = any) that disappeared at or after `since`.
    `ask_cache`: a dict shared by the calls of one round (median-ask index per key and currency)."""
    ask_cache = {} if ask_cache is None else ask_cache
    migrate(store)
    sql = f"""select l.*, e.condition as cond from listings l join extractions e on e.listing_id=l.id
              where e.key=? and l.disappeared_at is not null and l.disappeared_at>=? and {_SALEABLE}"""
    args: list = [key, _iso(since)]
    frag, extra = condition_filter(condition)
    sql, args = sql + frag, args + extra
    out = []
    for r in store.conn.execute(sql + " order by l.disappeared_at, l.id", args).fetchall():
        gone = _t(r["disappeared_at"])
        starts = [t for t in (_t(r["posted_at"]), _t(r["appeared_at"])) if t is not None]
        if not starts:
            continue  # present when watching started, no posted date: age unknown
        age = gone - min(starts)
        if age > max_age:
            continue
        price = _price_at(store, r["id"], gone, r["price"])
        med, n = median_ask(store, key, r["currency"], gone, ask_window, cache=ask_cache)
        if med is None or n < min_asks or price > med:
            continue
        out.append(CompRow(key=key, condition=r["cond"], channel=channel_of(r["source"]), kind="probably_sold",
                           price=price, currency=r["currency"].upper(), at=gone, weight=comp_weights()["probably_sold"],
                           ref=r["url"] or f"listing:{r['id']}", price_pln=_pln(price, r["currency"]),
                           country=r["country"], listing_id=r["id"], days_to_sell=age.total_seconds() / 86400))
    return out


def active_asks(store: Store, key: str, condition: str | None, now: datetime, *,
                channel_of: Callable[[str], str] = lambda s: s,
                max_age: timedelta = ACTIVE_ASK_MAX_AGE) -> list[CompRow]:
    """Live asks for `key` from tracked listings: not disappeared, last seen within `max_age` before `now`."""
    migrate(store)
    sql = f"""select l.*, e.condition as cond from listings l join extractions e on e.listing_id=l.id
              where e.key=? and l.disappeared_at is null and l.last_seen_at>=? and l.last_seen_at<=?
              and {_SALEABLE}"""
    args: list = [key, _iso(now - max_age), _iso(now)]
    frag, extra = condition_filter(condition)
    sql, args = sql + frag, args + extra
    out = []
    for r in store.conn.execute(sql + " order by l.id", args).fetchall():
        price = Decimal(r["price"])
        out.append(CompRow(key=key, condition=r["cond"], channel=channel_of(r["source"]), kind="active_ask",
                           price=price, currency=r["currency"].upper(), at=_t(r["last_seen_at"]),
                           weight=comp_weights()["active_ask"], ref=r["url"] or f"listing:{r['id']}",
                           price_pln=_pln(price, r["currency"]), country=r["country"], listing_id=r["id"]))
    return out


def watched_hours(store: Store, source: str | None = None, until: datetime | None = None) -> float:
    """Observed time in hours: the UNION of the observation windows (one hour watched on three sources at once is
    one hour, not three), optionally of one `source`, and up to `until`. Open windows count up to their last
    observation. The status, books and web pages and the improvement loop's clock (improve.monitor) all use this."""
    migrate(store)
    sql, args = "select opened_at, closed_at, last_obs_at from windows", []
    if source is not None:
        sql, args = sql + " where source=?", [source]
    spans = []
    for w in store.conn.execute(sql, args).fetchall():
        a = _t(w["opened_at"])
        b = _t(w["closed_at"]) or _t(w["last_obs_at"]) or a
        if until is not None:
            b = min(b, until)
        if b > a:
            spans.append((a, b))
    spans.sort()
    total, cur_a, cur_b = 0.0, None, None
    for a, b in spans:
        if cur_b is None or a > cur_b:
            if cur_b is not None:
                total += (cur_b - cur_a).total_seconds()
            cur_a, cur_b = a, b
        else:
            cur_b = max(cur_b, b)
    if cur_b is not None:
        total += (cur_b - cur_a).total_seconds()
    return total / 3600
