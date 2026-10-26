"""Offline gates and every decision threshold of the improvement loop (spec §9.3, §9.4; research §3.4-§3.9, §4.5).

FROZEN (dh.config.FROZEN_PATHS): a human changes this file through git; the improvement loop only reads it. The
rounds refuse to run if it changes while the agent runs. Everything that DECIDES (gate order, thresholds, the exact
tests, the stop-rule constants) lives here so the loop cannot move its own goalposts (research §1 rule 4).

Gate cascade, cheapest first; the first failing stage rejects:
1. schema   - the challenger parses, respects the caps, differs from the champion in exactly ONE item, touches no
              sentinel item, edits only an editable prompt, and never removes an arm (arms retire in the bandit).
2. must_pass - 100% of evals/must_pass.jsonl (known scams, box-only, wanted ads, true deals).
3. golden   - paired on evals/golden.jsonl: b = champion right & challenger wrong (regressions), c = the reverse
              (fixes). Reject if b >= c ("fix more than you break"); the exact McNemar p is reported.
4. replay   - the last REPLAY_DAYS of stored listings: reject if the expected net of tier 1-3 falls more than 5%,
              if known false positives rise, or if a known true deal is lost.
Doors (research §4.2): alias / scam_kw = judgment (all four stages, then shadow); prompt = llm (update only; the
offline stages use cached LLM output and cannot see a prompt change, so the loop never promotes one: it stops at
"owner_review" and the owner applies it after a LIVE must-pass run); query / cadence = arm (offline replay cannot
score a query nobody ran: probation with the CUSUM decides).

Offline evaluation is deterministic (rules + cached LLM output + fixed FX), so pass^k is pass^1 here; the live-LLM
variance is handled in shadow, where only labelled disagreements count.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

from .. import config as dhconfig
from ..models import CompRef, Listing
from ..store import Store
from ..valuation import ExitEstimate
from . import replay
from .playbook import EDITABLE_PROMPT_TARGETS, Playbook, door_of, eval_root

ROOT = Path(__file__).resolve().parents[2]
EVALS = ROOT / "evals"
MUST_PASS = EVALS / "must_pass.jsonl"
GOLDEN = EVALS / "golden.jsonl"

# ------------------------------------------------------------------ evaluation constants
EVAL_DAY = date(2026, 10, 15)  # fixed: FX, duty dates and comps ages never drift
EVAL_NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
EVAL_FX = {"EUR": "4.25", "USD": "3.90", "GBP": "5.00", "CZK": "0.170", "HUF": "0.0105"}  # PLN per unit
DEFAULT_CHANNELS = ("allegro", "olx")

REPLAY_DAYS = 21  # spec: archived snapshots of the last 2-4 weeks
REPLAY_MAX_NET_DROP = Decimal("0.05")

# ------------------------------------------------------------------ shadow (research §4.2, plan Task 29)
SHADOW_MIN_ROUNDS = 40
SHADOW_MIN_LABELLED = 10  # research: >= 10 adjudicated disagreements
SHADOW_ALPHA = 0.10  # one-sided exact binomial; evaluated ONCE (single look) so peeking cannot inflate it
SHADOW_MAX_WATCHED_HOURS = 336.0  # 14 watched days; inconclusive at the cap -> keep the champion
SHADOW_TWYMAN_RATE = 0.25  # a one-item delta that changes > 25% of judgments is flagged for audit

# ------------------------------------------------------------------ probation, CUSUM, stop rules (spec §9.4)
PROBATION_WATCHED_HOURS = 48.0
COOLDOWN_WATCHED_HOURS = (8.0, 16.0, 32.0, 64.0)  # after the 1st, 2nd, 3rd, 4th+ consecutive rejection
MAINTENANCE_AFTER_REJECTIONS = 3
WEEKLY_REVIEW_WATCHED_HOURS = 168.0
SAME_CAUSE_TRIGGER = 3  # >= 3 failures with the same cause open a proposal
HEALTH_ALARM_EVENTS = 3  # >= 3 health events of one (source, kind) since the last check
CUSUM_DROP_RATIO = 0.5  # watch for the rate halving (lambda1 = lambda0 / 2)
CUSUM_H = 4.0  # research §3.8: ARL0 ~1300 rounds at lambda0 0.3
CUSUM_MIN_BASELINE_ROUNDS = 30
CUSUM_BASELINE_ROUNDS = 200
BREAKAGE_P = 0.001  # Fisher exact on (champion, sentinel) x (baseline, since promotion), looked at on every alarm
BREAKAGE_P_FINAL = 0.01  # the same test, one single look when probation ends
SENTINEL_DROP_P = 0.01  # the sentinel's count since promotion significantly below its own baseline -> market moved
SENTINEL_HOLD_ROUNDS = 30  # an alarm neither test explains: watch this long, then treat it as a false alarm
MUST_PASS_RECHECK_WATCHED_HOURS = 24.0
MDE_WINDOW_WATCHED_HOURS = 336.0  # "2 weeks of watched data"
MDE_ALPHA, MDE_POWER = 0.05, 0.80
PLAUSIBLE_GAIN = 1.5  # largest plausible deal-rate ratio of one playbook item
REJECTED_IDEA_DAYS = 30


@dataclass
class GateResult:
    passed: bool
    stage: str  # the failing stage, or "offline" when every offline stage passed
    detail: dict = field(default_factory=dict)


# ------------------------------------------------------------------ exact statistics (no scipy)


def _binom_logpmf(i: int, n: int, p: float) -> float:
    return (math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1)
            + i * math.log(p) + (n - i) * math.log1p(-p))


def _logsumexp_prob(terms: list[float]) -> float:
    if not terms:
        return 0.0
    m = max(terms)
    return min(1.0, math.exp(m) * sum(math.exp(t - m) for t in terms))


def binom_sf(k: int, n: int, p: float = 0.5) -> float:
    """P(X >= k) for X ~ Binomial(n, p), exact, in log space (no overflow for any n)."""
    if k <= 0:
        return 1.0
    if k > n:
        return 0.0
    if p <= 0:
        return 0.0
    if p >= 1:
        return 1.0
    return _logsumexp_prob([_binom_logpmf(i, n, p) for i in range(k, n + 1)])


def binom_cdf(k: int, n: int, p: float) -> float:
    """P(X <= k) for X ~ Binomial(n, p), exact, in log space."""
    if k < 0:
        return 0.0
    if k >= n:
        return 1.0
    if p <= 0:
        return 1.0
    if p >= 1:
        return 0.0
    return _logsumexp_prob([_binom_logpmf(i, n, p) for i in range(0, k + 1)])


def hypergeom_cdf(k: int, total: int, successes: int, draws: int) -> float:
    """P(X <= k) for X ~ Hypergeometric(total, successes, draws), exact (log-space terms)."""
    def lc(n: int, r: int) -> float:
        return math.lgamma(n + 1) - math.lgamma(r + 1) - math.lgamma(n - r + 1)
    lo, hi = max(0, draws - (total - successes)), min(draws, successes)
    if k < lo:
        return 0.0
    den = lc(total, draws)
    return _logsumexp_prob([lc(successes, x) + lc(total - successes, draws - x) - den
                            for x in range(lo, min(k, hi) + 1)])


def poisson_cdf(k: int, mu: float) -> float:
    """P(X <= k) for X ~ Poisson(mu), exact (log-space terms)."""
    if k < 0:
        return 0.0
    if mu <= 0:
        return 1.0
    return min(1.0, sum(math.exp(i * math.log(mu) - mu - math.lgamma(i + 1)) for i in range(int(k) + 1)))


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar p on the discordant pairs (binomial on b + c with p = 0.5)."""
    n = b + c
    if n == 0:
        return 1.0
    return min(1.0, 2 * binom_cdf(min(b, c), n, 0.5))


