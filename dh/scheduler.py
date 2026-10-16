"""Round scheduler (spec §4 component 1, §9.2): one `run_round` = search -> ledger -> filter -> extract -> value ->
economics -> risk -> tier -> detail/verify -> cards/push -> books/bandit.

Arms are (source x playbook query item). Every round runs:
- the sentinel arms (playbook query items with `sentinel: true`; the agent never edits them), the control group;
- the feed arms: one per feed source (RSS, retail watch; `scheduler.feed_sources`) with an empty query. Feeds return
  the same page for every query, so one sweep per round is enough. They are not ledger-swept: an RSS "hot" item or a
  retail price that leaves the feed was not sold on a classified, so it must never become a `probably_sold` comp;
- bandit-chosen arms (`Bandit.order`), up to `budget` searches per round and `max_searches_per_source` per source.
  The budget grows by `budget_boost` (to at most `budget_max`) after a round in which no LLM item ever waited for a
  worker slot, and shrinks back towards `budget` when the queue backed up (spec §10 "full capacity").
Arms of adapters that are disabled (eBay without keys) or whose domain is blocked/disabled by the fetcher are skipped
(health throttles them, not the bandit).

Sources run concurrently, each source's arms sequentially (the fetcher paces per domain anyway). Search pages go
through a per-round page cache (`RoundFetcher`), so category-only sources (Bazos) fetch each category page once per
round however many queries filter it. Detail pages and alive checks always use the real fetcher (fresh).

Per listing (only new listings, a changed price, or a stale evaluation older than `reeval_hours`):
1. cheap filter: rules matcher; wanted/box-only/for-parts ads are dropped; no key and no hardware word -> dropped;
   a keyed price outside 0.15..3 x the key's median known price is dropped (accessories, typos, bait);
2. LLM extraction only when the rules are not certain (< 0.9), through an asyncio.Semaphore of `models.worker.parallel`
   (default 4) slots; at most `max_llm_items` per round, the rest wait for the next round. A stored LLM result for
   the same title is reused;
3. valuation per enabled exit channel -> economics per channel -> best exit -> risk -> tiering;
4. tier <= 3: detail fetch, flags and risk re-run on the full description, dedupe; tier <= 2: live re-check (alive
   AND same price) — a confirmed re-check is "verified now" and may lift a deal to tier 1; a gone listing is dropped;
   tier 1: discovery screenshot and ntfy push (not with `dry`);
5. deal saved, card written.
After the sweeps: newly disappeared listings are confirmed with `alive()` (<= `alive_confirm_per_source`), pending
pushes retried, eBay asks ingested as comps, owner "sold" marks become owner_sale comps (once), costs recorded,
the bandit updated per arm and discounted.
"""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
import math
import random
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

from . import books, cards, dedupe, economics, extract, ledger, notify, partout, photos, risk as riskmod, tiering, valuation, verify
from .bandit import Bandit, value_units
from .config import Config, assert_frozen_unchanged
from .match import rules_extract
from .models import Deal, Extraction, Listing
from .money import FxError, q2
from .playbook_io import load_items
from .sources.base import KIND_PATTERNS, AdapterError, Query

log = logging.getLogger(__name__)

DEFAULTS = {
    "round_interval_s": 600, "budget": 60, "budget_max": 120, "budget_boost": 1.5, "max_searches_per_source": 12,
    "max_llm_items": 200, "alive_confirm_per_source": 5, "reeval_hours": 24, "round_timeout_s": 1500,
    "ebay_comp_keys": 10, "ebay_insights_keys": 10, "maintenance_hours": 24, "retention_days": 90,
    "price_range": [0.15, 3.0], "min_prices_for_range": 3, "min_estimate_comps": 3,
    "feed_sources": ["pepper_pl", "hotukdeals", "retail"],
}
CATEGORY_FOR_ECONOMICS = {"gpu": "gpu", "gpu-mobile": "gpu", "ram": "ram", "ram-sodimm": "ram", "ssd": "ssd", "pc": "pc"}
PC_KEY = "pc:bundle"
KRAKOW = (50.0647, 19.9450)
_HW = re.compile("|".join(rx for kind, rx in KIND_PATTERNS if kind in ("gpu", "ram", "disk")), re.I)
_SYSTEM_WORDS = re.compile(r"komputer|zestaw|(?<![a-z])pc(?![a-z])|laptop|notebook|desktop|rechner|počítač|pocitac|"
                           r"számítógép|szamitogep|macbook|konsol|playstation|xbox", re.I)
EVAL_SQL = """create table if not exists evaluations (
  listing_id integer primary key, price text, title_hash text, at text, tier integer, note text)"""
MARK_COMPS_SQL = "create table if not exists mark_comps (mark_id integer primary key)"
MAINT_SQL = "create table if not exists maintenance (task text primary key, at text not null)"


def is_mail_source(ad) -> bool:
    """The alert mailbox: polled once per round (each poll consumes unseen mail), never searched per query."""
    return bool(getattr(ad, "via_mailbox", False))


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def sched_cfg(cfg) -> dict:
    s = cfg.get("scheduler") if cfg is not None else None
    s = s.to_dict() if s is not None and hasattr(s, "to_dict") else (s or {})
    return {**DEFAULTS, **s}


# ------------------------------------------------------------------------------------------------ arms


@dataclass
class Arm:
    id: str
    source: str
    query: Query
    item_id: str
    sentinel: bool = False
    feed: bool = False


def _targets(target: Any, names: list[str]) -> list[str]:
    t = str(target or "all").strip().lower()
    if t in ("all", "*", "sources:all"):
        return list(names)
    want = [x.strip() for x in t.split(",") if x.strip()]
    return [n for n in want if n in names]


def build_arms(items: list[dict], adapters: dict, feed_sources: list[str]) -> list[Arm]:
    """Query items x their target sources, plus one feed arm per feed source (dedup by arm id; sentinel wins)."""
    feeds = [n for n in adapters if n in set(feed_sources) and not is_mail_source(adapters[n])]
    searchable = [n for n in adapters if n not in set(feed_sources) and not is_mail_source(adapters[n])]
    out: dict[str, Arm] = {}
    for it in items:
        if it.get("kind") != "query":
            continue
        text = " ".join(str(it["value"]).lower().split())
        for src in _targets(it.get("target"), searchable):
            aid = ledger.arm_id(src, text)
            q = Query(text=text, category=it.get("category"),
                      min_price_pln=None if it.get("min_pln") is None else Decimal(str(it["min_pln"])),
                      max_price_pln=None if it.get("max_pln") is None else Decimal(str(it["max_pln"])), arm_id=aid)
            arm = Arm(aid, src, q, it["id"], sentinel=bool(it.get("sentinel")))
            if aid not in out or arm.sentinel:
                out[aid] = arm
    for src in feeds:
        aid = ledger.arm_id(src, "")
        out[aid] = Arm(aid, src, Query(text="", arm_id=aid), f"feed:{src}", feed=True)
    return list(out.values())


