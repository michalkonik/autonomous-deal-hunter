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
import contextlib
import dataclasses
import hashlib
import json
import logging
import math
import random
import re
import sqlite3
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

from . import books, cards, dedupe, economics, extract, ledger, notify, partout, photos, risk as riskmod, shipping, tiering, valuation, valuegate, verify
from . import triage as triagemod
from .llm import LLMUnavailable
from .bandit import Bandit, value_units
from .config import Config, assert_frozen_unchanged
from .match import has_papers, rules_extract
from .models import Deal, Extraction, Listing
from .money import RecordingFx, q2
from .playbook_io import load_items
from .sources.base import KIND_PATTERNS, AdapterError, Query

log = logging.getLogger(__name__)

DEFAULTS = {
    "round_interval_s": 600, "budget": 60, "budget_max": 120, "budget_boost": 1.5, "max_searches_per_source": 12,
    "max_llm_items": 200, "alive_confirm_per_source": 5, "reeval_hours": 24, "round_timeout_s": 1500,
    "ebay_comp_keys": 10, "ebay_insights_keys": 10, "maintenance_hours": 24,
    "price_range": [0.15, 3.0], "min_prices_for_range": 3, "min_estimate_comps": 3,
    "feed_sources": ["pepper_pl", "hotukdeals", "retail"], "requeue_hours": 48, "requeue_max": 500,
    "timeout_grace_s": 60,
    "bandit": {"gamma": 0.98, "floor": 0.10, "prior": [1.0, 10.0], "new_pulls": 3},
    "trend": {"min_30d": 0.15, "min_cons_net_usd": 0},
}
SWEEP_TIMEOUT_S = 300  # default of sources.sweep_timeout_s
CATEGORY_FOR_ECONOMICS = {"gpu": "gpu", "gpu-mobile": "gpu", "ram": "ram", "ram-sodimm": "ram", "ssd": "ssd", "pc": "pc"}
PC_KEY = "pc:bundle"
DEFAULT_ORIGIN = (50.0647, 19.9450)  # Krakow: only used when config/pickup.yaml has no `origin`
DEFAULT_RETENTION_DAYS = 90
_HW = re.compile("|".join(rx for kind, rx in KIND_PATTERNS if kind in ("gpu", "ram", "disk")), re.I)
_SYSTEM_WORDS = re.compile(r"komputer|zestaw|(?<![a-z])pc(?![a-z])|laptop|notebook|desktop|rechner|počítač|pocitac|"
                           r"számítógép|szamitogep|macbook|konsol|playstation|xbox", re.I)
EVAL_SQL = """create table if not exists evaluations (
  listing_id integer primary key, price text, title_hash text, at text, tier integer, note text)"""
MARK_COMPS_SQL = "create table if not exists mark_comps (mark_id integer primary key)"
MAINT_SQL = "create table if not exists maintenance (task text primary key, at text not null)"
PUSHES_SQL = "create table if not exists deal_pushes (deal_id integer primary key, at text not null)"
TRIAGE_SQL = """create table if not exists triage (
  listing_id integer primary key, title_hash text, price text, result text not null, at text not null,
  prompt_sha text)"""
CADENCE_SQL = "create table if not exists cadence_runs (source text primary key, at text not null)"
CADENCE_SLACK_S = 30  # rounds are paced start to start: a source due "every 30 min" may run at 29.5
DEAL_INDEX_SQL = "create unique index if not exists idx_deals_listing on deals(listing_ref)"
REQUEUE_NOTES = ("deferred", "llm_pending")


_INDEX_WARNED = False  # the deal_index health event is recorded once per process, not once per round


def ensure_deal_index(store) -> bool:
    """One deal per listing (the scheduler updates it on re-evaluation). False when old duplicates prevent it
    (reported once per process)."""
    global _INDEX_WARNED
    try:
        store.ensure_table(DEAL_INDEX_SQL)
        return True
    except sqlite3.IntegrityError:
        if not _INDEX_WARNED:
            _INDEX_WARNED = True
            store.add_health("scheduler", "deal_index", "duplicate deals per listing exist: unique index not created")
        return False


def is_mail_source(ad) -> bool:
    """The alert mailbox: polled once per round (each poll consumes unseen mail), never searched per query."""
    return bool(getattr(ad, "via_mailbox", False))


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def pickup_origin(cfg) -> tuple[float, float]:
    """Where personal pickups start: config/pickup.yaml `origin: {lat, lon}`."""
    try:
        o = cfg.pickup.origin
        return float(o.lat), float(o.lon)
    except (AttributeError, KeyError, TypeError, ValueError):
        return DEFAULT_ORIGIN


def retention_days(cfg) -> tuple[int, int]:
    """(photo-hash days, raw snapshot / saved-mail days): config/sources.yaml `retention`. The old
    `scheduler.retention_days` is kept as an alias for BOTH when `sources.retention` does not set them."""
    s = cfg.get("scheduler") if cfg is not None else None
    alias = (s.to_dict() if s is not None else {}).get("retention_days")
    src = cfg.get("sources") if cfg is not None else None
    r = ((src.to_dict() if src is not None else {}).get("retention")) or {}
    base = int(alias) if alias is not None else DEFAULT_RETENTION_DAYS
    return int(r.get("photos_days", base)), int(r.get("snapshots_days", base))


def bandit_kwargs(cfg) -> dict:
    """Bandit(store, **kwargs) from config/scheduler.yaml `bandit:` (gamma, floor, prior, new_pulls)."""
    b = {**DEFAULTS["bandit"], **(sched_cfg(cfg).get("bandit") or {})}
    return {"gamma": float(b["gamma"]), "floor": float(b["floor"]), "prior": (float(b["prior"][0]), float(b["prior"][1])),
            "new_pulls": int(b["new_pulls"])}


