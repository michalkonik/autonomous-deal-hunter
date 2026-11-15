"""Effort class, confidence and tiering (spec §5). FROZEN: the agent never edits this file.

Every threshold comes from config/rules.yaml (`rules` below is the Config `rules` section or a plain dict
of the same shape). Money in USD, as Decimal.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

from .economics import EU, load_fees, norm_country

Effort = Literal["easy", "moderate", "heavy"]
PL_SOURCES = frozenset({"olx", "allegro", "allegro_lokalnie", "sprzedajemy"})  # a missing country means PL here


@dataclass
class TierResult:
    tier: int | None
    star: bool
    floor_usd: Decimal
    reasons: list[str] = field(default_factory=list)


def _rules(rules: Any):
    if isinstance(rules, dict):
        from .config import Config
        return Config(rules)
    return rules


def _d(x: Any) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


def _fmt(x: Decimal) -> str:
    return format(x.normalize(), "f")


# ------------------------------------------------------------------------------------------ effort

def effort_class(listing, exit_channel: str, flags: list[str], estimate, *, extraction=None) -> Effort:
    """heavy: origin outside the EU, `needs_vat_ue`, `bdo_check` with a non-EU origin, or a bundle of >= 3
    items; moderate: any non-PL leg (origin, or the exit channel's buyer country) or days_to_sell > 14;
    else easy. An unknown listing country is moderate unless the source is a Polish marketplace (PL_SOURCES).
    `extraction` supplies bundle_items (kept keyword-only: the brief's signature has no extraction)."""
    country = getattr(listing, "country", None)
    unknown_origin = not country and getattr(listing, "source", None) not in PL_SOURCES
    origin = norm_country(country or "PL")
    non_eu = origin not in EU
    bundle = len(getattr(extraction, "bundle_items", None) or [])
    if non_eu or "needs_vat_ue" in flags or ("bdo_check" in flags and non_eu) or bundle >= 3:
        return "heavy"
    dest = norm_country(str(load_fees().get(exit_channel, {}).get("dest", "PL")))
    dts = getattr(estimate, "days_to_sell", None)
    if unknown_origin or origin != "PL" or dest != "PL" or (dts is not None and dts > 14):
        return "moderate"
    return "easy"


# ------------------------------------------------------------------------------------------ confidence

def min_comps(capital_usd: Any, rules: Any) -> int:
    c = _rules(rules).confidence
    th = c.capital_thresholds_usd
    cap = _d(capital_usd)
    if cap > _d(th.high):
        return int(c.min_comps.capital_over_15000)
    if cap > _d(th.mid):
        return int(c.min_comps.capital_over_5000)
    return int(c.min_comps.base)


def confidence(extraction, estimate, risk, verified_age_s, capital_usd, rules) -> tuple[str, list[str]]:
    """('high'|'medium'|'low'|'excluded', reasons). The reasons list every unmet condition for high, so a card can say
    why a deal is not tier 1.

    high: match certainty >= 0.9, >= min_comps comparables <= 30 days old (estimate.n_sold_effective: the summed
    weight of owner sales 1.0, eBay sold 1.0, probably-sold 0.5, each x its fallback multiplier; asks do not
    count), p_scam < 0.05, verified live at the same price <= 10 min ago
    (`verified_age_s` is None when never verified or when the price changed), no defect flags, condition known, no hard exclude.
    medium: certainty >= 0.75, p_scam < 0.15, known condition, comparables >= half of min_comps, no hard exclude.
    Otherwise low. A hard-excluded listing is 'excluded' and assign() never tiers it."""
    c = _rules(rules).confidence
    need = min_comps(capital_usd, rules)
    n_sold = float(getattr(estimate, "n_sold_effective", 0.0) or 0.0) if estimate is not None else 0.0
    cert = extraction.certainty if extraction.key else 0.0
    if risk.hard_exclude:
        flags = ", ".join(getattr(risk, "flags", []) or [])
        return "excluded", ["hard exclude" + (f": {flags}" if flags else "")]
    why: list[str] = []
    if cert < float(c.match_certainty_min):
        why.append(f"match certainty {cert:.2f} < {c.match_certainty_min}")
    if n_sold < need:
        why.append(f"{n_sold:g} effective sold comparables < {need} needed")
    if risk.p_scam >= float(c.p_scam_max):
        why.append(f"p_scam {risk.p_scam:.3f} >= {c.p_scam_max}")
    if verified_age_s is None or verified_age_s > float(c.verify_max_age_min) * 60:
        why.append("not verified live at the same price in the last %s min" % c.verify_max_age_min)
    if extraction.defects:
        why.append("unresolved defects: " + ", ".join(extraction.defects))
    if extraction.condition == "unknown":
        why.append("condition unknown")
    if not why:
        return "high", []
    m = c.medium
    hard: list[str] = []
    if cert < float(m.match_certainty_min):
        hard.append("weak match")
    if risk.p_scam >= float(m.p_scam_max):
        hard.append("scam score too high")
    if extraction.condition == "unknown":
        hard.append("condition unknown")
    if n_sold < math.ceil(need * float(m.min_comps_frac)):
        hard.append("thin price data")
    if hard:
        return "low", why + [f"low: {h}" for h in hard]
    return "medium", why


# ------------------------------------------------------------------------------------------ tiering

def assign(cons_usd: Any, exp_usd: Any, effort: str, conf: str, capital_usd: Any, rules: Any,
           ship_pl: bool = False) -> TierResult:
    """`conf` 'excluded' -> tier None. Tier 1: cons >= t1_min (300) and high confidence. Tier 2: clears the effort floor, confidence >= medium.
    Tier 3: clears the floor, low confidence. Tier 4: floor*(1-0.2) <= cons < floor. Else None (not logged).
    Capital above the cap: None. star = exp >= star_usd (only for logged deals). The trend-signal tier 4 is
    produced elsewhere. `ship_pl`: the item ships directly to Poland (dh/shipping.py) — the owner can order it
    himself, so the effort floor is discounted 5% (additional points, owner ruling 2026-11-07)."""
    r = _rules(rules)
    if conf == "excluded":  # a hard-excluded listing is never tiered, at any net
        return TierResult(None, False, _d(r.floors[effort]), ["hard exclude: not logged"])
    cons, exp, cap = _d(cons_usd), _d(exp_usd), _d(capital_usd)
    floor = _d(r.floors[effort])
    if ship_pl:
        floor = floor * Decimal("0.95")
    if cap > _d(r.capital_cap_usd):
        return TierResult(None, False, floor, [f"over capital cap ({_fmt(cap)} > {_fmt(_d(r.capital_cap_usd))} USD)"])
    reasons: list[str] = []
    t1 = _d(r.tiers.t1_min_cons_net_usd)
    near = floor * (1 - _d(r.tiers.t4_floor_margin))
    if cons >= floor:
        reasons.append(f"conservative net {_fmt(cons)} clears the {effort} floor {_fmt(floor)}")
        if cons >= t1 and conf == "high":
            tier = 1
            reasons.append(f"net >= {_fmt(t1)} with high confidence")
        elif conf in ("high", "medium"):
            tier = 2
            reasons.append(f"confidence {conf}" + (f" (tier 1 needs net >= {_fmt(t1)})" if conf == "high" else ""))
        else:
            tier = 3
            reasons.append("low confidence")
    elif cons >= near:
        tier = 4
        reasons.append(f"conservative net {_fmt(cons)} is within {_fmt(_d(r.tiers.t4_floor_margin) * 100)}% under the {effort} floor {_fmt(floor)}")
    else:
        return TierResult(None, False, floor,
                          [f"conservative net {_fmt(cons)} below the {effort} floor {_fmt(floor)} (and not within 20%)"])
    star = exp >= _d(r.star_usd)
    if star:
        reasons.append(f"expected net {_fmt(exp)} >= {_fmt(_d(r.star_usd))}")
    return TierResult(tier, star, floor, reasons)