# ------------------------------------------------------------------------------------------------ fetch / fx helpers


class RoundFetcher:
    """Per-round memo of successful plain GETs (search pages). Counts real requests per domain."""

    def __init__(self, inner):
        self._inner = inner
        self._cache: dict[tuple[str, str], Any] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self.requests: Counter = Counter()

    async def get(self, url: str, *, kind: str, headers: dict | None = None):
        if headers:
            self.requests[urlsplit(url).netloc.lower()] += 1
            return await self._inner.get(url, kind=kind, headers=headers)
        k = (url, kind)
        lock = self._locks.setdefault(k, asyncio.Lock())
        async with lock:
            if k in self._cache:
                return self._cache[k]
            res = await self._inner.get(url, kind=kind)
            self.requests[urlsplit(url).netloc.lower()] += 1
            if getattr(res, "status", 0) == 200 and not getattr(res, "blocked", False):
                self._cache[k] = res
            return res

    def __getattr__(self, name):
        return getattr(self._inner, name)


class MemoFx:
    """Per-round in-memory memo over money.Fx (today's NBP rate is not pinned in the DB before it is published,
    so without this every appraisal would ask NBP again)."""

    def __init__(self, inner):
        self._inner = inner
        self._m: dict[tuple[str, date], tuple[Decimal, date]] = {}

    async def rate_with_date(self, ccy: str, day: date) -> tuple[Decimal, date]:
        k = (ccy.upper(), day)
        if k not in self._m:
            self._m[k] = await self._inner.rate_with_date(ccy, day)
        return self._m[k]

    async def rate(self, ccy: str, day: date) -> Decimal:
        return (await self.rate_with_date(ccy, day))[0]

    async def to_pln(self, amount: Decimal, ccy: str, day: date) -> Decimal:
        return q2(Decimal(amount) * await self.rate(ccy, day))

    async def pln_to_usd(self, amount_pln: Decimal, day: date) -> Decimal:
        return q2(Decimal(amount_pln) / await self.rate("USD", day))