def trend_signal(trend: float | None, cons_usd: Any, capital_usd: Any, conf: str, rules: Any, sc: dict,
                 floor_usd: Any = 0) -> tiering.TierResult | None:
    """Tier 4 with reason "trend": a rising market makes holding attractive even below the floor. Needs the key's
    30-day trend >= scheduler.trend.min_30d (+0.15), a conservative net >= scheduler.trend.min_cons_net_usd (0),
    a listing that is not hard-excluded, and capital within the cap. tiering.assign (frozen) is untouched."""
    t = {**DEFAULTS["trend"], **(sc.get("trend") or {})}
    if trend is None or conf == "excluded" or trend < float(t["min_30d"]):
        return None
    if Decimal(str(cons_usd)) < Decimal(str(t["min_cons_net_usd"])):
        return None
    if Decimal(str(capital_usd)) > Decimal(str(rules.capital_cap_usd)):
        return None
    return tiering.TierResult(4, False, Decimal(str(floor_usd)),
                              [f"trend: the key's 30-day trend is {trend:+.0%} (>= {float(t['min_30d']):+.0%}) and the "
                               f"conservative net {Decimal(str(cons_usd)):.0f} USD is not negative"])


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
        if rules_extract(text, "", "pl").key == PC_KEY:
            continue  # whole-PC queries ("gaming pc", "komputer gamingowy") are false positives
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
    updated: int = 0
    requeued: int = 0
    price_changed: int = 0
    llm_mode: str = "on"
    foreign_models: list = field(default_factory=list)
    sweep_timeouts: list = field(default_factory=list)
    cadence: dict = field(default_factory=dict)  # source -> {minutes, due, cap} for sources with a cadence item
    triage: dict = field(default_factory=dict)  # keep / demote / exclude / unavailable / cached counts
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
    #: async () -> models llama-swap runs outside the configured roles ([] = fine); None = no check (tests, no-llm)
    llm_check: Callable[[], Awaitable[list[str]]] | None = None
    stop: asyncio.Event | None = None
    frozen_digest: dict | None = None
    budget: int | None = None
    usage: dict = field(default_factory=lambda: {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "seconds": 0.0})

    def on_usage(self, role: str, prompt_tokens: int, completion_tokens: int, seconds: float) -> None:
        """llm.Client(on_usage=ctx.on_usage): token accounting for the books."""
        self.usage["prompt_tokens"] += int(prompt_tokens)
        self.usage["completion_tokens"] += int(completion_tokens)
        self.usage["seconds"] += float(seconds)

    def on_paid(self, role: str, usd: float) -> None:
        """llm.Client(on_paid=ctx.on_paid): paid-API spend of this round (USD) for the books.
        Each provider is booked on its own row so its monthly cap is not mixed with another's."""
        self.usage["paid_usd"] = float(self.usage.get("paid_usd", 0.0)) + float(usd)
        prov = None
        llm = getattr(self, "llm", None)
        if llm is not None and hasattr(llm, "provider_of"):
            try:
                prov = llm.provider_of(role)
            except Exception:  # noqa: BLE001 - a missing route still counts in the total
                prov = None
        if prov:
            by = self.usage.setdefault("paid_by_provider", {})
            by[prov] = float(by.get(prov, 0.0)) + float(usd)


class _CountingLLM:
    """Every LLM call of a round goes through here: counted, and never sent while llama-swap runs a model outside
    the configured roles. The round-start guard (`_Round._llm_guard`) is not enough: another workload can load its
    model in the middle of a round (validation, 30 Oct: one did, swapped the role model out and four calls hung
    for 60 s); the next call would make llama-swap load the role model back and evict theirs. So EVERY call checks
    llama-swap `/running` first, with no cache: concurrent calls share the one check in flight (a shared future),
    a call arriving after it finished starts a new one. A refused call raises LLMUnavailable (the listing waits as
    llm_pending) and is not counted. (A model loaded between the check and llama-swap receiving the call cannot be
    seen by any client; the window is the round trip to localhost.)"""

    def __init__(self, inner, stats: RoundStats, check: Callable[[], Awaitable[list[str]]] | None = None,
                 on_foreign: Callable[[list[str]], None] | None = None):
        self._inner, self._stats = inner, stats
        self._check, self._on_foreign = check, on_foreign
        self._inflight: asyncio.Future | None = None
        self._foreign: list[str] = []

    async def _run_check(self) -> list[str]:
        try:
            found = list(await self._check())
        except Exception as e:  # noqa: BLE001 - cannot tell: be safe, no LLM call
            found = [f"check failed: {type(e).__name__}"]
        if found and not self._foreign and self._on_foreign is not None:
            self._on_foreign(found)
        self._foreign = found
        return found

    async def _foreign_now(self) -> list[str]:
        if self._check is None:
            return []
        fut = self._inflight
        if fut is None or fut.done():
            fut = asyncio.ensure_future(self._run_check())
            self._inflight = fut
        return await asyncio.shield(fut)  # one caller's cancellation never cancels the others' shared check

    async def chat_json(self, *a, **kw):
        role = a[0] if a else kw.get("role")
        remote = False
        provider_of = getattr(self._inner, "provider_of", None)
        if callable(provider_of):
            try:
                remote = bool(provider_of(role))
            except Exception:  # noqa: BLE001
                remote = False
        # A remote call does not load or evict a llama-swap model, so another workload's model is not a reason to skip it.
        if not remote:
            foreign = await self._foreign_now()
            if foreign:
                raise LLMUnavailable("llama-swap runs a model outside the roles: " + ", ".join(foreign))
        self._stats.llm_calls += 1
        return await self._inner.chat_json(*a, **kw)

    def __getattr__(self, name):  # anything else (paid_booked, aclose, ...) is the wrapped client's
        return getattr(self._inner, name)


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
    fxr: Any = None  # money.RecordingFx: every rate this appraisal used (stored in the evidence for replay)
    triage: dict | None = None  # the finalist triage result (dh.triage), recorded on the card
    ship_pl: int | None = None  # direct-to-Poland shipping: 1/0/None (dh/shipping.py)


# ------------------------------------------------------------------------------------------------ the round