def shadow_verdict(wins: int, losses: int, rounds: int, watched_hours: float) -> tuple[str, dict]:
    """'promote' | 'reject' | 'wait' for a challenger in shadow, from LABELLED disagreements only.

    Single-look exact binomial: the test runs once, at the first moment both rounds >= SHADOW_MIN_ROUNDS and
    labelled >= SHADOW_MIN_LABELLED; promote iff P(X >= wins | n, 0.5) < SHADOW_ALPHA, otherwise reject. Before that
    moment the only exits are rejections, which can never raise the false-promotion rate: the champion already wins
    significantly (P(X >= losses) < SHADOW_ALPHA), or the cap (SHADOW_MAX_WATCHED_HOURS -> reject: an unproven change
    is a cost; research §3.4)."""
    n = wins + losses
    d = {"wins": wins, "losses": losses, "labelled": n, "rounds": rounds, "watched_hours": round(watched_hours, 2)}
    if rounds >= SHADOW_MIN_ROUNDS and n >= SHADOW_MIN_LABELLED:
        p = binom_sf(wins, n)
        d["p"] = round(p, 5)
        return ("promote" if p < SHADOW_ALPHA else "reject"), d
    if n and binom_sf(losses, n) < SHADOW_ALPHA:
        return "reject", d | {"why": "the champion wins the labelled disagreements",
                              "p_champion": round(binom_sf(losses, n), 5)}
    if watched_hours >= SHADOW_MAX_WATCHED_HOURS:
        return "reject", d | {"why": "inconclusive at the shadow cap"}
    return "wait", d


