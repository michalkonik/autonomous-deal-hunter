"""Offline judge and replay archive (spec §9.3 step 3.4; research §3.3, §3.4).

`Judge` re-runs the round's judgment for ONE listing with a given playbook, with no network and no live LLM:
rules matcher (with the playbook's aliases) -> bait / no-key / price-range filters -> cached LLM extraction (from the
store) when the rules are not certain -> the scheduler's own `_Round._appraise` (valuation, economics per exit
channel, risk with the playbook's scam keywords, confidence, tier). It is a thin adapter over dh/scheduler.py's
internals (read-only use; the dependency is reported in the Task 27-30 report): the offline judgment is the
production judgment, not a copy of it. Not reproduced offline: detail fetch, photo check, live re-check (a listing
counts as verified only when the stored deal was verified), dedupe, push.

`run(playbook, since, until, store)` replays every listing seen in the window (the snapshot corpus: stored listings,
their cached extractions, comps and FX rows) and returns `ReplayStats`. Replay can score a JUDGMENT change on old
search results; it cannot score a query nobody ran (Li et al. 2011), so query and cadence deltas are never judged here.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from .. import config as dhconfig
from .. import extract as extractmod
from .. import scheduler
from ..match import rules_extract
from ..models import Extraction, Listing
from ..money import FxError
from ..valuation import cached_rate
from ..verify import load_listing
from .playbook import Playbook, eval_root

ROOT = Path(__file__).resolve().parents[2]
EXCLUDED_REASONS = ("bait", "no_key", "price_range", "hard_exclude")
DEFAULT_TOKENS_PER_LLM_ITEM = 900  # used until rounds have measured it
REPLAY_CAP = 3000  # most recent listings replayed per run


@dataclass
class Outcome:
    """What the pipeline would do with one listing under one playbook."""
    key: str | None
    excluded: bool  # a categorical "never a deal": bait, no key, out of the key's price range, hard risk exclude
    reason: str  # bait | no_key | price_range | hard_exclude | no_valuation | not_priced | below_floor | deal
    tier: int | None = None
    star: bool = False
    cons_usd: Decimal | None = None
    exp_usd: Decimal | None = None
    p_scam: float | None = None
    confidence: str | None = None
    flags: list[str] = field(default_factory=list)
    method: str = "rules"
    needs_llm: bool = False  # the rules were not certain and no cached LLM result existed
    notes: list[str] = field(default_factory=list)

    @property
    def deal(self) -> bool:
        return self.tier in (1, 2, 3)

    def signature(self) -> tuple:
        return (self.key, self.excluded, self.tier)

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        for k in ("cons_usd", "exp_usd"):
            d[k] = None if d[k] is None else str(d[k])
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Outcome":
        d = dict(d)
        for k in ("cons_usd", "exp_usd"):
            d[k] = None if d.get(k) is None else Decimal(d[k])
        return cls(**{k: v for k, v in d.items() if k in {f.name for f in dataclasses.fields(cls)}})


# ------------------------------------------------------------------------------------------------ FX without network


class TableFx:
    """FX from a fixed table (PLN per unit) — the frozen eval sets use one so their outcomes never drift."""

    def __init__(self, rates: dict[str, Any]):
        self.rates = {k.upper(): Decimal(str(v)) for k, v in rates.items()}
        self.rates["PLN"] = Decimal(1)

    async def rate_with_date(self, ccy: str, day: date) -> tuple[Decimal, date]:
        c = (ccy or "").upper()
        if c not in self.rates:
            raise FxError(f"no eval rate for {c}")
        return self.rates[c], day


class StoreFx:
    """FX from the store's `fx` table only (NBP rows the live rounds pinned); never calls NBP."""

    def __init__(self, store, max_back_days: int = 7):
        self.store, self.back = store, max_back_days

    async def rate_with_date(self, ccy: str, day: date) -> tuple[Decimal, date]:
        c = (ccy or "").upper()
        if c == "PLN":
            return Decimal(1), day
        hit = cached_rate(self.store, c, day, self.back)
        if hit is None:
            raise FxError(f"no stored rate for {c} near {day}")
        return hit