def make_to_pln(memo: dict[str, Decimal]) -> Callable[[Decimal, str], Decimal | None]:
    """Sync converter for adapters' `filter_query`, from rates warmed at round start."""
    def to_pln(amount: Decimal, ccy: str) -> Decimal | None:
        c = (ccy or "").upper()
        if c == "PLN":
            return q2(Decimal(amount))
        r = memo.get(c)
        return None if r is None else q2(Decimal(amount) * r)
    return to_pln


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    (la1, lo1), (la2, lo2) = a, b
    p1, p2 = math.radians(la1), math.radians(la2)
    dp, dl = p2 - p1, math.radians(lo2 - lo1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


# ------------------------------------------------------------------------------------------------ context / stats


@dataclass
class RoundStats:
    round_id: int | None = None
    started_at: str = ""
    seconds: float = 0.0
    budget: int = 0
    arms: int = 0
    sentinel_arms: int = 0
    feed_arms: int = 0
    skipped_arms: int = 0
    sources: dict = field(default_factory=dict)  # source -> {searches, ok, failed, listings}
    listings_seen: int = 0
    processed: int = 0
    candidates: int = 0
    llm_items: int = 0
    llm_deferred: int = 0
    llm_calls: int = 0
    llm_tokens: int = 0
    llm_max_wait: int = 0
    deals_by_tier: dict = field(default_factory=dict)
    deals: list = field(default_factory=list)
    duplicates: int = 0
    dropped: dict = field(default_factory=dict)
    disappeared: int = 0
    alive_cleared: int = 0
    comps_ingested: int = 0
    owner_sale_comps: int = 0
    insights_calls: int = 0
    mail_listings: int = 0
    mail_committed: int = 0
    partout: int = 0
    photo_reuse: int = 0
    maintenance: dict = field(default_factory=dict)
    pushes_retried: int = 0
    fetches: int = 0
    energy_pln: str = "0.00"
    errors: list = field(default_factory=list)

    def drop(self, why: str) -> None:
        self.dropped[why] = self.dropped.get(why, 0) + 1

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["deals_by_tier"] = {str(k): v for k, v in sorted(self.deals_by_tier.items())}
        return d


@dataclass
class RoundContext:
    cfg: Config
    root: Path
    store: Any
    fetcher: Any
    adapters: dict
    llm: Any  # dh.llm.Client-like (chat_json) or None = rules only
    fx: Any
    customs: Any
    bandit: Bandit
    rng: random.Random
    cards_root: Path
    shots_root: Path
    dry: bool = False
    now: Callable[[], datetime] = utcnow
    push: Callable[..., Awaitable[bool]] = notify.push_tier1
    screenshot: Callable[..., Awaitable[bool]] = verify.screenshot
    gpu_sample: Callable[[], Awaitable[float | None]] = books.sample_gpu_watts
    items: list[dict] | None = None
    insights: Any = None  # dh.sources.ebay_insights.EbayInsights (session-wide: it disables itself once) or None
    photo_check: Callable[..., Awaitable[list[str]]] = photos.check
    stop: asyncio.Event | None = None
    frozen_digest: dict | None = None
    budget: int | None = None
    usage: dict = field(default_factory=lambda: {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "seconds": 0.0})

    def on_usage(self, role: str, prompt_tokens: int, completion_tokens: int, seconds: float) -> None:
        """llm.Client(on_usage=ctx.on_usage): token accounting for the books."""
        self.usage["prompt_tokens"] += int(prompt_tokens)
        self.usage["completion_tokens"] += int(completion_tokens)
        self.usage["seconds"] += float(seconds)


class _CountingLLM:
    def __init__(self, inner, stats: RoundStats):
        self._inner, self._stats = inner, stats

    async def chat_json(self, *a, **kw):
        self._stats.llm_calls += 1
        return await self._inner.chat_json(*a, **kw)


@dataclass
class Appraisal:
    listing: Listing
    lid: int
    ex: Extraction
    est: Any
    net: Any
    inp: economics.NetInputs
    risk: Any
    channel: str
    cons_usd: Decimal
    exp_usd: Decimal
    capital_usd: Decimal
    effort: str
    confidence: str
    why: list[str]
    tier: tiering.TierResult


# ------------------------------------------------------------------------------------------------ the round


class _Round:
    def __init__(self, ctx: RoundContext):
        self.ctx = ctx
        self.cfg, self.store, self.root = ctx.cfg, ctx.store, Path(ctx.root)
        self.sc = sched_cfg(ctx.cfg)
        self.stats = RoundStats()
        self.rf = RoundFetcher(ctx.fetcher)
        self.fx = MemoFx(ctx.fx)
        self.llm = _CountingLLM(ctx.llm, self.stats) if ctx.llm is not None else None
        workers = ctx.cfg.get("models")
        par = 4
        try:
            par = int(((workers.to_dict() if workers is not None else {}).get("worker") or {}).get("parallel", 4))
        except (TypeError, ValueError):
            pass
        self.sem = asyncio.Semaphore(max(1, par))
        self.parallel = max(1, par)
        self.waiting = 0
        self.llm_budget = int(self.sc["max_llm_items"])
        self.today = ctx.now().date()
        self.to_pln_rates: dict[str, Decimal] = {}
        self.to_pln = make_to_pln(self.to_pln_rates)
        self.enabled_channels = list(ctx.cfg.economics.exit_channels.enabled)
        self.channels = valuation.Channels.from_config(ctx.cfg)
        self.params = valuation.Params.from_config(ctx.cfg)
        self.est_cache: dict[tuple, Any] = {}
        self.warmed: set[tuple] = set()
        self.median_cache: dict[str, Decimal | None] = {}
        self.arm_value: dict[str, list[tuple[int | None, float]]] = defaultdict(list)
        self.arm_fetches: Counter = Counter()
        self.keys_seen: set[str] = set()
        self.insight_keys: set[str] = set()
        self.partout_cache: dict[int, Any] = {}
        self.tasks: list[asyncio.Task] = []
        self.task_sem = asyncio.Semaphore(64)
        self.shipping = economics.load_shipping()
        self.repo_web_url = str((ctx.cfg.get("publish").to_dict() if ctx.cfg.get("publish") is not None else {})
                                .get("repo_web_url") or "").rstrip("/")
        self.store.ensure_table(EVAL_SQL)
        self.store.ensure_table(MARK_COMPS_SQL)
        self.store.ensure_table(MAINT_SQL)

    # ------------------------------------------------------------ helpers
    def _stopped(self) -> bool:
        return self.ctx.stop is not None and self.ctx.stop.is_set()

    async def _warm_rates(self) -> None:
        for ccy in ("EUR", "CZK", "HUF", "GBP", "USD"):
            try:
                self.to_pln_rates[ccy] = await self.fx.rate(ccy, self.today)
                await self.fx.rate(ccy, self.today - timedelta(days=1))  # pins a dated row valuation can read
            except (FxError, Exception) as e:  # noqa: BLE001 - a missing rate only disables bounds for that ccy
                self.stats.errors.append(f"fx {ccy}: {type(e).__name__}: {e}"[:200])

    async def _price_pln(self, l: Listing) -> Decimal | None:
        p = self.to_pln(l.price, l.currency)
        if p is not None:
            return p
        try:
            return await self.fx.to_pln(l.price, l.currency, self.today)
        except Exception:  # noqa: BLE001
            return None

    def _key_median(self, key: str) -> Decimal | None:
        if key not in self.median_cache:
            since = self.ctx.now() - timedelta(days=90)
            prices = [Decimal(r["price_pln"]) for r in self.store.comps(key, since=since) if r["price_pln"]]
            rows = self.store.conn.execute(
                "select l.price from listings l join extractions e on e.listing_id=l.id "
                f"where e.key=? and upper(l.currency)='PLN' and {ledger._SALEABLE}", (key,)).fetchall()
            prices += [Decimal(r["price"]) for r in rows]
            n = int(self.sc["min_prices_for_range"])
            self.median_cache[key] = median(prices) if len(prices) >= n else None
        return self.median_cache[key]

    def _in_range(self, key: str, price_pln: Decimal | None) -> bool:
        med = self._key_median(key)
        if med is None or price_pln is None:
            return True
        lo, hi = (Decimal(str(x)) for x in self.sc["price_range"])
        return lo * med <= price_pln <= hi * med

    @staticmethod
    def _title_hash(l: Listing) -> str:
        return hashlib.sha1((l.title or "").encode()).hexdigest()[:16]

    def _needs_eval(self, lid: int, l: Listing) -> bool:
        r = self.store.conn.execute("select price, at, note from evaluations where listing_id=?", (lid,)).fetchone()
        if r is None or Decimal(r["price"]) != l.price:
            return True
        if r["note"] == "llm_pending" and self.llm is not None:
            return True  # valued on the rules alone while the LLM was off or down: ask it now
        return datetime.fromisoformat(r["at"]) < self.ctx.now() - timedelta(hours=float(self.sc["reeval_hours"]))

    def _mark_eval(self, lid: int, l: Listing, tier: int | None = None, note: str = "") -> None:
        self.store.conn.execute(
            "insert or replace into evaluations (listing_id, price, title_hash, at, tier, note) values (?,?,?,?,?,?)",
            (lid, str(l.price), self._title_hash(l), self.ctx.now().isoformat(), tier, note))
        self.store.conn.commit()

    def _stored_llm(self, lid: int, l: Listing) -> Extraction | None:
        ev = self.store.conn.execute("select title_hash from evaluations where listing_id=?", (lid,)).fetchone()
        if ev is None or ev["title_hash"] != self._title_hash(l):
            return None
        r = self.store.conn.execute("select * from extractions where listing_id=?", (lid,)).fetchone()
        if r is None or r["method"] != "llm":
            return None
        return Extraction(key=r["key"], category=r["category"], certainty=float(r["certainty"] or 0),
                          condition=r["condition"] or "unknown", defects=json.loads(r["defects"] or "[]"),
                          box_only=bool(r["box_only"]), for_parts=bool(r["for_parts"]), wanted_ad=bool(r["wanted_ad"]),
                          bundle_items=json.loads(r["bundle_items"] or "[]"), method="llm")

    # ------------------------------------------------------------ arms
    def _plan(self) -> list[Arm]:
        items = self.ctx.items if self.ctx.items is not None else load_items(self.root)
        arms = build_arms(items, self.ctx.adapters, list(self.sc["feed_sources"]))
        usable = []
        for a in arms:
            ad = self.ctx.adapters[a.source]
            dom = getattr(ad, "domain", "") or a.source
            if getattr(ad, "enabled", True) is False:
                continue  # e.g. eBay without API keys: reported once by the adapter, not an arm
            if self.ctx.fetcher.disabled(dom) or self.ctx.fetcher.blocked(dom):
                self.stats.skipped_arms += 1
                continue
            usable.append(a)
        fixed = [a for a in usable if a.sentinel or a.feed]
        free = [a for a in usable if not (a.sentinel or a.feed)]
        budget = self.ctx.budget or int(self.sc["budget"])
        self.stats.budget = budget
        per_src: Counter = Counter(a.source for a in fixed)
        cap = int(self.sc["max_searches_per_source"])
        chosen = list(fixed)
        by_id = {a.id: a for a in free}
        for aid in self.ctx.bandit.order(list(by_id), self.ctx.rng):
            if len(chosen) >= budget + len(fixed):
                break
            a = by_id[aid]
            if per_src[a.source] >= cap:
                continue
            per_src[a.source] += 1
            chosen.append(a)
        self.stats.arms = len(chosen)
        self.stats.sentinel_arms = sum(a.sentinel for a in chosen)
        self.stats.feed_arms = sum(a.feed for a in chosen)
        return chosen

    # ------------------------------------------------------------ sweep one source
    async def _run_source(self, source: str, arms: list[Arm]) -> None:
        ad = self.ctx.adapters[source]
        dom = getattr(ad, "domain", "") or source
        s = self.stats.sources.setdefault(source, {"searches": 0, "ok": 0, "failed": 0, "listings": 0})
        newly: list[int] = []
        for arm in arms:
            if self._stopped() or self.ctx.fetcher.disabled(dom):
                break
            before = sum(self.rf.requests.values())
            ts = self.ctx.now()
            s["searches"] += 1
            try:
                found = await ad.search(arm.query, self.rf, self.to_pln)
            except AdapterError as e:
                s["failed"] += 1
                ledger.record_sweep(self.store, source, arm.id, ts, str(e)[:300])
                self.arm_fetches[arm.id] += max(1, sum(self.rf.requests.values()) - before)
                continue
            except Exception as e:  # noqa: BLE001 - one broken adapter must not end the round
                s["failed"] += 1
                ledger.record_sweep(self.store, source, arm.id, ts, f"{type(e).__name__}: {e}"[:300])
                self.store.add_health(source, "adapter_error", f"{type(e).__name__}: {e}"[:500])
                self.arm_fetches[arm.id] += 1
                continue
            self.arm_fetches[arm.id] += max(1, sum(self.rf.requests.values()) - before)
            found = [l for l in found if l.source == source and l.price is not None and l.price > 0]
            s["ok"] += 1
            s["listings"] += len(found)
            self.stats.listings_seen += len(found)
            ledger.observe(self.store, source, found, ts, arm=None if arm.feed else arm.id)
            if not arm.feed:
                newly += ledger.sweep_disappeared(self.store, source, ts)
            for l in found:
                row = self.store.conn.execute("select id from listings where source=? and source_id=?",
                                              (source, l.source_id)).fetchone()
                if row is None or not self._needs_eval(row["id"], l):
                    continue
                self.tasks.append(asyncio.create_task(self._guarded(l, row["id"], arm, ad)))
        await self._confirm_disappeared(ad, source, newly)

    async def _run_mail(self, name: str, ad) -> Arm:
        """Poll the alert mailbox ONCE per round; `commit()` acks the mails only after their listings are persisted
        (at-least-once: a failed commit re-delivers next round, and the listing upsert is idempotent). Its listings carry their platform as source
        (olx, allegro, kleinanzeigen...): each is a sighting without an arm, so it can never make other listings
        disappear. They are never detail-fetched, and `alive` raises, so they stay unverified (<= medium)."""
        arm = Arm(ledger.arm_id(name, ""), name, Query(text="", arm_id=ledger.arm_id(name, "")), f"mail:{name}", feed=True)
        s = self.stats.sources.setdefault(name, {"searches": 0, "ok": 0, "failed": 0, "listings": 0})
        s["searches"] += 1
        self.arm_fetches[arm.id] += 1
        try:
            found = await ad.poll(self.ctx.fetcher)
        except Exception as e:  # noqa: BLE001 - AdapterError (IMAP) or anything else: the round goes on
            s["failed"] += 1
            self.store.add_health(name, "poll_failed", f"{type(e).__name__}: {e}"[:300])
            return arm
        ts = self.ctx.now()
        found = [l for l in found if l.price is not None and l.price > 0]
        s["ok"] += 1
        s["listings"] += len(found)
        self.stats.listings_seen += len(found)
        self.stats.mail_listings += len(found)
        by_src: dict[str, list[Listing]] = defaultdict(list)
        for l in found:
            by_src[l.source].append(l)
        for src, ls in by_src.items():
            ledger.observe(self.store, src, ls, ts)  # persisted (upsert + observation) before the mail is acked
        commit = getattr(ad, "commit", None)
        if commit is not None:
            try:
                self.stats.mail_committed += int(await commit() or 0)
            except Exception as e:  # noqa: BLE001 - AdapterError on IMAP trouble: mail stays unseen, retried next round
                self.store.add_health(name, "commit_failed", f"{type(e).__name__}: {e}"[:300])
        for l in found:
            row = self.store.conn.execute("select id from listings where source=? and source_id=?",
                                          (l.source, l.source_id)).fetchone()
            if row is not None and self._needs_eval(row["id"], l):
                self.tasks.append(asyncio.create_task(self._guarded(l, row["id"], arm, ad)))
        return arm

    def _maintenance(self) -> None:
        """Daily: photo hashes, raw snapshots and saved alert mails older than the retention (90 days) are deleted."""
        now = self.ctx.now()
        r = self.store.conn.execute("select at from maintenance where task='daily'").fetchone()
        if r is not None and now - datetime.fromisoformat(r["at"]) < timedelta(hours=float(self.sc["maintenance_hours"])):
            return
        days = int(self.sc["retention_days"])
        out = {}
        try:
            out["photo_hashes"] = photos.purge(self.store, now)
        except Exception as e:  # noqa: BLE001
            self.stats.errors.append(f"photos.purge: {e}"[:200])
        for n, ad in self.ctx.adapters.items():
            if is_mail_source(ad) and hasattr(ad, "purge"):
                try:
                    out[f"{n}_eml"] = ad.purge(older_than_days=days)
                except Exception as e:  # noqa: BLE001
                    self.stats.errors.append(f"{n}.purge: {e}"[:200])
        purge = getattr(self.ctx.fetcher, "purge_snapshots", None)
        if purge is not None:
            try:
                out["snapshot_files"] = purge(days)
            except Exception as e:  # noqa: BLE001
                self.stats.errors.append(f"purge_snapshots: {e}"[:200])
        self.store.conn.execute("insert or replace into maintenance (task, at) values ('daily', ?)", (now.isoformat(),))
        self.store.conn.commit()
        self.stats.maintenance = out

    async def _confirm_disappeared(self, ad, source: str, ids: list[int]) -> None:
        self.stats.disappeared += len(ids)
        for lid in ids[: int(self.sc["alive_confirm_per_source"])]:
            if self._stopped():
                return
            l = verify.load_listing(self.store, lid)
            if l is None:
                continue
            try:
                ok, price = await ad.alive(l, self.ctx.fetcher)
            except AdapterError:
                continue  # not confirmed either way: the ledger keeps its inference
            except Exception as e:  # noqa: BLE001
                self.store.add_health(source, "alive_error", f"{type(e).__name__}: {e}"[:300])
                continue
            if ok and price is not None and price > 0:
                # seen alive: a sighting without an arm clears disappeared_at and records no sweep
                ledger.observe(self.store, source, [dataclasses.replace(l, price=price)], self.ctx.now())
                self.stats.alive_cleared += 1

    # ------------------------------------------------------------ evaluate one listing
    async def _guarded(self, l: Listing, lid: int, arm: Arm, ad) -> None:
        async with self.task_sem:
            try:
                await self._evaluate(l, lid, arm, ad)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - one bad listing must not end the round
                log.exception("evaluate %s", l.url)
                self.stats.errors.append(f"{l.source}:{l.source_id}: {type(e).__name__}: {e}"[:300])

    async def _evaluate(self, l: Listing, lid: int, arm: Arm, ad) -> None:
        self.stats.processed += 1
        rules = rules_extract(l.title, l.description, (l.country or "pl").lower())
        if rules.wanted_ad or rules.box_only or rules.for_parts:
            self.store.save_extraction(lid, rules)
            self._mark_eval(lid, l, note="bait")
            return self.stats.drop("bait_or_parts")
        if rules.key is None and (not _HW.search(l.title or "") or _SYSTEM_WORDS.search(l.title or "")):
            self.store.save_extraction(lid, rules)
            self._mark_eval(lid, l, note="no_key")
            return self.stats.drop("no_key")
        price_pln = await self._price_pln(l)
        if rules.key and rules.key != PC_KEY and not self._in_range(rules.key, price_pln):
            self.store.save_extraction(lid, rules)
            self._mark_eval(lid, l, note="price_range")
            return self.stats.drop("price_range")

        ex = rules
        pending = False  # an uncertain listing valued without the LLM is re-evaluated once the LLM is there
        if rules.key == PC_KEY:
            pass  # whole PC: dh.partout finds and values the parts (its own LLM step), not the product extractor
        elif rules.certainty < extract.RULES_CERTAIN:
            stored = self._stored_llm(lid, l)
            if stored is not None:
                ex = stored
            elif self.llm is not None:
                if self.llm_budget <= 0:
                    self.stats.llm_deferred += 1
                    return  # not marked evaluated: next round
                self.llm_budget -= 1
                self.stats.llm_items += 1
                self.waiting += 1
                self.stats.llm_max_wait = max(self.stats.llm_max_wait, max(0, self.waiting - self.parallel))
                async with self.sem:
                    self.waiting -= 1
                    ex = await extract.extract(l, rules, self.llm, self.root)
                if "llm_unavailable" in ex.notes:
                    pending = True
            else:
                ex = dataclasses.replace(rules, notes=[*rules.notes, "llm_off"])
                pending = True
        self.store.save_extraction(lid, ex)
        if not ex.key or ex.box_only or ex.wanted_ad or ex.for_parts:
            self._mark_eval(lid, l, note="llm_pending" if pending else "no_key_after_llm")
            return self.stats.drop("no_key")
        if ex.key != rules.key and ex.key != PC_KEY and not self._in_range(ex.key, price_pln):
            self._mark_eval(lid, l, note="price_range")
            return self.stats.drop("price_range")
        self.stats.candidates += 1
        self.keys_seen.add(ex.key)
        tier = await self._deal_path(l, lid, ex, arm, price_pln, ad)
        self._mark_eval(lid, l, tier=tier, note="llm_pending" if pending else "")

    # ------------------------------------------------------------ valuation / economics / risk / tier
    def _estimate(self, key: str, cond: str | None, ch: str, lid: int):
        k = (key, cond, ch, lid)
        if k not in self.est_cache:
            self.est_cache[k] = valuation.estimate(self.store, key, cond, ch, self.ctx.now(), params=self.params,
                                                   channels=self.channels, exclude_listing_ids=[lid])
        return self.est_cache[k]

    def _shipping(self, l: Listing, category: str) -> tuple[Decimal, Decimal, float | None] | None:
        """(inbound, outbound, pickup_km) or None when the listing cannot be priced (pickup with no location)."""
        out = self.shipping.get("outbound_pln") or {}
        outbound = Decimal(str(out.get(category, out.get("default", 25))))
        origin = economics.norm_country(l.country or "PL")
        if l.pickup_only or l.ships is False:
            if l.lat is None or l.lon is None:
                return None
            return Decimal("0"), outbound, round(haversine_km(KRAKOW, (l.lat, l.lon)), 1)
        inb = self.shipping.get("inbound_pln") or {}
        key = "PL" if origin == "PL" else ("EU" if origin in economics.EU else "non_EU")
        return Decimal(str(inb.get(key, 0))), outbound, None

    async def _insights(self, key: str) -> None:
        """eBay Marketplace Insights sold comps for a key, once per key and at most `ebay_insights_keys` per round,
        ingested BEFORE the key is valued so this round already uses them. [] when the scope is not granted."""
        ins = self.ctx.insights
        if ins is None or key == PC_KEY or key in self.insight_keys or getattr(ins, "enabled", True) is False:
            return
        if len(self.insight_keys) >= int(self.sc["ebay_insights_keys"]):
            return
        self.insight_keys.add(key)
        self.stats.insights_calls += 1
        try:
            rows = await ins.sold_comps(key, self.ctx.fetcher)
            if rows:
                self.stats.comps_ingested += await valuation.ingest_comps(self.store, rows, self.fx)
                self.est_cache = {k: v for k, v in self.est_cache.items() if k[0] != key}
                self.median_cache.pop(key, None)
        except Exception as e:  # noqa: BLE001 - a comps feed failure never stops valuation
            self.stats.errors.append(f"insights {key}: {type(e).__name__}: {e}"[:200])

    async def _partout(self, l: Listing, lid: int, ex: Extraction):
        """Part-out exit estimate of a whole PC (cached per listing for the round). Uses the worker LLM for the parts
        the rules miss, within the round's LLM item budget and worker slots."""
        if lid in self.partout_cache:
            return self.partout_cache[lid]
        client = None
        if self.llm is not None and self.llm_budget > 0:
            self.llm_budget -= 1
            self.stats.llm_items += 1
            client = self.llm
        if client is not None:
            async with self.sem:
                est = await partout.value(l, ex, self.store, self.ctx.now(), client, params=self.params,
                                          channels=self.channels)
        else:
            est = await partout.value(l, ex, self.store, self.ctx.now(), None, params=self.params, channels=self.channels)
        self.partout_cache[lid] = est
        if est is not None:
            self.stats.partout += 1
        return est

    async def _appraise(self, l: Listing, lid: int, ex: Extraction, price_pln: Decimal | None,
                        verified_age_s: float | None = None, stock_photo: bool = False) -> Appraisal | None:
        cfg = self.cfg
        cond = ex.condition if ex.condition in ("new", "like_new") else "used"
        if ex.key == PC_KEY:
            est = await self._partout(l, lid, ex)
            ests = {est.channel: est} if est is not None else {}
            if est is not None:  # effort sees the parts (>= 3 valued parts -> heavy)
                ex = dataclasses.replace(ex, bundle_items=partout.parts_of(est))
        else:
            await self._insights(ex.key)
            wkey = (ex.key, cond)
            if wkey not in self.warmed:
                self.warmed.add(wkey)
                try:
                    await valuation.warm_fx(self.store, self.fx, ex.key, cond, self.ctx.now(), params=self.params,
                                            channels=self.channels)
                except Exception as e:  # noqa: BLE001 - comps without a rate are skipped by estimate
                    self.stats.errors.append(f"warm_fx {ex.key}: {e}"[:200])
            need = int(self.sc["min_estimate_comps"])  # one stray ask is not a market price
            ests = {ch: e for ch in self.enabled_channels
                    if (e := self._estimate(ex.key, cond, ch, lid)) is not None and len(e.comps) >= need}
        if not ests:
            self.stats.drop("no_valuation")
            return None
        category = CATEGORY_FOR_ECONOMICS.get(ex.category or "", "gpu")
        ship = self._shipping(l, category)
        if ship is None:
            self.stats.drop("pickup_unknown_distance")
            return None
        inbound, outbound, km = ship
        ref = max(ests.values(), key=lambda e: e.n_effective)
        rk = riskmod.assess(l, ex, ref, cfg, price_pln=price_pln, stock_photo=stock_photo, root=self.root)
        if rk.hard_exclude:
            self.stats.drop("hard_exclude")
            return None

        def inputs(ch: str, e, p_scam: float) -> economics.NetInputs:
            return economics.NetInputs(
                buy_price=l.price, buy_ccy=l.currency, origin_country=l.country or "PL",
                seller_is_business=bool(l.is_business), has_vat_invoice=False, exit_channel=ch,
                exit_price_cons_pln=q2(e.p25_pln), exit_price_exp_pln=q2(e.median_pln), inbound_ship_pln=inbound,
                outbound_ship_pln=outbound, pickup_km=km, items=1, day=self.today, category=category,
                p_scam=round(float(p_scam), 4))

        results, inps = {}, {}
        for ch, e in ests.items():
            inp = inputs(ch, e, rk.p_scam)
            try:
                results[ch] = await economics.net(inp, cfg, self.fx, self.ctx.customs)
                inps[ch] = inp
            except Exception as err:  # noqa: BLE001 - FX/duty lookup failure: this channel cannot be priced
                self.stats.errors.append(f"net {ex.key} {ch}: {type(err).__name__}: {err}"[:200])
        try:
            ch, net = economics.best_exit(results, None if ex.key == PC_KEY else self.enabled_channels)
        except ValueError:
            self.stats.drop("no_exit")
            return None
        est = ests[ch]
        rk2 = riskmod.assess(l, ex, est, cfg, price_pln=price_pln, stock_photo=stock_photo, root=self.root)
        if rk2.hard_exclude:
            self.stats.drop("hard_exclude")
            return None
        inp = inps[ch]
        if round(rk2.p_scam, 4) != inp.p_scam:
            inp = inputs(ch, est, rk2.p_scam)
            net = await economics.net(inp, cfg, self.fx, self.ctx.customs)
        if "pickup_out_of_range" in net.flags:
            self.stats.drop("pickup_out_of_range")
            return None
        cons_usd = await self.fx.pln_to_usd(net.cons_net_pln, self.today)
        exp_usd = await self.fx.pln_to_usd(net.exp_net_pln, self.today)
        cap_usd = net.capital_usd if net.capital_usd is not None else await self.fx.pln_to_usd(net.capital_pln, self.today)
        effort = tiering.effort_class(l, ch, net.flags, est, extraction=ex)
        conf, why = tiering.confidence(ex, est, rk2, verified_age_s, cap_usd, cfg.rules)
        tr = tiering.assign(cons_usd, exp_usd, effort, conf, cap_usd, cfg.rules)
        return Appraisal(l, lid, ex, est, net, inp, rk2, ch, cons_usd, exp_usd, cap_usd, effort, conf, why, tr)

    def _deal(self, a: Appraisal, evidence: str = "{}") -> Deal:
        return Deal(id=None, listing_ref=a.lid, key=a.ex.key, tier=a.tier.tier, star=a.tier.star, effort=a.effort,
                    confidence=a.confidence, cons_net_pln=a.net.cons_net_pln, exp_net_pln=a.net.exp_net_pln,
                    cons_net_usd=a.cons_usd, exp_net_usd=a.exp_usd, capital_usd=a.capital_usd,
                    breakdown_json=cards.breakdown_json(a.net), evidence_json=evidence,
                    reasons=list(a.tier.reasons) + [f"confidence: {w}" for w in a.why], created_at=self.ctx.now())

    async def _deal_path(self, l: Listing, lid: int, ex: Extraction, arm: Arm, price_pln, ad) -> int | None:
        a = await self._appraise(l, lid, ex, price_pln)
        if a is None or a.tier.tier is None:
            if a is not None:
                self.stats.drop("below_floor")
            return None
        buy_pln = await self._price_pln(l) or Decimal(0)
        pre_ev = {"buy_pln": str(q2(buy_pln))}
        if a.ex.key == PC_KEY:
            pre_ev["components"] = partout.parts_of(a.est)
        if dedupe.is_duplicate(self.store, dataclasses.replace(self._deal(a), evidence_json=json.dumps(pre_ev))):
            self.stats.duplicates += 1
            return a.tier.tier
        verified = False
        if a.tier.tier <= 3:
            if not is_mail_source(ad):  # mailbox (Allegro/OLX) listings: the e-mail is all we may read
                try:
                    l = await ad.detail(l, self.ctx.fetcher)
                    self.arm_fetches[arm.id] += 1
                    self.store.upsert_listing(l)
                except AdapterError as e:
                    self.stats.errors.append(f"detail {l.url}: {e}"[:200])
                except Exception as e:  # noqa: BLE001
                    self.stats.errors.append(f"detail {l.url}: {type(e).__name__}: {e}"[:200])
            try:
                pflags = await self.ctx.photo_check(l, self.ctx.fetcher, self.store)
            except Exception as e:  # noqa: BLE001 - the photo check is a signal, never a blocker
                pflags = []
                self.stats.errors.append(f"photos {l.url}: {type(e).__name__}: {e}"[:200])
            stock = photos.FLAG in pflags
            self.stats.photo_reuse += int(stock)
            r2 = rules_extract(l.title, l.description, (l.country or "pl").lower())
            ex = dataclasses.replace(ex, for_parts=ex.for_parts or r2.for_parts, wanted_ad=ex.wanted_ad or r2.wanted_ad,
                                     box_only=ex.box_only or r2.box_only,
                                     defects=sorted(set(ex.defects) | set(r2.defects)),
                                     condition="for_parts" if (ex.for_parts or r2.for_parts) else ex.condition)
            self.store.save_extraction(lid, ex)
            a = await self._appraise(l, lid, ex, price_pln, stock_photo=stock)
            if a is None or a.tier.tier is None:
                return None
            if a.tier.tier <= 2:
                try:
                    alive, now_price = await ad.alive(l, self.ctx.fetcher)
                    self.arm_fetches[arm.id] += 1
                except AdapterError:
                    alive, now_price = None, None  # unverifiable: keep the deal, unverified
                if alive is False:
                    self.stats.drop("gone")
                    return None
                if alive and now_price is not None and now_price == l.price:
                    verified = True
                    a2 = await self._appraise(l, lid, ex, price_pln, verified_age_s=0.0, stock_photo=stock)
                    if a2 is not None and a2.tier.tier is not None:
                        a = a2
        return await self._save(a, arm, verified)

    async def _save(self, a: Appraisal, arm: Arm, verified: bool) -> int:
        l, tier = a.listing, a.tier.tier
        rate, rdate = await self.fx.rate_with_date(l.currency, self.today)
        shots: dict = {}
        if tier == 1:
            p = Path(self.ctx.shots_root) / self.today.isoformat() / f"{a.lid}-discovery.png"
            if await self.ctx.screenshot(l.url, p, self.ctx.fetcher):
                shots["discovery"] = str(p)
        ev = cards.build_evidence(a.inp, l, a.est, a.risk, fx_rate=rate, fx_date=rdate,
                                  buy_pln=await self._price_pln(l) or Decimal(0), screenshots=shots)
        if a.ex.key == PC_KEY:  # dedupe tells two PCs of one seller apart by their parts
            ev = json.dumps({**json.loads(ev), "components": partout.parts_of(a.est)})
        deal = self._deal(a, ev)
        deal.id = self.store.save_deal(deal)
        if verified:
            verify.record_verification(self.store, deal.id, True)
        deal = self.store.get_deal(deal.id)
        path = cards.write(deal, self.ctx.cards_root, cards.render(deal, l, a.est, a.net, a.risk, self.root))
        self.stats.deals_by_tier[tier] = self.stats.deals_by_tier.get(tier, 0) + 1
        self.stats.deals.append({"id": deal.id, "tier": tier, "star": deal.star, "key": deal.key,
                                 "cons_net_usd": str(deal.cons_net_usd), "exp_net_usd": str(deal.exp_net_usd),
                                 "confidence": deal.confidence, "effort": deal.effort, "channel": a.channel,
                                 "source": l.source, "url": l.url, "verified": verified})
        self.arm_value[arm.id].append((tier, float(deal.exp_net_usd)))
        if tier == 1 and not self.ctx.dry:
            day = deal.created_at.astimezone(timezone.utc).strftime("%Y-%m-%d")
            card_url = f"{self.repo_web_url}/blob/log/deals/{day}/{path.name}" if self.repo_web_url else ""
            await self.ctx.push(deal, card_url, store=self.store, cfg=self.cfg, root=self.root, listing_url=l.url,
                                source=l.source, city=l.city)
        return tier

    # ------------------------------------------------------------ after the sweeps
    async def _ebay_comps(self) -> None:
        ads = [a for n, a in self.ctx.adapters.items() if n.startswith("ebay_") and getattr(a, "enabled", False)]
        if not ads:
            return
        for key in sorted(self.keys_seen)[: int(self.sc["ebay_comp_keys"])]:
            for ad in ads:
                try:
                    rows = await ad.active_comps(key, self.ctx.fetcher)
                    self.stats.comps_ingested += await valuation.ingest_comps(self.store, rows, self.fx)
                except Exception as e:  # noqa: BLE001
                    self.stats.errors.append(f"ebay comps {key}: {type(e).__name__}: {e}"[:200])

    def _owner_sales(self) -> None:
        rows = self.store.conn.execute(
            "select m.id, m.deal_id, m.price_pln, m.created_at from marks m left join mark_comps c on c.mark_id=m.id "
            "where m.status='sold' and m.price_pln is not null and c.mark_id is null").fetchall()
        for m in rows:
            deal = self.store.get_deal(m["deal_id"])
            self.store.conn.execute("insert or ignore into mark_comps (mark_id) values (?)", (m["id"],))
            if deal is None or not deal.key:
                continue
            ev = json.loads(deal.evidence_json or "{}")
            ch = (ev.get("net_inputs") or {}).get("exit_channel") or "allegro"
            cond = self.store.conn.execute("select condition from extractions where listing_id=?",
                                           (deal.listing_ref,)).fetchone()
            c = (cond["condition"] if cond else None) or "used"
            at = datetime.fromisoformat(str(m["created_at"]).replace(" ", "T"))
            at = at if at.tzinfo else at.replace(tzinfo=timezone.utc)
            self.store.add_comp(deal.key, "used" if c == "unknown" else c, ch, Decimal(m["price_pln"]), at,
                                "owner_sale", f"mark:{m['id']}")
            self.stats.owner_sale_comps += 1
        self.store.conn.commit()

    def _adapt_budget(self) -> None:
        base, cur = int(self.sc["budget"]), self.stats.budget
        if self.stats.llm_max_wait == 0:
            nxt = min(int(self.sc["budget_max"]), math.ceil(cur * float(self.sc["budget_boost"])))
        elif self.stats.llm_max_wait > 2 * self.parallel:
            nxt = max(base, math.floor(cur / float(self.sc["budget_boost"])))
        else:
            nxt = cur
        self.ctx.budget = nxt

    # ------------------------------------------------------------ main
    async def run(self) -> RoundStats:
        ctx, st = self.ctx, self.stats
        if ctx.frozen_digest is not None:
            assert_frozen_unchanged(self.cfg, ctx.frozen_digest)
        started = ctx.now()
        t0 = time.monotonic()
        st.started_at = started.isoformat()
        ctx.usage.update({"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "seconds": 0.0})
        sampler = books.GpuSampler(float((self.cfg.get("books").to_dict() if self.cfg.get("books") is not None
                                          else {}).get("gpu_sample_s", 15)), ctx.gpu_sample)
        sampler.start()
        try:
            self._maintenance()
            await self._warm_rates()
            arms = self._plan()
            by_src: dict[str, list[Arm]] = defaultdict(list)
            for a in arms:
                by_src[a.source].append(a)
            sweeps = [asyncio.create_task(self._run_source(s, a)) for s, a in by_src.items()]
            mail = [asyncio.create_task(self._run_mail(n, ad)) for n, ad in self.ctx.adapters.items()
                    if is_mail_source(ad)]
            sweeps += mail
            timeout = float(self.sc["round_timeout_s"])
            try:
                await asyncio.wait_for(asyncio.gather(*sweeps), timeout)
                arms += [t.result() for t in mail]
                while self.tasks:  # evaluation tasks may still be running (LLM, detail, verify)
                    pending, self.tasks = self.tasks, []
                    await asyncio.wait_for(asyncio.gather(*pending), max(1.0, timeout - (time.monotonic() - t0)))
            except asyncio.TimeoutError:
                st.errors.append(f"round timeout after {timeout:.0f}s")
                for t in [*sweeps, *self.tasks]:
                    t.cancel()
                await asyncio.gather(*sweeps, *self.tasks, return_exceptions=True)
            if not ctx.dry:
                try:
                    st.pushes_retried = await notify.retry_pending(self.store, self.cfg, self.root)
                except Exception as e:  # noqa: BLE001
                    st.errors.append(f"retry_pending: {e}"[:200])
            await self._ebay_comps()
            self._owner_sales()
        finally:
            watts = await sampler.stop()
        st.seconds = round(time.monotonic() - t0, 1)
        st.llm_tokens = int(ctx.usage["prompt_tokens"]) + int(ctx.usage["completion_tokens"])
        # requests the arms cost: real search requests (a cached page still costs its arm one slot), details, alive
        st.fetches = int(sum(self.arm_fetches.values()))
        ended = ctx.now()
        rid = self.store.log_round(started, ended, None, st.listings_seen, sum(st.deals_by_tier.values()), "")
        st.round_id = rid
        cost = books.record_round(self.store, rid, self.cfg, seconds=st.seconds, gpu_watts=watts,
                                  llm={**ctx.usage, "calls": st.llm_calls}, fetches=st.fetches)
        st.energy_pln = str(cost["energy_pln"])
        executed = {a.id for a in arms if a.id in self.arm_fetches}
        for aid in executed:
            ctx.bandit.update(aid, value_units(self.arm_value.get(aid, [])), int(self.arm_fetches[aid]))
        ctx.bandit.discount()
        self._adapt_budget()
        self.store.conn.execute("update rounds set notes=? where id=?", (json.dumps(st.to_dict(), default=str), rid))
        self.store.conn.commit()
        return st


async def run_round(ctx: RoundContext) -> RoundStats:
    """One round (see module doc). Raises config.FrozenChanged when the frozen layer changed since start."""
    return await _Round(ctx).run()


def summary_line(st: RoundStats) -> str:
    hit = ", ".join(f"{s} {v['ok']}/{v['searches']} ({v['listings']})" for s, v in sorted(st.sources.items()))
    tiers = ", ".join(f"T{k}: {v}" for k, v in sorted(st.deals_by_tier.items())) or "none"
    return (f"round {st.round_id}: {st.seconds:.0f}s, {st.arms} arms ({st.sentinel_arms} sentinel, {st.feed_arms} feed), "
            f"budget {st.budget}; sources ok/searches (listings): {hit or '-'}; listings {st.listings_seen}, "
            f"evaluated {st.processed}, candidates {st.candidates}, LLM items {st.llm_items} ({st.llm_calls} calls, "
            f"{st.llm_tokens} tokens, {st.llm_deferred} deferred); deals {tiers}; duplicates {st.duplicates}; "
            f"disappeared {st.disappeared} (alive {st.alive_cleared}); energy {st.energy_pln} PLN")