def _log_pmf(n: int, p: float) -> list[float]:
    lq, lp = math.log1p(-p), math.log(p)
    lc = math.lgamma(n + 1)
    return [lc - math.lgamma(k + 1) - math.lgamma(n - k + 1) + k * lp + (n - k) * lq for k in range(n + 1)]


def _power(n: int, p1: float, alpha: float) -> float:
    """Power of the one-sided exact binomial test of p0 = 0.5 at size alpha with n trials (O(n))."""
    f0 = [math.exp(x) for x in _log_pmf(n, 0.5)]
    f1 = [math.exp(x) for x in _log_pmf(n, p1)]
    tail0 = tail1 = 0.0
    k = n + 1
    while k > 0 and tail0 + f0[k - 1] <= alpha:  # smallest critical k with P0(X >= k) <= alpha
        k -= 1
        tail0 += f0[k]
        tail1 += f1[k]
    return tail1 if k <= n else 0.0


def required_deals(ratio: float, alpha: float = MDE_ALPHA, power: float = MDE_POWER, n_max: int = 20000) -> int:
    """Total deals the exact conditional binomial test (p0 = 0.5, p1 = R/(1+R), one-sided) needs for `power`
    (research §3.2; equal exposure). Power of a discrete test saw-tooths, so this is the smallest n found by a
    doubling + bisection search on the power curve (within a few deals of the exact minimum). n_max + 1 if unreachable."""
    p1 = ratio / (1 + ratio)
    if p1 <= 0.5:
        return n_max + 1
    hi = 8
    while _power(hi, p1, alpha) < power:
        hi *= 2
        if hi > n_max:
            return n_max + 1
    lo = hi // 2
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if _power(mid, p1, alpha) >= power:
            hi = mid
        else:
            lo = mid
    return hi


@lru_cache(maxsize=64)
def _req_cached(r100: int) -> int:
    return required_deals(r100 / 100)


def mde_ratio(total_deals: float) -> float:
    """Smallest deal-rate ratio (1.05 .. 5.0 grid) detectable with `total_deals`; inf when none is."""
    for r100 in [105, 110, 115, 120, 125, 130, 140, 150, 175, 200, 250, 300, 400, 500]:
        if _req_cached(r100) <= total_deals:
            return r100 / 100
    return math.inf