class _Round:
    def __init__(self, ctx: RoundContext):
        self.ctx = ctx
        self.cfg, self.store, self.root = ctx.cfg, ctx.store, Path(ctx.root)
        self.sc = sched_cfg(ctx.cfg)
        self.stats = RoundStats()
        self.rf = RoundFetcher(ctx.fetcher)
        self.fx = MemoFx(ctx.fx)
        self.llm = (_CountingLLM(ctx.llm, self.stats, ctx.llm_check, self._foreign_midround)
                    if ctx.llm is not None else None)
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
        # the LOCAL date, as money.Fx pins rates by date.today(): a UTC date would disagree around midnight
        self.today = ctx.now().astimezone().date()
        self.to_pln_rates: dict[str, Decimal] = {}
        self.to_pln = make_to_pln(self.to_pln_rates)
        self.enabled_channels = list(ctx.cfg.economics.exit_channels.enabled)
        self.channels = valuation.Channels.from_config(ctx.cfg)
        self.params = valuation.Params.from_config(ctx.cfg)
        self.gate = valuegate.GateParams.from_valuation(ctx.cfg["valuation"])  # dh/valuegate.py screen
        self.gate_cache: dict[tuple, valuegate.MarketData] = {}  # raw market picture per (key, condition), once per round
        self.est_cache: dict[tuple, Any] = {}
        self.ledger_cache: dict[tuple, list] = {}  # (key, condition) -> ledger comparables, once per round (I5)
        self.ask_cache: dict = {}  # median-ask index per (key, currency) for clears_at
        self.warmed: set[tuple] = set()
        self.median_cache: dict[str, Decimal | None] = {}
        self.arm_value: dict[str, list[tuple[int | None, float]]] = defaultdict(list)
        self.arm_fetches: Counter = Counter()
        self.keys_seen: set[str] = set()
        self.insight_keys: set[str] = set()
        self.partout_cache: dict[int, Any] = {}
        self.tasks: list[asyncio.Task] = []
        self.sweeps: list[asyncio.Task] = []
        self.created: list[asyncio.Task] = []  # every evaluation task of the round (cleanup on cancel)
        self.inflight: set[int] = set()  # listing ids queued this round: one evaluation per listing per round
        self.save_lock = asyncio.Lock()  # final dedupe re-check + save/update, with no await in between
        self.mail_arms: list[Arm] = []
        self.task_sem = asyncio.Semaphore(64)
        self.revalued: dict[int, Listing] = {}  # listing id -> the listing at the live price read at verification
        self.shipping = economics.load_shipping()
        self.repo_web_url = str((ctx.cfg.get("publish").to_dict() if ctx.cfg.get("publish") is not None else {})
                                .get("repo_web_url") or "").rstrip("/")
        self.origin = pickup_origin(ctx.cfg)
        self.store.ensure_table(EVAL_SQL)
        self.store.ensure_table(MARK_COMPS_SQL)
        self.store.ensure_table(MAINT_SQL)
        self.store.ensure_table(PUSHES_SQL)
        self.store.ensure_table(TRIAGE_SQL)
        if "prompt_sha" not in {r[1] for r in self.store.conn.execute("pragma table_info(triage)")}:
            self.store.conn.execute("alter table triage add column prompt_sha text")  # tables from before the key
            self.store.conn.commit()
        self.store.ensure_table(CADENCE_SQL)
        ensure_deal_index(self.store)
        self._items_cache: list[dict] | None = None

    # ------------------------------------------------------------ helpers
    def _stopped(self) -> bool:
        return self.ctx.stop is not None and self.ctx.stop.is_set()

    async def _warm_rates(self) -> None:
        for ccy in ("EUR", "CZK", "HUF", "GBP", "USD", "CHF"):
            try:
                self.to_pln_rates[ccy] = await self.fx.rate(ccy, self.today)
                await self.fx.rate(ccy, self.today - timedelta(days=1))  # pins a dated row valuation can read
            except Exception as e:  # noqa: BLE001 - a missing rate only disables bounds for that ccy
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
        if r["note"] == "deferred" or (r["note"] == "llm_pending" and self.llm is not None):
            return True  # waited for an LLM slot, or valued on the rules alone while the LLM was off: do it now
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
    def _items(self) -> list[dict]:
        if self._items_cache is None:
            self._items_cache = self.ctx.items if self.ctx.items is not None else load_items(self.root)
        return self._items_cache

    def _cadence(self, usable_sources: set[str], cap: int, budget: int) -> tuple[set[str], dict[str, int]]:
        """Playbook `cadence` items (target = one source, value = minutes between searches of it). With the round
        interval R minutes and the cadence C, the multiplier m = C / R:
        - m > 1: the source's bandit arms run only in a round at least C minutes after the last round that searched
          them (the source is skipped in between);
        - m < 1: its per-round cap is max_searches_per_source / m (rounded up, at most the budget).
        Sentinel and feed arms are never affected (the control group and one feed sweep per round).
        Returns (sources skipped this round, per-source caps)."""
        cad: dict[str, int] = {}
        for it in self._items():
            if it.get("kind") == "cadence" and str(it.get("target") or "") in usable_sources:
                try:
                    cad[str(it["target"])] = int(it["value"])
                except (TypeError, ValueError):
                    continue
        round_min = max(1e-9, float(self.sc["round_interval_s"]) / 60)
        now = self.ctx.now()
        skip, caps = set(), {}
        for src, minutes in sorted(cad.items()):
            m = minutes / round_min
            due = True
            if m > 1:
                r = self.store.conn.execute("select at from cadence_runs where source=?", (src,)).fetchone()
                if r is not None:
                    due = (now - datetime.fromisoformat(r["at"])).total_seconds() >= minutes * 60 - CADENCE_SLACK_S
            caps[src] = min(budget, math.ceil(cap / m)) if m < 1 else cap
            if not due:
                skip.add(src)
            self.stats.cadence[src] = {"minutes": minutes, "due": due, "cap": caps[src]}
        return skip, caps

    def _plan(self) -> list[Arm]:
        items = self._items()
        arms = build_arms(items, self.ctx.adapters, list(self.sc["feed_sources"]))
        usable = []
        for a in arms:
            ad = self.ctx.adapters[a.source]
            dom = getattr(ad, "domain", "") or a.source
            if getattr(ad, "enabled", True) is False:
                note = getattr(ad, "note_disabled", None)
                if note is not None:
                    note(self.store)  # one health event, deduped by the adapter; the arms are not searched
                continue
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
        skip, caps = self._cadence({a.source for a in usable}, cap, budget)
        free = [a for a in free if a.source not in skip]
        chosen = list(fixed)
        by_id = {a.id: a for a in free}
        # Multiple-play Thompson (I5 ruling): each arm runs at most once per round, so the round takes the k = budget
        # arms with the highest draws, after the new arms' guaranteed pulls and with ceil(floor x k) uniformly random
        # exploration slots. Arms skipped by the per-source cap are replaced from the rest of the draw order.
        ranked = self.ctx.bandit.select(list(by_id), budget, self.ctx.rng)
        taken = 0
        for aid in ranked:
            if taken >= budget:
                break
            a = by_id[aid]
            if per_src[a.source] >= caps.get(a.source, cap):
                continue
            per_src[a.source] += 1
            chosen.append(a)
            taken += 1
        for src in caps:
            if any(a.source == src for a in chosen if not (a.sentinel or a.feed)):
                self.store.conn.execute("insert or replace into cadence_runs (source, at) values (?,?)",
                                        (src, self.ctx.now().isoformat()))
        self.store.conn.commit()
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
                self._queue(l, row["id"], arm, ad)
        await self._confirm_disappeared(ad, source, newly)

    async def _run_mail(self, name: str, ad) -> Arm:
        """Poll the alert mailbox ONCE per round; `commit()` acks the mails only after their listings are persisted
        (at-least-once: a failed commit re-delivers next round, and the listing upsert is idempotent). Its listings carry their platform as source
        (olx, allegro, kleinanzeigen...): each is a sighting without an arm, so it can never make other listings
        disappear. They are never detail-fetched, and `alive` raises, so they stay unverified (<= medium)."""
        arm = Arm(ledger.arm_id(name, ""), name, Query(text="", arm_id=ledger.arm_id(name, "")), f"mail:{name}", feed=True)
        self.mail_arms.append(arm)
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
                self._queue(l, row["id"], arm, ad)
        return arm

    def _queue(self, l: Listing, lid: int, arm: Arm, ad) -> None:
        if lid in self.inflight:
            return  # another arm already returned it this round (Critical 1: one deal, one push)
        self.inflight.add(lid)
        t = asyncio.create_task(self._guarded(l, lid, arm, ad))
        self.tasks.append(t)
        self.created.append(t)

    def _requeue(self) -> None:
        """Listings persisted in the last `requeue_hours` whose evaluation never happened or was put off (an acked
        mail, a round cut short, an LLM slot budget, the LLM off): evaluated now, even if no search returns them."""
        notes = ("deferred",) + (("llm_pending",) if self.llm is not None else ())
        since = (self.ctx.now() - timedelta(hours=float(self.sc["requeue_hours"]))).isoformat()
        rows = self.store.conn.execute(
            f"""select l.id from listings l left join evaluations e on e.listing_id=l.id
                where l.last_seen_at >= ? and cast(l.price as real) > 0
                and (e.listing_id is null or e.note in ({",".join("?" * len(notes))}))
                order by l.last_seen_at desc limit ?""", (since, *notes, int(self.sc["requeue_max"]))).fetchall()
        mail = next((a for a in self.ctx.adapters.values() if is_mail_source(a)), None)
        for r in rows:
            l = verify.load_listing(self.store, r["id"])
            if l is None:
                continue
            ad = self.ctx.adapters.get(l.source) or mail  # platform listings from alert mails: the mailbox adapter
            if ad is None:
                continue
            arm = Arm(f"requeue:{l.source}", l.source, Query(text=""), "requeue")
            before = len(self.inflight)
            self._queue(l, r["id"], arm, ad)
            self.stats.requeued += len(self.inflight) - before

    def _maintenance(self) -> None:
        """Daily: photo hashes, raw snapshots and saved alert mails older than the retention (sources.retention, 90 days) are deleted."""
        now = self.ctx.now()
        r = self.store.conn.execute("select at from maintenance where task='daily'").fetchone()
        if r is not None and now - datetime.fromisoformat(r["at"]) < timedelta(hours=float(self.sc["maintenance_hours"])):
            return
        photo_days, snap_days = retention_days(self.cfg)
        out = {}
        try:
            out["photo_hashes"] = photos.purge(self.store, now, days=photo_days)
        except Exception as e:  # noqa: BLE001
            self.stats.errors.append(f"photos.purge: {e}"[:200])
        for n, ad in self.ctx.adapters.items():
            if is_mail_source(ad) and hasattr(ad, "purge"):
                try:
                    out[f"{n}_eml"] = ad.purge(older_than_days=snap_days)
                except Exception as e:  # noqa: BLE001
                    self.stats.errors.append(f"{n}.purge: {e}"[:200])
        purge = getattr(self.ctx.fetcher, "purge_snapshots", None)
        if purge is not None:
            try:
                out["snapshot_files"] = purge(snap_days)
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
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.store.add_health(source, "alive_error", f"{type(e).__name__}: {e}"[:300])
                continue
            if ok is False:  # the platform says it is gone: its deal is too
                verify.mark_listing_gone(self.store, lid, "listing disappeared and was confirmed gone",
                                         self.ctx.cards_root)
                continue
            if ok and price is not None and price > 0:
                # seen alive: a sighting without an arm clears disappeared_at and records no sweep
                ledger.observe(self.store, source, [dataclasses.replace(l, price=price)], self.ctx.now())
                self.stats.alive_cleared += 1

    # ------------------------------------------------------------ evaluate one listing
    async def _us_ships_to_poland(self, l: Listing, ad) -> bool:
        """US-located goods count only when the payload, or one item lookup, says they ship to Poland.

        The search filter `deliveryCountry:PL` is not enough: a US seller's domestic shipping options used to
        pass as "ships". No checker, or a failed lookup, is a no — never an assumption.
        """
        desc = l.description or ""
        if l.ships is False or "does not ship to Poland" in desc:
            return False
        if l.ships is True and "ships to Poland" in desc:
            return True
        fn = getattr(ad, "confirm_ships_to_poland", None)
        if fn is None:
            return False
        try:
            ok = bool(await fn(l))
        except Exception as e:  # noqa: BLE001 - an unconfirmed US item is not a deal
            self.stats.errors.append(f"us-ship {l.source}:{l.source_id}: {type(e).__name__}: {e}"[:200])
            return False
        l.ships = bool(ok)
        if ok and "ships to Poland" not in desc:
            l.description = (desc + "; ships to Poland").lstrip("; ")
        elif not ok and "does not ship to Poland" not in desc:
            l.description = (desc + "; does not ship to Poland").lstrip("; ")
        self.store.upsert_listing(l)
        return ok

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
        rules = rules_extract(l.title, l.description, (l.country or "pl").lower(), root=self.root)
        if rules.wanted_ad or rules.box_only or rules.for_parts:
            self.store.save_extraction(lid, rules)
            self._mark_eval(lid, l, note="bait")
            return self.stats.drop("bait_or_parts")
        if "rules_rejected" in rules.notes:
            # server RAM, a brand clash, a part number that disagrees with the title: the LLM must not invent a key
            self.store.save_extraction(lid, rules)
            self._mark_eval(lid, l, note="rules_rejected")
            return self.stats.drop("rules_rejected")
        if rules.key == PC_KEY:
            # Whole PCs are almost always false positives. Do not part them out or spend an LLM call.
            self.store.save_extraction(lid, rules)
            self._mark_eval(lid, l, note="pc_bundle")
            return self.stats.drop("pc_bundle")
        if rules.key is None and (not _HW.search(l.title or "") or _SYSTEM_WORDS.search(l.title or "")):
            self.store.save_extraction(lid, rules)
            self._mark_eval(lid, l, note="no_key")
            return self.stats.drop("no_key")
        if str(l.country or "").upper() == "US" and not await self._us_ships_to_poland(l, ad):
            self.store.save_extraction(lid, rules)
            self._mark_eval(lid, l, note="us_not_to_poland")
            return self.stats.drop("us_not_to_poland")
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
                    self._mark_eval(lid, l, note="deferred")  # re-queued at the next round's start
                    return
                self.llm_budget -= 1
                self.stats.llm_items += 1
                async with self._llm_slot():
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
        if pending and ex.key != PC_KEY and (ex.bundle_items or ex.certainty < ledger.COMP_MIN_CERTAINTY):
            # LLM off or unreachable and the rules only guess (a laptop or PC naming a GPU, a lot): never valued as
            # the bare product; it waits for the LLM (live round 29 Oct: an RTX 5090 laptop became a T3 "card")
            self._mark_eval(lid, l, note="llm_pending")
            return self.stats.drop("weak_match_waits_for_llm")
        if ex.key != rules.key and ex.key != PC_KEY and not self._in_range(ex.key, price_pln):
            self._mark_eval(lid, l, note="price_range")
            return self.stats.drop("price_range")
        self.stats.candidates += 1
        self.keys_seen.add(ex.key)
        tier = await self._deal_path(l, lid, ex, arm, price_pln, ad)
        # re-valued at the live price after verification: record THAT price, or the next sweep (which reads the same
        # live price) would see a price change and evaluate the listing again
        self._mark_eval(lid, self.revalued.pop(lid, l), tier=tier, note="llm_pending" if pending else "")

    # ------------------------------------------------------------ valuation / economics / risk / tier
    def _ledger_rows(self, key: str, cond: str | None) -> list:
        """The clears-at ledger comparables of (key, condition), computed once per round for every candidate."""
        k = (key, cond)
        if k not in self.ledger_cache:
            self.ledger_cache[k] = valuation.ledger_rows(self.store, key, cond, self.ctx.now(), params=self.params,
                                                         channels=self.channels, ask_cache=self.ask_cache)
        return self.ledger_cache[k]

    def _estimate(self, key: str, cond: str | None, ch: str, lid: int):
        k = (key, cond, ch, lid)
        if k not in self.est_cache:  # the candidate's own listing is dropped from the shared rows here
            self.est_cache[k] = valuation.estimate(self.store, key, cond, ch, self.ctx.now(), params=self.params,
                                                   channels=self.channels, exclude_listing_ids=[lid],
                                                   ledger_rows=self._ledger_rows(key, cond))
        return self.est_cache[k]

    def _shipping(self, l: Listing, category: str) -> tuple[Decimal, Decimal, float | None] | None:
        """(inbound, outbound, pickup_km) or None when the listing cannot be priced (pickup with no location)."""
        out = self.shipping.get("outbound_pln") or {}
        outbound = Decimal(str(out.get(category, out.get("default", 25))))
        origin = economics.norm_country(l.country or "PL")
        # A domestic-only classified (Tori, Kleinanzeigen, Marktplaats, 2ememain) does not ship to
        # Poland. Pricing it as the EU parcel rate made a Finnish pickup look like a 60 PLN delivery.
        if l.pickup_only or l.ships is False or shipping.domestic_classified(l.source):
            if l.lat is None or l.lon is None:
                return None
            return Decimal("0"), outbound, round(haversine_km(self.origin, (l.lat, l.lon)), 1)
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

    @contextlib.asynccontextmanager
    async def _llm_slot(self):
        """One worker slot. `llm_max_wait` = the most items ever waiting for a slot (0 = the queue never backed up)."""
        must_wait = self.sem.locked()
        if must_wait:
            self.waiting += 1
            self.stats.llm_max_wait = max(self.stats.llm_max_wait, self.waiting)
        try:
            async with self.sem:
                if must_wait:
                    self.waiting -= 1
                    must_wait = False
                yield
        finally:
            if must_wait:
                self.waiting -= 1

    async def _partout(self, l: Listing, lid: int, ex: Extraction, condition: str | None = None):
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
            async with self._llm_slot():
                est = await partout.value(l, ex, self.store, self.ctx.now(), client, params=self.params,
                                          channels=self.channels, condition=condition)
        else:
            est = await partout.value(l, ex, self.store, self.ctx.now(), None, params=self.params,
                                      channels=self.channels, condition=condition)
        self.partout_cache[lid] = est
        if est is not None:
            self.stats.partout += 1
        return est

    async def _appraise(self, l: Listing, lid: int, ex: Extraction, price_pln: Decimal | None,
                        verified_age_s: float | None = None, stock_photo: bool = False) -> Appraisal | None:
        cfg = self.cfg
        cond = valuation.benchmark_condition(ex.condition)
        if ex.key == PC_KEY:
            est = await self._partout(l, lid, ex, cond)
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
                                            channels=self.channels, rows=self._ledger_rows(ex.key, cond))
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

        fxr = RecordingFx(self.fx)  # what this appraisal is valued with is what verify and the books replay
        results, inps = {}, {}
        for ch, e in ests.items():
            inp = inputs(ch, e, rk.p_scam)
            try:
                results[ch] = await economics.net(inp, cfg, fxr, self.ctx.customs)
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
            net = await economics.net(inp, cfg, fxr, self.ctx.customs)
        if "pickup_out_of_range" in net.flags:
            self.stats.drop("pickup_out_of_range")
            return None
        cons_usd = await fxr.pln_to_usd(net.cons_net_pln, self.today)
        exp_usd = await fxr.pln_to_usd(net.exp_net_pln, self.today)
        cap_usd = net.capital_usd if net.capital_usd is not None else await fxr.pln_to_usd(net.capital_pln, self.today)
        effort = tiering.effort_class(l, ch, net.flags, est, extraction=ex)
        ship = shipping.ship_pl(l)
        if ship == 1 and effort == "heavy":
            effort = "moderate"  # direct-to-PL shipping removes the forwarding leg (owner bonus 2026-11-07)
        conf, why = tiering.confidence(ex, est, rk2, verified_age_s, cap_usd, cfg.rules)
        tr = tiering.assign(cons_usd, exp_usd, effort, conf, cap_usd, cfg.rules, ship_pl=ship == 1)
        if ship == 1:
            tr.reasons.append("ships directly to Poland: 5% floor bonus, no forwarding needed")
        elif shipping.easy_poland(l.source, l.country, ship):
            tr.reasons.append("Polish offer: easy to collect in Poland")
        if tr.tier is None:  # below the floor and not within 20%: a rising market can still make it a tier-4 watch
            tr = trend_signal(est.trend_30d, cons_usd, cap_usd, conf, cfg.rules, self.sc, tr.floor_usd) or tr
        return Appraisal(l, lid, ex, est, net, inp, rk2, ch, cons_usd, exp_usd, cap_usd, effort, conf, why, tr, fxr,
                         ship_pl=ship)

    def _deal(self, a: Appraisal, evidence: str = "{}") -> Deal:
        return Deal(id=None, listing_ref=a.lid, key=a.ex.key, tier=a.tier.tier, star=a.tier.star, effort=a.effort,
                    confidence=a.confidence, cons_net_pln=a.net.cons_net_pln, exp_net_pln=a.net.exp_net_pln,
                    cons_net_usd=a.cons_usd, exp_net_usd=a.exp_usd, capital_usd=a.capital_usd,
                    breakdown_json=cards.breakdown_json(a.net), evidence_json=evidence,
                    reasons=list(a.tier.reasons) + [f"confidence: {w}" for w in a.why], created_at=self.ctx.now(),
                    ship_pl=a.ship_pl)

    def _verifier(self, l: Listing, ad):
        """The adapter that may detail-fetch / verify `l`: its own adapter, or, for a listing that arrived through the
        alert mailbox, the platform's REAL adapter when that one may be read politely (Kleinanzeigen) or through an
        API (eBay with keys). OLX and Allegro mail listings have none: the e-mail is all we may read (unverifiable,
        capped at medium confidence)."""
        if not is_mail_source(ad):
            return ad
        real = self.ctx.adapters.get(l.source)
        if real is None or is_mail_source(real) or getattr(real, "enabled", True) is False:
            return None
        if l.source == "kleinanzeigen" or l.source.startswith("ebay"):
            return real
        return None

    async def _deal_path(self, l: Listing, lid: int, ex: Extraction, arm: Arm, price_pln, ad) -> int | None:
        vad = self._verifier(l, ad)
        a = await self._appraise(l, lid, ex, price_pln)
        if a is None or a.tier.tier is None:
            if a is not None:
                self.stats.drop("below_floor")
            return None
        a = self._value_gate(a)
        if a is None:
            return None
        if self._cross_duplicate(a, await self._price_pln(l) or Decimal(0)):
            self.stats.duplicates += 1  # the same item on another listing / platform: no detail fetch, no deal
            return a.tier.tier
        verified = False
        if a.tier.tier <= 3:
            if vad is not None:  # mailbox Allegro/OLX listings have none: the e-mail is all we may read
                try:
                    l = await vad.detail(l, self.ctx.fetcher)
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
            r2 = rules_extract(l.title, l.description, (l.country or "pl").lower(), root=self.root)
            ex = dataclasses.replace(ex, for_parts=ex.for_parts or r2.for_parts, wanted_ad=ex.wanted_ad or r2.wanted_ad,
                                     box_only=ex.box_only or r2.box_only,
                                     defects=sorted(set(ex.defects) | set(r2.defects)),
                                     condition="for_parts" if (ex.for_parts or r2.for_parts) else ex.condition)
            self.store.save_extraction(lid, ex)
            a = await self._appraise(l, lid, ex, price_pln, stock_photo=stock)
            if a is None or a.tier.tier is None:
                return None
            if a.tier.tier <= 2:  # finalist: the senior review comes BEFORE the live re-check and any push
                tri = await self._triage(l, lid, a)
                if tri.verdict == "exclude":
                    self.stats.drop("triage_exclude")  # a hard exclude, reason "triage"
                    return None
                a = self._apply_triage(a, tri)
            if a.tier.tier <= 2:
                try:
                    if vad is None:
                        raise AdapterError("not verifiable: alert e-mail only")
                    alive, now_price = await vad.alive(l, self.ctx.fetcher)
                    self.arm_fetches[arm.id] += 1
                except AdapterError:
                    alive, now_price = None, None  # unverifiable: keep the deal, unverified
                if alive is False:
                    self.stats.drop("gone")
                    verify.mark_listing_gone(self.store, lid, "listing gone at the live re-check", self.ctx.cards_root)
                    return None
                if alive and now_price is not None and now_price > 0:
                    changed = now_price != l.price
                    if changed:  # live price differs: the deal is re-valued at the price that is on offer now
                        self.stats.price_changed += 1
                        l = dataclasses.replace(l, price=now_price, seen_at=self.ctx.now())
                        self.revalued[lid] = l  # the evaluation is recorded at this price (see _evaluate)
                        ledger.observe(self.store, l.source, [l], self.ctx.now())  # sighting, no arm
                        price_pln = await self._price_pln(l)
                    verified = True  # alive, and the price just read is the one the deal is valued at
                    a2 = await self._appraise(l, lid, ex, price_pln, verified_age_s=0.0, stock_photo=stock)
                    if a2 is not None and a2.tier.tier is not None:
                        a2 = self._apply_triage(a2, tri)
                        a2 = self._value_gate(a2)  # a changed price is a changed market picture: the screen runs again
                    if a2 is None or a2.tier.tier is None:
                        if changed:
                            self.stats.drop("price_changed_below_floor")
                            verify.mark_listing_gone(self.store, lid, f"failed re-verification: price now {now_price} "
                                                     f"{l.currency}, no longer a deal", self.ctx.cards_root)
                            return None
                    else:
                        a = a2
        return await self._save(a, arm, verified)

    # ------------------------------------------------------------ value gate (dh/valuegate.py)
    def _watch(self, a: Appraisal, v: valuegate.Verdict) -> None:
        a.tier.tier = 5
        a.tier.reasons.append(v.note)
        a.why.append(v.note)

    def _value_gate(self, a: Appraisal) -> Appraisal | None:
        """Evidence-and-edge screen between pricing and saving: keeps the tier, forces it to the tier-5 watchlist
        (stored and shown, never pushed, never re-fetched), or drops the listing outright. Part-out PCs skip the
        one-key market screen (their value is the sum of their parts) and the shop-price check. A new item with
        no receipt and no guarantee must be strictly under the same model's shop price. Every kept tier still
        has to clear `min_return_on_capital`: a real but thin gap is a watch. Like-new is priced as used."""
        if not self.gate.enabled or a.ex.key is None:
            return a
        if a.ex.key != PC_KEY:
            cond = valuation.benchmark_condition(a.ex.condition)
            ck = (a.ex.key, cond)
            if ck not in self.gate_cache:
                self.gate_cache[ck] = valuegate.collect(self.store, a.ex.key, cond, self.ctx.now(), self.gate)
            buy = valuegate.buy_pln(a.inp, getattr(a, "fxr", None))
            v = valuegate.verdict(buy, self.gate_cache[ck], self.gate)
            if v.code == "gate_no_data":
                return a
            if v.action == "drop":
                self.stats.drop(f"value_gate_{v.code}")
                return None
            if v.action == "watch":
                self._watch(a, v)
                return a
            if a.ex.condition == "new":
                listing = getattr(a, "listing", None)
                papers = has_papers(getattr(listing, "title", "") or "",
                                    getattr(listing, "description", "") or "") if listing is not None else False
                shop = valuegate.paperless_new_watch(buy, self.gate_cache[ck].retail_floor, papers, self.gate)
                if shop is not None and a.tier.tier is not None and a.tier.tier < 5:
                    self._watch(a, shop)
                    return a
        # a rising-market tier 4 is under the floor on purpose; the return check must not erase that signal
        trend = any(str(r).startswith("trend:") for r in (a.tier.reasons or ()))
        thin = None if trend else valuegate.thin_margin(
            getattr(a, "cons_usd", None), getattr(a, "capital_usd", None), self.gate)
        if thin is not None and a.tier.tier is not None and a.tier.tier < 5:
            self._watch(a, thin)
        return a

    # ------------------------------------------------------------ finalist triage (dh/triage.py)
    async def _triage(self, l: Listing, lid: int, a: Appraisal) -> triagemod.Triage:
        """The senior review of a tier <= 2 candidate, reused while the listing's title and price are unchanged
        (an `unavailable` result is never reused)."""
        th, price = self._title_hash(l), str(l.price)
        prompt = self._prompt(triagemod.PROMPT_ID)
        psha = extract.prompt_sha(prompt or "")  # the playbook's prompt identity: a promoted prompt re-triages
        r = self.store.conn.execute("select title_hash, price, result, prompt_sha from triage where listing_id=?",
                                    (lid,)).fetchone()
        if r is not None and r["title_hash"] == th and r["price"] == price and r["prompt_sha"] == psha:
            self.stats.triage["cached"] = self.stats.triage.get("cached", 0) + 1
            return triagemod.Triage.from_dict(json.loads(r["result"]))
        med = getattr(a.est, "median_pln", None)
        tri = await triagemod.review(self.llm, prompt, l, market_median_pln=med)
        self.stats.triage[tri.verdict] = self.stats.triage.get(tri.verdict, 0) + 1
        if tri.verdict != "unavailable":
            self.store.conn.execute(
                "insert or replace into triage (listing_id, title_hash, price, result, at, prompt_sha) values (?,?,?,?,?,?)",
                (lid, th, price, json.dumps(tri.to_dict()), self.ctx.now().isoformat(), psha))
            self.store.conn.commit()
        return tri

    def _prompt(self, item_id: str) -> str | None:
        """A prompt item of the round's playbook; a round given only arm items falls back to the active playbook."""
        for items in (self._items(), None):
            if items is None:
                try:
                    items = load_items(self.root)
                except (OSError, ValueError):
                    return None
            hit = next((str(i.get("value") or "") for i in items if i.get("id") == item_id), None)
            if hit:
                return hit
        return None

    def _apply_triage(self, a: Appraisal, tri: triagemod.Triage | None) -> Appraisal:
        """demote -> at most tier 3; unavailable -> confidence at most medium (never tier 1 without a triage)."""
        if tri is None:
            return a
        a = dataclasses.replace(a, triage=tri.to_dict())
        if tri.verdict == "unavailable" and a.confidence == "high":
            why = [*a.why, "no finalist triage (" + "; ".join(tri.reasons)[:120] + "): capped at medium"]
            tr = tiering.assign(a.cons_usd, a.exp_usd, a.effort, "medium", a.capital_usd, self.cfg.rules)
            a = dataclasses.replace(a, confidence="medium", why=why, tier=tr if tr.tier is not None else a.tier)
        if tri.verdict == "demote" and a.tier.tier is not None and a.tier.tier < 3:
            reasons = "; ".join(tri.reasons) or "no reason given"
            a = dataclasses.replace(a, tier=tiering.TierResult(
                3, a.tier.star, a.tier.floor_usd, [*a.tier.reasons, f"triage: demoted to tier 3 ({reasons})"]))
        return a

    def _evidence_probe(self, a: Appraisal, buy_pln: Decimal) -> str:
        ev = {"buy_pln": str(q2(buy_pln))}
        if a.ex.key == PC_KEY:
            ev["components"] = partout.parts_of(a.est)
        return json.dumps(ev)

    def _existing_deal_id(self, lid: int) -> int | None:
        r = self.store.conn.execute("select id from deals where listing_ref=? order by id limit 1", (lid,)).fetchone()
        return r["id"] if r else None

    def _cross_duplicate(self, a: Appraisal, buy_pln: Decimal) -> bool:
        """Dedupe against OTHER listings only: the same listing's own deal is updated, not duplicated."""
        probe = dataclasses.replace(self._deal(a), id=self._existing_deal_id(a.lid),
                                    evidence_json=self._evidence_probe(a, buy_pln))
        return dedupe.is_duplicate(self.store, probe)

    def _update_deal(self, deal_id: int, d: Deal) -> None:
        self.store.conn.execute(
            """update deals set key=?, tier=?, star=?, effort=?, confidence=?, cons_net_pln=?, exp_net_pln=?,
               cons_net_usd=?, exp_net_usd=?, capital_usd=?, breakdown_json=?, evidence_json=?, reasons=?,
               ship_pl=?, verified_at=null, status=case when status='gone' then 'new' else status end where id=?""",
            (d.key, d.tier, int(d.star), d.effort, d.confidence, str(d.cons_net_pln), str(d.exp_net_pln),
             str(d.cons_net_usd), str(d.exp_net_usd), str(d.capital_usd), d.breakdown_json, d.evidence_json,
             json.dumps(d.reasons), d.ship_pl, deal_id))
        self.store.conn.commit()

    def _pushed(self, deal_id: int) -> bool:
        return self.store.conn.execute("select 1 from deal_pushes where deal_id=?", (deal_id,)).fetchone() is not None

    async def _save(self, a: Appraisal, arm: Arm, verified: bool) -> int:
        """Save a new deal or update this listing's deal (re-tier). Everything awaited happens before the lock; the
        final duplicate re-check, the insert/update, the card and the push reservation happen under it with no await,
        so two concurrent evaluations can never produce two deals or two pushes."""
        l, tier = a.listing, a.tier.tier
        fxr = a.fxr if a.fxr is not None else RecordingFx(self.fx)
        rate, rdate = await fxr.rate_with_date(l.currency, self.today)
        usd_rate, usd_date = await fxr.rate_with_date("USD", self.today)
        buy_pln = await fxr.to_pln(l.price, l.currency, self.today)
        shots: dict = {}
        old = self._existing_deal_id(a.lid)
        if tier == 1 and not (old is not None and self._pushed(old)):
            p = Path(self.ctx.shots_root) / self.today.isoformat() / f"{a.lid}-discovery.png"
            if await self.ctx.screenshot(l.url, p, self.ctx.fetcher):
                shots["discovery"] = str(p)
        ev = cards.build_evidence(a.inp, l, a.est, a.risk, fx_rate=rate, fx_date=rdate, buy_pln=buy_pln,
                                  screenshots=shots, fx_rates=fxr.as_json(), fx_usd=(usd_rate, usd_date))
        if a.ex.key == PC_KEY:  # dedupe tells two PCs of one seller apart by their parts
            ev = json.dumps({**json.loads(ev), "components": partout.parts_of(a.est)})
        if a.triage is not None:
            ev = json.dumps({**json.loads(ev), "triage": a.triage})
        push = False
        async with self.save_lock:
            deal = self._deal(a, ev)
            old = self._existing_deal_id(a.lid)
            if old is None:
                if dedupe.is_duplicate(self.store, deal):
                    self.stats.duplicates += 1
                    return tier
                try:
                    deal.id = self.store.save_deal(deal)
                except sqlite3.IntegrityError:  # unique listing_ref: another writer saved it first
                    old = self._existing_deal_id(a.lid)
            if old is not None:
                deal.id = old
                self._update_deal(old, deal)
                self.stats.updated += 1
            if verified:
                verify.record_verification(self.store, deal.id, True)
            deal = self.store.get_deal(deal.id)
            path = cards.write(deal, self.ctx.cards_root, cards.render(deal, l, a.est, a.net, a.risk, self.root))
            if tier == 1 and not self.ctx.dry and not self._pushed(deal.id):
                self.store.conn.execute("insert into deal_pushes (deal_id, at) values (?,?)",
                                        (deal.id, self.ctx.now().isoformat()))
                self.store.conn.commit()
                push = True  # reserved: at most one push per deal, even across rounds
        self.stats.deals_by_tier[tier] = self.stats.deals_by_tier.get(tier, 0) + 1
        self.stats.deals.append({"id": deal.id, "tier": tier, "star": deal.star, "key": deal.key,
                                 "cons_net_usd": str(deal.cons_net_usd), "exp_net_usd": str(deal.exp_net_usd),
                                 "confidence": deal.confidence, "effort": deal.effort, "channel": a.channel,
                                 "source": l.source, "url": l.url, "verified": verified, "updated": old is not None})
        self.arm_value[arm.id].append((tier, float(deal.exp_net_usd)))
        if push:
            day = deal.created_at.astimezone(timezone.utc).strftime("%Y-%m-%d")
            card_url = f"{self.repo_web_url}/blob/log/deals/{day}/{path.name}" if self.repo_web_url else ""
            try:
                await self.ctx.push(deal, card_url, store=self.store, cfg=self.cfg, root=self.root,
                                    listing_url=l.url, source=l.source, city=l.city)
            except asyncio.CancelledError:
                # SIGTERM / round cancel while the push was in flight: the reservation stays (never sent twice), the
                # push goes to the retry queue so the next run delivers it (a duplicate beats a lost tier-1 push)
                with contextlib.suppress(Exception):
                    notify.queue_push(deal, card_url, store=self.store, cfg=self.cfg, root=self.root,
                                      listing_url=l.url, source=l.source, city=l.city, reason="cancelled in flight")
                raise
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

    def _foreign_midround(self, foreign: list[str]) -> None:
        """Another workload loaded a model during this round: the rest of the round's LLM work waits (llm_pending)."""
        self.stats.llm_mode = "foreign_model"
        self.stats.foreign_models = list(foreign)
        self.store.add_health("scheduler", "foreign_model",
                              "mid-round: LLM calls stopped; llama-swap runs " + ", ".join(foreign))

    # ------------------------------------------------------------ main
    async def _llm_guard(self) -> None:
        """A model outside the configured roles is loaded (another workload): this round runs on the rules alone and
        LLM-needing listings are marked llm_pending, so the round never makes llama-swap evict that model."""
        if self.llm is None:
            self.stats.llm_mode = "off"
            return
        if self.ctx.llm_check is None:
            return
        try:
            foreign = list(await self.ctx.llm_check())
        except Exception as e:  # noqa: BLE001 - cannot tell: be safe, no LLM this round
            foreign = [f"check failed: {type(e).__name__}"]
        if foreign:
            self.llm = None
            self.stats.llm_mode = "foreign_model"
            self.stats.foreign_models = foreign
            self.store.add_health("scheduler", "foreign_model", "rules-only round; llama-swap runs " + ", ".join(foreign))

    async def _sweep(self, source: str, arms: list[Arm] | None, mail_ad=None) -> None:
        """One source's sweep under its own deadline (sources.sweep_timeout_s): a stuck source is cut alone."""
        src = self.cfg.get("sources")
        limit = float((src.to_dict() if src is not None else {}).get("sweep_timeout_s", SWEEP_TIMEOUT_S))
        coro = self._run_mail(source, mail_ad) if mail_ad is not None else self._run_source(source, arms)
        try:
            await asyncio.wait_for(coro, limit)
        except asyncio.TimeoutError:
            self.stats.sweep_timeouts.append(source)
            self.store.add_health(source, "sweep_timeout", f"sweep cut after {limit:.0f}s")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - one source's broken sweep is reported; the other sources go on
            log.exception("sweep %s", source)
            self.stats.errors.append(f"sweep {source}: {type(e).__name__}: {e}"[:300])
            self.store.add_health(source, "sweep_error", f"{type(e).__name__}: {e}"[:500])

    async def _drain(self, deadline: float, grace: float) -> None:
        """Wait for the evaluation tasks until the round deadline; past it, started work gets `grace` seconds more,
        then whatever is left is cancelled."""
        hard: float | None = None
        while self.tasks:
            pending, self.tasks = self.tasks, []
            now = time.monotonic()
            if now >= deadline and hard is None:
                hard = now + grace
                self.stats.errors.append(f"round timeout: {len(pending)} evaluation(s) get {grace:.0f}s grace")
            limit = (hard if hard is not None else deadline) - now
            _, left = await asyncio.wait(pending, timeout=max(0.0, limit))
            if left:
                if hard is not None and time.monotonic() >= hard:
                    for t in left:
                        t.cancel()
                    await asyncio.gather(*left, return_exceptions=True)
                    self.stats.errors.append(f"round timeout: {len(left)} evaluation(s) cancelled")
                    return
                self.tasks = list(left) + self.tasks

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
        arms: list[Arm] = []
        try:
            self._maintenance()
            await self._llm_guard()
            await self._warm_rates()
            arms = self._plan()
            self._requeue()  # before the sweeps: what they return again is not queued twice
            by_src: dict[str, list[Arm]] = defaultdict(list)
            for a in arms:
                by_src[a.source].append(a)
            deadline = t0 + float(self.sc["round_timeout_s"])
            self.sweeps = [asyncio.create_task(self._sweep(s_, a_)) for s_, a_ in by_src.items()]
            self.sweeps += [asyncio.create_task(self._sweep(n, None, ad)) for n, ad in self.ctx.adapters.items()
                            if is_mail_source(ad)]
            cut: set = set()
            if self.sweeps:
                _, cut = await asyncio.wait(self.sweeps, timeout=max(0.0, deadline - time.monotonic()))
            if cut:  # outer guard: unfinished sweeps stop; evaluations already queued still get their chance
                st.errors.append(f"round timeout: {len(cut)} sweep(s) cut")
                for t in cut:
                    t.cancel()
                await asyncio.gather(*cut, return_exceptions=True)
            arms += self.mail_arms
            await self._drain(deadline, float(self.sc["timeout_grace_s"]))
            if not ctx.dry:
                try:
                    st.pushes_retried = await notify.retry_pending(self.store, self.cfg, self.root)
                except Exception as e:  # noqa: BLE001
                    st.errors.append(f"retry_pending: {e}"[:200])
            await self._ebay_comps()
            self._owner_sales()
        except BaseException:
            # cancelled (SIGTERM) or failed: stop every child task and wait for it, so nothing touches the store,
            # fetcher or LLM client after the caller closes them; no bookkeeping is written for a broken round
            children = [*self.sweeps, *self.created]
            for t in children:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*children, return_exceptions=True)
            self.tasks = []
            raise
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
        booked = float(ctx.usage.get("paid_usd", 0.0))  # now in the books' month-to-date: no longer "unbooked"
        ctx.usage["paid_usd"] = 0.0
        if booked and ctx.llm is not None and hasattr(ctx.llm, "paid_booked"):
            ctx.llm.paid_booked(booked)
        executed = {a.id for a in arms if a.id in self.arm_fetches}
        for aid in executed:
            ctx.bandit.update(aid, max(0.0, value_units(self.arm_value.get(aid, []))), int(self.arm_fetches[aid]))
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