# ------------------------------------------------------------------------------------------------ judge


class _JudgeRound(scheduler._Round):
    """The scheduler's round object, used only for its appraisal path. `estimate_fn(key, condition, channel)`
    replaces the store valuation (the eval sets carry their own market); None keeps the store valuation."""

    def __init__(self, ctx, estimate_fn: Callable[[str, str | None, str], Any] | None = None):
        super().__init__(ctx)
        self._estimate_fn = estimate_fn

    def _estimate(self, key, cond, ch, lid):
        if self._estimate_fn is not None:
            return self._estimate_fn(key, cond, ch)
        return super()._estimate(key, cond, ch, lid)


class Judge:
    """Offline judgment of listings under one playbook (see module doc)."""

    def __init__(self, playbook: Playbook, store, *, cfg=None, fx=None, now: datetime | None = None,
                 estimate_fn: Callable | None = None, customs=None):
        self.playbook = playbook
        self.root = eval_root(playbook)
        self.cfg = cfg if cfg is not None else dhconfig.load(ROOT)
        self.store = store
        now = now or datetime.now(timezone.utc)
        ctx = scheduler.RoundContext(
            cfg=self.cfg, root=self.root, store=store, fetcher=None, adapters={}, llm=None,
            fx=fx if fx is not None else StoreFx(store), customs=customs, bandit=None, rng=random.Random(0),
            cards_root=self.root / "cards", shots_root=self.root / "shots", dry=True, now=lambda: now,
            items=playbook.as_dicts())
        self._round = _JudgeRound(ctx, estimate_fn)

    async def judge(self, listing: Listing, *, lid: int = 0, cached: Extraction | None = None,
                    verified_age_s: float | None = None) -> Outcome:
        r = self._round
        lang = (listing.country or "pl").lower()
        rules = rules_extract(listing.title, listing.description, lang, root=self.root)
        notes = [n for n in rules.notes if n.startswith("alias:")]
        if rules.wanted_ad or rules.box_only or rules.for_parts:  # as the scheduler: the rules' key is kept
            return Outcome(rules.key, True, "bait", notes=notes)
        title = listing.title or ""
        if rules.key is None and (not scheduler._HW.search(title) or scheduler._SYSTEM_WORDS.search(title)):
            return Outcome(None, True, "no_key", notes=notes)
        price_pln = await r._price_pln(listing)
        if rules.key and rules.key != scheduler.PC_KEY and not r._in_range(rules.key, price_pln):
            return Outcome(rules.key, True, "price_range", notes=notes)
        ex, method, needs_llm = rules, "rules", False
        if rules.key != scheduler.PC_KEY and rules.certainty < extractmod.RULES_CERTAIN:
            if cached is not None and cached.method == "llm":
                ex, method = cached, "cached_llm"
            else:
                needs_llm = True
        if not ex.key or ex.box_only or ex.wanted_ad or ex.for_parts:
            return Outcome(None, True, "no_key", method=method, needs_llm=needs_llm, notes=notes)
        if ex.key != rules.key and ex.key != scheduler.PC_KEY and not r._in_range(ex.key, price_pln):
            return Outcome(ex.key, True, "price_range", method=method, needs_llm=needs_llm, notes=notes)
        before = dict(r.stats.dropped)
        a = await r._appraise(listing, lid, ex, price_pln, verified_age_s=verified_age_s)
        if a is None:
            new = [k for k, v in r.stats.dropped.items() if v > before.get(k, 0)]
            why = new[0] if new else "no_valuation"
            if why == "hard_exclude":
                return Outcome(ex.key, True, "hard_exclude", method=method, needs_llm=needs_llm, notes=notes)
            return Outcome(ex.key, False, why, method=method, needs_llm=needs_llm, notes=notes)
        t = a.tier
        return Outcome(ex.key, False, "deal" if t.tier is not None else "below_floor", tier=t.tier, star=bool(t.star),
                       cons_usd=a.cons_usd, exp_usd=a.exp_usd, p_scam=round(float(a.risk.p_scam), 4),
                       confidence=a.confidence, flags=list(a.risk.flags), method=method, needs_llm=needs_llm,
                       notes=notes)


