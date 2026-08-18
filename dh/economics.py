"""Frozen economics engine (spec §7): net profit of a resale deal for a Polish VAT-registered JDG.

FROZEN: the agent never edits this file. The formula, every simplification and 10 hand-computed
examples are in docs/ECONOMICS.md; tests/test_economics.py checks them to the grosz.

All money is PLN Decimal. Each line is rounded to 0.01 PLN (ROUND_HALF_UP) on its own, then lines are
added, so a human can re-add a deal card. Cost lines are negative; the lines of a scenario sum to its net.
Conservative and expected results differ only by the exit price (P25 vs median).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

import yaml

from .customs import DEFAULT_CN, import_fees_eur
from .money import q2

DATA = Path(__file__).resolve().parent.parent / "data"
ZERO = Decimal("0")

# The 27 EU member states (ISO 3166-1 alpha-2; Greece is GR, not the VAT prefix EL).
EU = frozenset({"AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE",
                "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE"})
# Non-ISO codes seen in listings and VAT numbers.
COUNTRY_ALIASES = {"EL": "GR", "UK": "GB"}

# VAT regimes (see docs/ECONOMICS.md "VAT"):
MARGIN = "margin"              # private seller in the EU: VAT inside max(0, sale - purchase)
INVOICE = "invoice"            # PL seller's VAT invoice: output - invoice VAT
IMPORT = "import"              # non-EU origin: output - import VAT
INTRA_EU_B2B = "intra_eu_b2b"  # EU (not PL) business seller: output, nothing deductible in PL
NO_INPUT = "no_input"          # PL business without a VAT invoice: output, nothing deductible


class DutyLookup(Protocol):
    async def duty_rate(self, cn_code: str, origin: str, day: date) -> Decimal: ...


@dataclass(frozen=True)
class NetInputs:
    """Inputs for one deal and one exit channel.

    Convention: every PLN amount is GROSS, as actually paid or charged (VAT included where the supplier
    charges Polish VAT). The engine itself deducts the input VAT on Polish-invoiced service costs
    (channel fees, domestic shipping, clearance) according to `vat_deductible` in data/fees.yaml and
    data/shipping.yaml, and shows it as the line "input VAT on services".
    """
    buy_price: Decimal            # in buy_ccy, gross (what we pay the seller)
    buy_ccy: str
    origin_country: str           # ISO2 of where the item ships from (EL/UK accepted)
    seller_is_business: bool
    has_vat_invoice: bool
    exit_channel: str             # key in data/fees.yaml
    exit_price_cons_pln: Decimal  # P25 of comparables, gross, all items together
    exit_price_exp_pln: Decimal   # median of comparables, gross
    inbound_ship_pln: Decimal     # gross; seller/forwarder to us, incl. insurance and forwarder fees
    outbound_ship_pln: Decimal    # gross; us to the buyer, if seller-paid
    pickup_km: float | None       # one-way km from Krakow for a personal pickup
    items: int                    # units bought; each is resold as a separate sale
    day: date                     # evaluation day: FX and duty-rate date; import fees use day + transit_days
    category: str                 # gpu|ram|ssd|pc, or a 10-digit CN code
    p_scam: float


@dataclass(frozen=True)
class Line:
    label: str
    pln: Decimal
    note: str = ""


@dataclass
class NetResult:
    lines_cons: list[Line]
    lines_exp: list[Line]
    cons_net_pln: Decimal
    exp_net_pln: Decimal
    capital_pln: Decimal
    flags: list[str]
    capital_usd: Decimal | None = None
    vat_regime: str = ""


def dec(x: Any) -> Decimal:
    """Decimal from Decimal/int/str/float; floats (YAML) go through str to avoid binary noise."""
    if isinstance(x, Decimal):
        return x
    if isinstance(x, float):
        return Decimal(str(x))
    return Decimal(x)


def _neg(x: Decimal) -> Decimal:
    return -x if x else abs(x)  # no "-0.00" on cards


def _num(x: Decimal) -> str:
    return format(x.normalize(), "f")


def _as_date(v: Any) -> date:
    return v if isinstance(v, date) else date.fromisoformat(str(v))


def norm_country(code: str) -> str:
    c = (code or "").strip().upper()
    return COUNTRY_ALIASES.get(c, c)


# ---------------------------------------------------------------- data files (cached by path + mtime)

_CACHE: dict[tuple[str, str], tuple[int, Any]] = {}


def _cached(kind: str, path: Path, parse: Callable[[Any], Any]) -> Any:
    path = Path(path)
    mtime = path.stat().st_mtime_ns
    key = (kind, str(path.resolve()))
    hit = _CACHE.get(key)
    if hit is not None and hit[0] == mtime:
        return hit[1]
    val = parse(yaml.safe_load(path.read_text()))
    _CACHE[key] = (mtime, val)
    return val


def _parse_fees(raw: dict) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for ch, c in raw["channels"].items():
        out[ch] = {
            "pct": dec(c.get("pct", 0)),
            "fixed": dec(c.get("fixed", 0)),
            "fixed_ccy": str(c.get("fixed_ccy", "PLN")).upper(),
            "ccy": str(c.get("ccy", "PLN")).upper(),
            "dest": norm_country(str(c.get("dest", "PL"))),
            "b2c": bool(c.get("b2c", True)),
            "vat_deductible": bool(c.get("vat_deductible", False)),
            "by_category": {cat: {k: dec(v) for k, v in o.items()}
                            for cat, o in (c.get("by_category") or {}).items()},
        }
    return out


def load_fees(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """Channel fees from data/fees.yaml (cached; re-read when the file's mtime changes). Do not mutate."""
    return _cached("fees", path or DATA / "fees.yaml", _parse_fees)


def load_shipping(path: Path | None = None) -> dict[str, Any]:
    """Shipping defaults and lanes from data/shipping.yaml (cached like load_fees). Do not mutate."""
    return _cached("shipping", path or DATA / "shipping.yaml", lambda raw: raw)


def _region(country: str) -> str:
    return "PL" if country == "PL" else ("EU" if country in EU else "nonEU")


def lane_vat_deductible(src: str, dst: str, shipping: dict[str, Any] | None = None) -> bool:
    """True if shipping on this lane is invoiced with Polish VAT we can deduct. Unknown lane: False."""
    lanes = (shipping or load_shipping()).get("lanes") or {}
    lane = lanes.get(f"{_region(norm_country(src))}_{_region(norm_country(dst))}") or {}
    return bool(lane.get("vat_deductible", False))


def _cn_code(category: str) -> str:
    if category in DEFAULT_CN:
        return DEFAULT_CN[category]
    if re.fullmatch(r"\d{10}", category or ""):
        return category
    raise ValueError(f"no CN code for category {category!r}: pass a 10-digit CN code as the category")


def vat_regime(origin: str, seller_is_business: bool, has_vat_invoice: bool, margin_for_private: bool) -> str:
    origin = norm_country(origin)
    if origin not in EU:
        return IMPORT
    if origin == "PL":
        if has_vat_invoice:
            return INVOICE
        if seller_is_business or not margin_for_private:
            return NO_INPUT
        return MARGIN
    if seller_is_business:
        return INTRA_EU_B2B
    return MARGIN if margin_for_private else NO_INPUT


async def net(inp: NetInputs, cfg, fx, customs: DutyLookup | None = None) -> NetResult:
    """Net profit for one exit channel. `cfg` is the root Config (economics, pickup and rules are
    required); `customs` is needed for non-EU origins unless the consignment is low-value."""
    econ, tax = cfg.economics, cfg.economics.tax
    rules = cfg.get("rules")
    if rules is None:
        raise ValueError("cfg.rules is required (capital_cap_usd)")
    fees_all = load_fees()
    ch = inp.exit_channel
    if ch not in fees_all:
        raise ValueError(f"unknown exit channel {ch!r} (not in data/fees.yaml)")
    fee = fees_all[ch]
    cat_fee = fee["by_category"].get(inp.category, {})
    pct, fixed = cat_fee.get("pct", fee["pct"]), cat_fee.get("fixed", fee["fixed"])
    origin, day, items = norm_country(inp.origin_country), inp.day, int(inp.items)
    if items < 1:
        raise ValueError("items must be >= 1")
    if not 0 <= dec(inp.p_scam) <= 1:
        raise ValueError(f"p_scam must be within 0..1, got {inp.p_scam}")
    vat = dec(tax.vat_rate)
    vat_frac = vat / (1 + vat)  # e.g. 23/123: the VAT inside a gross price
    vat_frac_txt = f"{_num(vat * 100)}/{_num(100 + vat * 100)}"
    spread = dec(econ.fx_spread)
    shipping = load_shipping()
    flags: list[str] = []

    # ---------------------------------------------------------------- purchase side (both scenarios)
    buy = await fx.to_pln(dec(inp.buy_price), inp.buy_ccy, day)
    inbound = q2(dec(inp.inbound_ship_pln))
    outbound = q2(dec(inp.outbound_ship_pln))
    buy_note = (f"{inp.buy_price} {inp.buy_ccy.upper()} at NBP mid {await fx.rate(inp.buy_ccy, day)} ({day})"
                if inp.buy_ccy.upper() != "PLN" else "")
    pre: list[Line] = [Line("buy price", _neg(buy), buy_note),
                       Line("inbound shipping", _neg(inbound), "seller/forwarder to us, gross")]
    capital = buy + inbound
    # Polish-invoiced service costs whose VAT we deduct (gross amounts); fees are added per scenario.
    deductible: list[tuple[str, Decimal]] = []
    if lane_vat_deductible(origin, "PL", shipping):
        deductible.append(("inbound shipping", inbound))
    if lane_vat_deductible("PL", fee["dest"], shipping):
        deductible.append(("outbound shipping", outbound))

    if inp.pickup_km is not None:
        pk = cfg.pickup
        km = dec(inp.pickup_km)
        pickup = q2(dec(pk.pln_per_km) * 2 * km + dec(pk.pln_per_trip))
        pre.append(Line("pickup", _neg(pickup), f"{pk.pln_per_km} PLN/km x 2 x {km} km + {pk.pln_per_trip} PLN"))
        capital += pickup
        if km > dec(pk.max_km) or origin not in [norm_country(c) for c in pk.countries]:
            flags.append("pickup_out_of_range")

    regime = vat_regime(origin, inp.seller_is_business, inp.has_vat_invoice,
                        bool(tax.get("vat_margin_scheme_private", True)))
    import_vat = ZERO
    if regime == IMPORT:
        ic = econ.import_costs
        arrival = day + timedelta(days=int(ic.transit_days))
        value_eur = buy / await fx.rate("EUR", day)
        # Low-value consignment: the flat EUR/item duty (inside the import fees) REPLACES ad-valorem duty.
        low_value = value_eur <= dec(ic.consignment_max_eur) and arrival >= _as_date(ic.consignment_fee_from)
        cif = buy + inbound
        if low_value:
            duty = ZERO
            duty_note = f"replaced by the low-value duty EUR {ic.consignment_fee_eur}/item (goods <= EUR {ic.consignment_max_eur})"
        else:
            if customs is None:
                raise ValueError(f"import from {origin} needs a duty lookup (customs) to compute the duty")
            cn = _cn_code(inp.category)
            rate = dec(await customs.duty_rate(cn, origin, day))
            duty = q2(rate * cif)
            duty_note = f"CN {cn}, {_num(rate * 100)}% x CIF {cif}"
        fees_eur = import_fees_eur(value_eur, items, arrival, cfg)
        imp_fees = await fx.to_pln(fees_eur, "EUR", day)
        import_vat = q2(vat * (cif + duty + imp_fees))
        clearance = max(q2(dec(ic.clearance_fee_pln)),
                        q2(dec(ic.clearance_pct_of_duties_taxes) * (duty + imp_fees + import_vat)))
        pre += [
            Line("customs duty", _neg(duty), duty_note),
            Line("EU import fees", _neg(imp_fees),
                 f"EUR {fees_eur} on arrival {arrival} (low-value EUR {ic.consignment_fee_eur}/item, "
                 f"handling EUR {ic.handling_fee_eur}/item)"),
            Line("import VAT", _neg(import_vat), f"{_num(vat * 100)}% x (CIF {cif} + duty {duty} + fees {imp_fees})"),
            Line("clearance fee", _neg(clearance),
                 f"max({ic.clearance_fee_pln} PLN, {ic.clearance_pct_of_duties_taxes} x duties and taxes), gross"),
        ]
        capital += duty + imp_fees + import_vat + clearance
        deductible.append(("clearance fee", clearance))

    fx_buy = q2(spread * buy) if inp.buy_ccy.upper() != "PLN" else None
    if fx_buy is not None:
        capital += fx_buy
    risk = q2(dec(inp.p_scam) * buy)

    # ---------------------------------------------------------------- flags
    if origin != "PL":
        flags.append("bdo_check")
    if regime == INTRA_EU_B2B and not tax.vat_ue_registered:
        flags.append("needs_vat_ue")
    if ch not in econ.exit_channels.enabled:
        flags.append("channel_disabled")
    capital_usd = await fx.pln_to_usd(capital, day)
    if capital_usd > dec(rules.capital_cap_usd):
        flags.append("over_capital_cap")

    # ---------------------------------------------------------------- one scenario per exit price
    async def scenario(exit_price: Decimal) -> tuple[list[Line], Decimal]:
        e = q2(dec(exit_price))
        fixed_pln = await fx.to_pln(fixed * items, fee["fixed_ccy"], day) if fixed else ZERO
        fees = q2(pct * e) + fixed_pln
        out_vat = q2(e * vat_frac)
        if regime == MARGIN:
            due = q2(max(ZERO, e - buy) * vat_frac)
            vat_note = f"margin scheme: {vat_frac_txt} x max(0, {e} - {buy})"
        elif regime == INVOICE:
            in_vat = q2(buy * vat_frac)
            due = out_vat - in_vat
            vat_note = f"output {vat_frac_txt} x {e} = {out_vat} - invoice VAT {in_vat}"
        elif regime == IMPORT:
            due = out_vat - import_vat
            vat_note = f"output {vat_frac_txt} x {e} = {out_vat} - import VAT {import_vat}"
        else:
            due = out_vat
            vat_note = f"output {vat_frac_txt} x {e} = {out_vat}, no deductible input VAT ({regime})"
        parts = ([("channel fees", fees)] if fee["vat_deductible"] else []) + deductible
        svc = [(name, q2(amount * vat_frac)) for name, amount in parts]
        svc_vat = sum((v for _, v in svc), ZERO)
        svc_note = (" + ".join(f"{name} {v}" for name, v in svc) or "none deductible") + f" ({vat_frac_txt} of gross)"
        lines = [Line("exit price", e, f"{ch}"),
                 Line("channel fees", _neg(fees), f"{_num(pct * 100)}% + {fixed} {fee['fixed_ccy']} x {items}, gross"),
                 *pre,
                 Line("VAT due", _neg(due), vat_note),
                 Line("input VAT on services", svc_vat, svc_note),
                 Line("outbound shipping", _neg(outbound), "us to the buyer, gross")]
        if fee["b2c"]:
            lines.append(Line("returns reserve", _neg(q2(dec(econ.returns_reserve) * e)),
                              f"{econ.returns_reserve} x exit (14-day withdrawal, 2-year warranty)"))
            lines.append(Line("fiscal cost", _neg(q2(dec(econ.fiscal_cost_per_b2c_sale_pln) * items)),
                              "cash register per B2C sale"))
        if fx_buy is not None:
            lines.append(Line("FX spread (buy)", _neg(fx_buy), f"{spread} x {buy}"))
        if fee["ccy"] != "PLN":
            lines.append(Line("FX spread (exit)", _neg(q2(spread * e)), f"{spread} x {e}, paid out in {fee['ccy']}"))
        lines.append(Line("risk haircut", _neg(risk), f"p_scam {inp.p_scam} x {buy}"))
        pretax = sum((l.pln for l in lines), ZERO)
        income_tax, tax_note = _income_tax(tax, pretax, e, items)
        lines.append(Line("income tax", _neg(income_tax), f"{tax_note}; pre-tax {pretax}"))
        return lines, pretax - income_tax

    lines_cons, cons = await scenario(inp.exit_price_cons_pln)
    lines_exp, exp = await scenario(inp.exit_price_exp_pln)
    return NetResult(lines_cons=lines_cons, lines_exp=lines_exp, cons_net_pln=cons, exp_net_pln=exp,
                     capital_pln=capital, flags=flags, capital_usd=capital_usd, vat_regime=regime)


def _income_tax(tax, pretax: Decimal, exit_pln: Decimal, items: int) -> tuple[Decimal, str]:
    mode = tax.tax_mode
    if mode == "liniowy":
        rate = dec(tax.pit_rate) + dec(tax.health_rate)
        return q2(rate * max(ZERO, pretax)), f"liniowy {tax.pit_rate} + health {tax.health_rate} on profit"
    if mode == "skala":
        rate = dec(tax.skala_pit_rate) + dec(tax.skala_health_rate)
        return q2(rate * max(ZERO, pretax)), f"skala {tax.skala_pit_rate} + health {tax.skala_health_rate} on profit"
    if mode == "ryczalt":
        t = q2(dec(tax.ryczalt_rate) * exit_pln) + q2(dec(tax.ryczalt_health_pln_per_sale) * items)
        return t, f"ryczalt {tax.ryczalt_rate} x revenue {exit_pln} + health {tax.ryczalt_health_pln_per_sale}/sale"
    raise ValueError(f"unknown tax_mode {mode!r}")


def best_exit(results: dict[str, NetResult], enabled: Iterable[str] | None = None) -> tuple[str, NetResult]:
    """Highest conservative net among enabled channels (first wins on a tie)."""
    allowed = set(enabled) if enabled is not None else None
    cands = [(ch, r) for ch, r in results.items()
             if "channel_disabled" not in r.flags and (allowed is None or ch in allowed)]
    if not cands:
        raise ValueError("no enabled exit channel among the results")
    return max(cands, key=lambda cr: cr[1].cons_net_pln)