def cooldown_hours(consecutive_rejections: int) -> float:
    if consecutive_rejections <= 0:
        return 0.0
    return COOLDOWN_WATCHED_HOURS[min(consecutive_rejections, len(COOLDOWN_WATCHED_HOURS)) - 1]


# ------------------------------------------------------------------ eval cases


@dataclass
class CaseResult:
    id: str
    ok: bool
    why: str
    outcome: replay.Outcome


def load_cases(path: Path) -> list[dict]:
    p = Path(path)
    if not p.is_file():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def case_listing(case: dict) -> Listing:
    l = dict(case.get("listing") or {})
    photos = l.get("photos", 5)
    return Listing(
        source=l.get("source", "olx"), source_id=str(case["id"]), url=f"https://example.invalid/{case['id']}",
        title=l.get("title", ""), description=l.get("description", "") or "", price=Decimal(str(l.get("price", 0))),
        currency=l.get("currency", "PLN"), country=l.get("country", "PL"), city=l.get("city", "Krakow"),
        lat=l.get("lat"), lon=l.get("lon"), seller_hash="eval", seller_age_days=l.get("seller_age_days", 900),
        seller_ratings=l.get("seller_ratings", 40), seller_score=l.get("seller_score", 4.9),
        ships=l.get("ships", True), pickup_only=bool(l.get("pickup_only", False)), is_business=l.get("is_business", False),
        photos=[f"https://example.invalid/p{i}.jpg" for i in range(int(photos))] if isinstance(photos, int) else list(photos),
        posted_at=None, seen_at=EVAL_NOW, contact_in_text=bool(l.get("contact_in_text", False)))


def case_estimates(case: dict):
    """estimate_fn for the judge: the case's market for its key and channels; nothing for any other key."""
    m = case.get("market")
    if not m:
        return lambda key, cond, ch: None
    chans = tuple(m.get("channels") or DEFAULT_CHANNELS)

    n_sold = float(m.get("n_sold_effective", 0))
    n_eff = max(n_sold, float(m.get("n_effective", n_sold)))
    med = Decimal(str(m["median_pln"]))
    # the market's comparables as the card would list them (the estimate's numbers come from the case, not these)
    comps = [CompRef(kind="ebay_sold" if i < n_sold else "active_ask", channel="case", price_pln=med,
                     at=EVAL_NOW - timedelta(days=1 + i % 20), weight=1.0, ref=f"case:{case['id']}:{i}")
             for i in range(int(round(n_eff)))]

    def fn(key, cond, ch):
        if key != m["key"] or ch not in chans:
            return None
        return ExitEstimate(
            channel=ch, key=key, condition=cond, p25_pln=Decimal(str(m["p25_pln"])),
            median_pln=Decimal(str(m["median_pln"])), p75_pln=Decimal(str(m.get("p75_pln", m["median_pln"]))),
            n_effective=float(m.get("n_effective", m.get("n_sold_effective", 0))),
            n_sold=int(round(float(m.get("n_sold_effective", 0)))), newest_days=float(m.get("newest_days", 2)),
            days_to_sell=m.get("days_to_sell", 5.0), trend_30d=None, ceiling_pln=None, comps=list(comps),
            n_sold_effective=float(m.get("n_sold_effective", 0)))
    return fn


def expect_ok(o: replay.Outcome, expect: dict) -> tuple[bool, str]:
    if "key" in expect and o.key != expect["key"]:
        return False, f"key {o.key} != {expect['key']}"
    if "excluded" in expect and o.excluded != bool(expect["excluded"]):
        return False, f"excluded {o.excluded} != {expect['excluded']} ({o.reason})"
    if "max_tier" in expect and (o.tier is None or o.tier > int(expect["max_tier"])):
        return False, f"tier {o.tier} worse than {expect['max_tier']} ({o.reason})"
    if "min_tier" in expect and o.tier is not None and o.tier < int(expect["min_tier"]):
        return False, f"tier {o.tier} better than allowed {expect['min_tier']}"
    return True, ""