# ------------------------------------------------------------------------------------------------ replay archive


@dataclass
class ReplayStats:
    deals_by_tier: dict[int, int]
    false_pos_known: int  # tier 1-3 on listings the owner marked as a wrong deal
    llm_tokens: int  # LLM tokens the playbook would need for these listings (items without a certain rules match)
    fetches: int  # detail + live re-check fetches it would trigger (tier <= 3: detail, tier <= 2: re-check)
    exp_net_t13_usd: Decimal = Decimal(0)
    true_pos_known: int = 0  # tier 1-3 on listings the owner bought or sold
    n_listings: int = 0
    outcomes: dict[int, Outcome] = field(default_factory=dict)

    def summary(self) -> dict:
        return {"deals_by_tier": {str(k): v for k, v in sorted(self.deals_by_tier.items())},
                "exp_net_t13_usd": str(self.exp_net_t13_usd), "false_pos_known": self.false_pos_known,
                "true_pos_known": self.true_pos_known, "llm_tokens": self.llm_tokens, "fetches": self.fetches,
                "n_listings": self.n_listings}


def _iso(t: datetime) -> str:
    if t.tzinfo is None:
        raise ValueError("naive datetime")
    return t.astimezone(timezone.utc).isoformat()


def title_hash(title: str | None) -> str:
    """The scheduler's evaluations.title_hash (dh.scheduler._Round._title_hash)."""
    return hashlib.sha1((title or "").encode()).hexdigest()[:16]


def cached_extraction(store, lid: int, title: str | None = None, prompt_sha: str | None = None) -> Extraction | None:
    """The stored LLM extraction of a listing, or None when there is none, when the listing's title changed since
    (evaluations.title_hash, the scheduler's own staleness rule), or, with `prompt_sha`, when it was produced
    under another extraction prompt (the sentinel's pinned prompt must never see a newer prompt's output)."""
    r = store.conn.execute("select * from extractions where listing_id=? and method='llm'", (lid,)).fetchone()
    if r is None:
        return None
    if title is not None or prompt_sha is not None:
        try:
            ev = store.conn.execute("select title_hash, at from evaluations where listing_id=?", (lid,)).fetchone()
        except Exception:  # noqa: BLE001 - no evaluations table: nothing proves the cache is current
            return None
        if ev is None:
            return None
        if title is not None and ev["title_hash"] != title_hash(title):
            return None
        if prompt_sha is not None:
            made_by = r["prompt_sha"] if "prompt_sha" in r.keys() and r["prompt_sha"] else prompt_sha_at(store, ev["at"])
            if made_by != prompt_sha:  # the sha stamped on the row; old rows: the epoch at their evaluation time
                return None
    return Extraction(key=r["key"], category=r["category"], certainty=float(r["certainty"] or 0),
                      condition=r["condition"] or "unknown", defects=json.loads(r["defects"] or "[]"),
                      box_only=bool(r["box_only"]), for_parts=bool(r["for_parts"]), wanted_ad=bool(r["wanted_ad"]),
                      bundle_items=json.loads(r["bundle_items"] or "[]"), method="llm")


EPOCHS_SQL = """create table if not exists improve_prompt_epochs (
  id integer primary key, sha text not null, started_at text not null)"""


def record_prompt_epoch(store, sha: str | None, at: datetime) -> None:
    """Remember from when on an extraction prompt was active (one row per change)."""
    if sha is None:
        return
    store.ensure_table(EPOCHS_SQL)
    last = store.conn.execute("select sha from improve_prompt_epochs order by id desc limit 1").fetchone()
    if last is None or last["sha"] != sha:
        store.conn.execute("insert into improve_prompt_epochs (sha, started_at) values (?,?)", (sha, _iso(at)))
        store.conn.commit()