async def score_cases(pb: Playbook, cases: list[dict], *, cfg=None, llm=None) -> list[CaseResult]:
    """Judge every case with `pb` (fresh in-memory store, fixed FX and day, the case's own market). With `llm`
    (owner review of a prompt change) every case whose rules are not certain is extracted LIVE with pb's prompts;
    without it the case's cached LLM output is used."""
    current: dict[str, Any] = {"fn": None}
    store = Store(":memory:")
    try:
        j = replay.Judge(pb, store, cfg=cfg, fx=replay.TableFx(EVAL_FX), now=EVAL_NOW,
                         estimate_fn=lambda k, c, ch: current["fn"](k, c, ch))
        out = []
        for case in cases:
            current["fn"] = case_estimates(case)
            listing = case_listing(case)
            cached = _case_llm(case)
            if llm is not None:
                cached = await _live_extraction(pb, listing, llm) or cached
            o = await j.judge(listing, lid=0, cached=cached, verified_age_s=0.0 if case.get("verified") else None)
            ok, why = expect_ok(o, case.get("expect") or {})
            out.append(CaseResult(str(case["id"]), ok, why, o))
        return out
    finally:
        store.close()


async def _live_extraction(pb: Playbook, listing: Listing, llm):
    from .. import extract as extractmod
    from ..match import rules_extract
    root = eval_root(pb)
    rules = rules_extract(listing.title, listing.description, (listing.country or "pl").lower(), root=root)
    if rules.certainty >= extractmod.RULES_CERTAIN or rules.box_only or rules.wanted_ad or rules.for_parts:
        return None
    ex = await extractmod.extract(listing, rules, llm, root)
    return ex if ex.method == "llm" else None


def _case_llm(case: dict):
    from ..models import Extraction
    e = case.get("llm")
    if not e:
        return None
    return Extraction(key=e.get("key"), category=e.get("category"), certainty=float(e.get("certainty", 0)),
                      condition=e.get("condition", "unknown"), defects=list(e.get("defects", [])),
                      box_only=bool(e.get("box_only")), for_parts=bool(e.get("for_parts")),
                      wanted_ad=bool(e.get("wanted_ad")), bundle_items=list(e.get("bundle_items", [])), method="llm")


# ------------------------------------------------------------------ the cascade


def changed_items(champion: Playbook, challenger: Playbook) -> list[tuple[str, str]]:
    """[(item id, 'add'|'update'|'remove')] between two playbooks (content only: counters ignored)."""
    a = {i.id: i for i in champion.items}
    b = {i.id: i for i in challenger.items}
    out = [(k, "add") for k in b if k not in a] + [(k, "remove") for k in a if k not in b]
    out += [(k, "update") for k in a if k in b and a[k].content() != b[k].content()]
    return sorted(out)


def _schema(champion: Playbook, challenger: Playbook) -> tuple[list[str], dict]:
    errs = list(challenger.errors())
    changed = changed_items(champion, challenger)
    info: dict = {"changed": changed}
    if len(changed) != 1:
        errs.append(f"a challenger must change exactly one item, it changes {len(changed)}")
        return errs, info
    iid, op = changed[0]
    old, new = champion.get(iid), challenger.get(iid)
    it = new or old
    info["door"] = door_of(it.kind)
    if (old is not None and old.sentinel) or (new is not None and new.sentinel):
        errs.append(f"{iid}: sentinel items are immutable")
    if it.kind == "prompt" and it.target not in EDITABLE_PROMPT_TARGETS:
        errs.append(f"{iid}: prompt {it.target} is not editable by the loop")
    if it.kind == "prompt" and op != "update":
        errs.append(f"{iid}: prompts are only updated (owner review), never added or removed")
    if it.kind in ("query", "cadence") and op == "remove":
        errs.append(f"{iid}: arms are retired by the bandit's cold pool, not removed by a delta")
    if new is not None and old is not None and new.kind != old.kind:
        errs.append(f"{iid}: kind changed")
    return errs, info


async def evaluate(champion: Playbook, challenger: Playbook, store, *, cfg=None, now: datetime | None = None,
                   must_pass: list[dict] | None = None, golden: list[dict] | None = None,
                   replay_days: int = REPLAY_DAYS) -> GateResult:
    """Run the cascade (module doc). `detail` keeps what each stage measured; the controller decides what the
    reflector may see (stage and counts only: never case labels, research R2)."""
    cfg = cfg if cfg is not None else dhconfig.load(ROOT)
    errs, info = _schema(champion, challenger)
    detail: dict = {"changed": info["changed"], "door": info.get("door")}
    if errs:
        return GateResult(False, "schema", detail | {"errors": errs})

    mp = load_cases(MUST_PASS) if must_pass is None else must_pass
    res = await score_cases(challenger, mp, cfg=cfg)
    fails = [r for r in res if not r.ok]
    detail["must_pass"] = {"cases": len(res), "failed": [r.id for r in fails],
                           "why": {r.id: r.why for r in fails}}
    if fails or not res:
        return GateResult(False, "must_pass", detail)

    if detail["door"] == "llm":  # ruling: gates cannot judge a prompt without live calls -> the owner decides
        detail["golden"] = detail["replay"] = "not applicable offline (llm door)"
        return GateResult(True, "owner_review", detail)
    if detail["door"] != "judgment":
        detail["golden"] = detail["replay"] = f"not applicable offline ({detail['door']} door)"
        return GateResult(True, "offline", detail)

    gold = load_cases(GOLDEN) if golden is None else golden
    a = {r.id: r.ok for r in await score_cases(champion, gold, cfg=cfg)}
    b_res = await score_cases(challenger, gold, cfg=cfg)
    regressions = [r.id for r in b_res if a.get(r.id) and not r.ok]
    fixes = [r.id for r in b_res if not a.get(r.id) and r.ok]
    detail["golden"] = {"cases": len(b_res), "regressions": regressions, "fixes": fixes,
                        "mcnemar_p": round(mcnemar_exact(len(regressions), len(fixes)), 5),
                        "champion_correct": sum(a.values()), "challenger_correct": sum(r.ok for r in b_res)}
    if len(regressions) >= len(fixes):
        return GateResult(False, "golden", detail)

    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=replay_days)
    lids = replay.window_listings(store, since, now)
    if not lids:
        detail["replay"] = {"listings": 0, "note": "no archived listings in the window"}
        return GateResult(True, "offline", detail)
    rc = await replay.run(champion, since, now, store, cfg=cfg, lids=lids)
    rh = await replay.run(challenger, since, now, store, cfg=cfg, lids=lids)
    detail["replay"] = {"days": replay_days, "champion": rc.summary(), "challenger": rh.summary()}
    why = []
    if rc.exp_net_t13_usd > 0 and rh.exp_net_t13_usd < rc.exp_net_t13_usd * (1 - REPLAY_MAX_NET_DROP):
        why.append("expected net of tier 1-3 fell more than 5%")
    if rh.false_pos_known > rc.false_pos_known:
        why.append("known false positives rose")
    if rh.true_pos_known < rc.true_pos_known:
        why.append("a known true deal was lost")
    if why:
        detail["replay"]["why"] = why
        return GateResult(False, "replay", detail)
    return GateResult(True, "offline", detail)


async def must_pass_ok(pb: Playbook, *, cfg=None, llm=None) -> tuple[bool, list[str]]:
    """Re-run of the must-pass set (promotion check, daily probation re-check; with `llm`: live, for owner review)."""
    res = await score_cases(pb, load_cases(MUST_PASS), cfg=cfg, llm=llm)
    bad = [r.id for r in res if not r.ok]
    return (not bad and bool(res)), bad