def prompt_sha_at(store, at: str | None) -> str | None:
    """The extraction prompt active at `at` (ISO); before the first recorded epoch: the first epoch's prompt."""
    store.ensure_table(EPOCHS_SQL)
    if at is not None:
        r = store.conn.execute("select sha from improve_prompt_epochs where started_at <= ? order by id desc limit 1",
                               (at,)).fetchone()
        if r is not None:
            return r["sha"]
    r = store.conn.execute("select sha from improve_prompt_epochs order by id limit 1").fetchone()
    return None if r is None else r["sha"]


def verified_listings(store) -> set[int]:
    return {r["listing_ref"] for r in store.conn.execute(
        "select distinct listing_ref from deals where verified_at is not null")}


def tokens_per_llm_item(store) -> float:
    """Measured mean LLM tokens per LLM item over recent rounds (round notes), else a default."""
    tok = items = 0
    for r in store.conn.execute("select notes from rounds order by id desc limit 200"):
        try:
            n = json.loads(r["notes"] or "{}")
        except ValueError:
            continue
        tok += int(n.get("llm_tokens") or 0)
        items += int(n.get("llm_items") or 0)
    return tok / items if items else float(DEFAULT_TOKENS_PER_LLM_ITEM)


def window_listings(store, since: datetime, until: datetime, cap: int = REPLAY_CAP) -> list[int]:
    """Listings seen inside [since, until] (an observation in the window), newest first."""
    return [r["listing_id"] for r in store.conn.execute(
        "select listing_id, max(seen_at) last from observations where seen_at >= ? and seen_at <= ? "
        "group by listing_id order by last desc limit ?", (_iso(since), _iso(until), int(cap)))]


async def judge_listings(playbook: Playbook, store, lids: list[int], *, now: datetime, cfg=None,
                         fx=None, prompt_sha: str | None = None) -> dict[int, Outcome]:
    """Offline outcomes for stored listings (cached LLM extractions that are still current, stored comps and FX,
    no network). `prompt_sha`: use only cached extractions made under that extraction prompt."""
    j = Judge(playbook, store, cfg=cfg, fx=fx, now=now)
    verified = verified_listings(store)
    out: dict[int, Outcome] = {}
    for lid in lids:
        l = load_listing(store, lid)
        if l is None:
            continue
        out[lid] = await j.judge(l, lid=lid, cached=cached_extraction(store, lid, l.title, prompt_sha),
                                 verified_age_s=0.0 if lid in verified else None)
    return out


async def run(playbook: Playbook, since: datetime, until: datetime, store, *, cfg=None, fx=None,
              known: tuple[set[int], set[int]] | None = None, lids: list[int] | None = None) -> ReplayStats:
    """Replay the window with `playbook`. `known` = (false-positive listing ids, true-positive listing ids) from
    verified owner outcomes (default: evals.labels.known_outcomes)."""
    if known is None:
        from evals.labels import known_outcomes
        known = known_outcomes(store)
    fp, tp = known
    lids = window_listings(store, since, until) if lids is None else lids
    outs = await judge_listings(playbook, store, lids, now=until, cfg=cfg, fx=fx)
    tiers: dict[int, int] = {}
    net = Decimal(0)
    for o in outs.values():
        if o.tier is not None:
            tiers[o.tier] = tiers.get(o.tier, 0) + 1
        if o.deal and o.exp_usd is not None:
            net += o.exp_usd
    deals = [lid for lid, o in outs.items() if o.deal]
    per_item = tokens_per_llm_item(store)
    return ReplayStats(
        deals_by_tier=tiers, false_pos_known=sum(1 for lid in deals if lid in fp),
        llm_tokens=int(round(per_item * sum(1 for o in outs.values() if o.needs_llm))),
        fetches=sum(1 for o in outs.values() if o.deal) + sum(1 for o in outs.values() if o.tier in (1, 2)),
        exp_net_t13_usd=net, true_pos_known=sum(1 for lid in deals if lid in tp), n_listings=len(outs),
        outcomes=outs)


def default_window(now: datetime, days: int) -> tuple[datetime, datetime]:
    return now - timedelta(days=days), now
