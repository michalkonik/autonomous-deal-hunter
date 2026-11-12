"""Deal cards (spec section 12): Markdown + JSON, written to `deals/YYYY-MM-DD/<slug>-<deal_id>.md|.json`.

Every number on a card traces to its source: the line-by-line net, the FX rate and date, the comparables with
links and dates. The card JSON contains ONLY the keys in PUBLIC_KEYS (nested: PUBLIC_COMP_KEYS,
PUBLIC_LISTING_KEYS). It never carries seller text, only `p_scam` and flag names. `dh/publish.py` imports
these constants as its allowlist, so a card can never leak more than the publisher lets through.

Storage contract (a card can be rebuilt from the Deal alone, which `dh verify` relies on):
  deal.breakdown_json = {"cons": [Line...], "exp": [Line...]}        (see `breakdown_json`)
  deal.evidence_json  = `build_evidence(...)`: net inputs, FX, comps, screenshots, risk, checklist.
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import yaml

from .economics import NetInputs

PUBLIC_KEYS = frozenset({
    "deal_id", "key", "tier", "star", "effort", "confidence", "status",
    "buy_price", "buy_currency", "buy_pln", "exit_p25_pln", "exit_median_pln",
    "cons_net_pln", "exp_net_pln", "cons_net_usd", "exp_net_usd", "capital_usd", "days_to_sell",
    "breakdown", "fx", "fx_usd",
    "listing", "comps", "retail_ceiling_pln", "trend_30d", "screenshots",
    "risk", "reasons", "checklist", "outcome", "created_at", "verified_at", "triage", "gone_reason",
})
PUBLIC_COMP_KEYS = frozenset({"url", "price", "date", "kind"})
PUBLIC_LISTING_KEYS = frozenset({"url", "title", "price", "currency", "country", "city"})

DEFAULT_OUTCOME = {"status": "open", "price_pln": None, "date": None, "note": ""}
MAX_COMPS = 50
_NET_INPUT_FIELDS = ("buy_price", "buy_ccy", "origin_country", "seller_is_business", "has_vat_invoice",
                     "exit_channel", "exit_price_cons_pln", "exit_price_exp_pln", "inbound_ship_pln",
                     "outbound_ship_pln", "pickup_km", "items", "day", "category", "p_scam")
_DEC_FIELDS = {"buy_price", "exit_price_cons_pln", "exit_price_exp_pln", "inbound_ship_pln", "outbound_ship_pln"}


# ---------------------------------------------------------------- helpers

def _s(x: Any) -> str | None:
    return None if x is None else str(x)


def _iso(t: datetime | None) -> str | None:
    return None if t is None else t.astimezone(timezone.utc).isoformat()


def _q2(x: Any) -> str:
    return str(Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


_TRANSLIT = str.maketrans({"\u0142": "l", "\u0141": "L", "\u00f8": "o", "\u00d8": "O", "\u00df": "ss"})


def slug(text: str | None, limit: int = 40) -> str:
    t = unicodedata.normalize("NFKD", (text or "").translate(_TRANSLIT)).encode("ascii", "ignore").decode().lower()
    t = re.sub(r"[^a-z0-9]+", "-", t).strip("-")[:limit].strip("-")
    return t or "deal"


def inputs_to_json(inp: NetInputs) -> dict:
    out: dict[str, Any] = {}
    for f in _NET_INPUT_FIELDS:
        v = getattr(inp, f)
        out[f] = v.isoformat() if isinstance(v, date) else str(v) if f in _DEC_FIELDS else v
    return out


def inputs_from_json(d: dict) -> NetInputs:
    kw = dict(d)
    for f in _DEC_FIELDS:
        kw[f] = Decimal(str(kw[f]))
    kw["day"] = date.fromisoformat(kw["day"])
    kw["pickup_km"] = None if kw.get("pickup_km") is None else float(kw["pickup_km"])
    return NetInputs(**{f: kw[f] for f in _NET_INPUT_FIELDS})


def _lines(lines) -> list[dict]:
    return [{"label": l.label, "pln": _q2(l.pln), "note": l.note} for l in lines]


def breakdown_json(net) -> str:
    """Value for `Deal.breakdown_json` from an economics.NetResult."""
    return json.dumps({"cons": _lines(net.lines_cons), "exp": _lines(net.lines_exp)})


def _comp_dict(c) -> dict:
    return {"url": c.ref, "price": _q2(c.price_pln), "date": _iso(c.at), "kind": c.kind}


def build_evidence(inp: NetInputs, listing, estimate, risk, *, fx_rate: Decimal, fx_date: date,
                   buy_pln: Decimal, screenshots: dict | None = None, fx_rates: list[dict] | None = None,
                   fx_usd: tuple[Decimal, date] | None = None) -> str:
    """Value for `Deal.evidence_json`: everything the card shows plus the exact net inputs `dh verify` replays.
    `fx_rates` = every (currency, day) -> (rate, NBP effective date) the valuation used (money.RecordingFx), replayed
    by `dh verify` and the books; `fx_usd` = the USD rate and its NBP date the USD figures use."""
    ev = {
        "net_inputs": inputs_to_json(inp),
        "fx": {"currency": inp.buy_ccy.upper(), "rate": str(fx_rate), "date": fx_date.isoformat()},
        "fx_rates": list(fx_rates or []),
        "fx_usd": None if fx_usd is None else {"rate": str(fx_usd[0]), "date": fx_usd[1].isoformat()},
        "buy_pln": _q2(buy_pln),
        "listing": _listing_dict(listing),
        "estimate": _estimate_dict(estimate),
        "screenshots": {"discovery": None, "verification": None, **(screenshots or {})},
        "risk": {"p_scam": float(risk.p_scam), "flags": list(risk.flags)},
        "checklist": list(risk.checklist),
    }
    return json.dumps(ev)


def _listing_dict(l) -> dict:
    return {"url": l.url, "title": l.title, "price": str(l.price), "currency": l.currency,
            "country": l.country, "city": l.city}


def _estimate_dict(e) -> dict:
    if e is None:
        return {}
    return {"p25_pln": _q2(e.p25_pln), "median_pln": _q2(e.median_pln), "days_to_sell": e.days_to_sell,
            "ceiling_pln": None if e.ceiling_pln is None else _q2(e.ceiling_pln), "trend_30d": e.trend_30d,
            "comps": [_comp_dict(c) for c in sorted(e.comps, key=lambda c: c.at, reverse=True)[:MAX_COMPS]]}


# ---------------------------------------------------------------- card data

def card_data(deal, ev: dict, breakdown: dict, root: Path | None = None) -> dict:
    """The public card dict, built from the Deal row plus its evidence and breakdown."""
    est = ev.get("estimate") or {}
    lst = ev.get("listing") or {}
    risk = ev.get("risk") or {}
    out = {
        "deal_id": deal.id, "key": deal.key, "tier": deal.tier, "star": bool(deal.star), "effort": deal.effort,
        "confidence": deal.confidence, "status": deal.status,
        "buy_price": lst.get("price"), "buy_currency": lst.get("currency"), "buy_pln": ev.get("buy_pln"),
        "exit_p25_pln": est.get("p25_pln"), "exit_median_pln": est.get("median_pln"),
        "cons_net_pln": _q2(deal.cons_net_pln), "exp_net_pln": _q2(deal.exp_net_pln),
        "cons_net_usd": _q2(deal.cons_net_usd), "exp_net_usd": _q2(deal.exp_net_usd),
        "capital_usd": _q2(deal.capital_usd), "days_to_sell": est.get("days_to_sell"),
        "breakdown": {"cons": breakdown.get("cons", []), "exp": breakdown.get("exp", [])},
        "fx": ev.get("fx"), "fx_usd": ev.get("fx_usd"),
        "listing": {k: v for k, v in lst.items() if k in PUBLIC_LISTING_KEYS},
        "comps": [{k: v for k, v in c.items() if k in PUBLIC_COMP_KEYS} for c in est.get("comps", [])],
        "retail_ceiling_pln": est.get("ceiling_pln"), "trend_30d": est.get("trend_30d"),
        "screenshots": {k: _rel(v, root) for k, v in
                        (ev.get("screenshots") or {"discovery": None, "verification": None}).items()},
        "risk": {"p_scam": risk.get("p_scam"), "flags": list(risk.get("flags", []))},
        "triage": _triage_dict(ev.get("triage")),
        "reasons": list(deal.reasons), "checklist": list(ev.get("checklist", [])),
        "outcome": dict(DEFAULT_OUTCOME),
        "created_at": _iso(deal.created_at), "verified_at": _iso(deal.verified_at),
    }
    gone = ev.get("gone") if isinstance(ev.get("gone"), dict) else None
    out["gone_reason"] = str(gone.get("reason") or "") if deal.status == "gone" and gone else None
    assert set(out) <= PUBLIC_KEYS, set(out) - PUBLIC_KEYS
    return out


def _triage_dict(t) -> dict | None:
    if not isinstance(t, dict):
        return None
    return {"verdict": t.get("verdict"), "reasons": [str(r) for r in t.get("reasons") or []]}


def render(deal, listing, estimate, net, risk, root: Path | None = None) -> tuple[str, dict]:
    """(Markdown, JSON dict) for one deal. FX rate/date and screenshots come from `deal.evidence_json`
    (written by `build_evidence`); everything else from the objects passed in."""
    prev = _load(deal.evidence_json)
    ev = {
        "net_inputs": prev.get("net_inputs"), "fx": prev.get("fx"), "buy_pln": prev.get("buy_pln"),
        "fx_rates": prev.get("fx_rates") or [], "fx_usd": prev.get("fx_usd"), "triage": prev.get("triage"),
        "gone": prev.get("gone"),
        "listing": _listing_dict(listing), "estimate": _estimate_dict(estimate),
        "screenshots": prev.get("screenshots") or {"discovery": None, "verification": None},
        "risk": {"p_scam": float(risk.p_scam), "flags": list(risk.flags)}, "checklist": list(risk.checklist),
    }
    if ev["buy_pln"] is None and net.lines_cons:
        ev["buy_pln"] = _q2(-net.lines_cons[0].pln)
    data = card_data(deal, ev, json.loads(breakdown_json(net)), root)
    return _markdown(data), data


def from_deal(deal, root: Path | None = None) -> tuple[str, dict]:
    """Rebuild the card from stored evidence (no listing or estimate objects needed)."""
    data = card_data(deal, _load(deal.evidence_json), _load(deal.breakdown_json), root)
    return _markdown(data), data


def _load(text: str | None) -> dict:
    try:
        v = json.loads(text or "{}")
        return v if isinstance(v, dict) else {}
    except ValueError:
        return {}


def _esc(x) -> str:
    """Untrusted (seller-authored) text into one Markdown line: whitespace collapsed, link/table syntax escaped."""
    t = " ".join(str(x if x is not None else "").split())
    return re.sub(r"([\\\[\]|<>`])", r"\\\1", t)


def _rel(path, root: Path | None) -> str | None:
    """Screenshot path as stored in the card: relative to the card root, never an absolute local path."""
    if not path:
        return None
    p = Path(str(path))
    if p.is_absolute():
        try:
            return p.relative_to(Path(root).resolve() if root else Path("/nonexistent")).as_posix()
        except ValueError:
            return p.name
    return p.as_posix()


_URL_UNSAFE = {" ": "%20", "(": "%28", ")": "%29", "<": "%3C", ">": "%3E", "[": "%5B", "]": "%5D", "`": "%60"}


def _link(label: str, url: str | None) -> str:
    """A Markdown link. `label` is already escaped by the caller (never escaped again here); the URL's characters
    that would end or break the link target are percent-encoded. Without a web URL: the escaped URL text, or the
    label alone."""
    if url and str(url).startswith(("http://", "https://")):
        target = "".join(_URL_UNSAFE.get(ch, ch) for ch in str(url).strip())
        return f"[{label}]({target})"
    return _esc(url) if url else label


def _ask_ratio() -> str:
    """valuation.ask_to_sale_ratio (config/valuation.yaml), for the comparables legend of a card."""
    try:
        from .valuation import default_params
        return str(default_params().ask_to_sale_ratio)
    except Exception:  # noqa: BLE001 - a card is never lost over its legend
        return "0.92"


def _table(rows: list[dict]) -> list[str]:
    out = ["| line | PLN | note |", "|---|---:|---|"]
    for r in rows:
        out.append(f"| {_esc(r['label'])} | {r['pln']} | {_esc(r['note'])} |")
    return out


def _markdown(d: dict) -> str:
    fm = {"deal_id": d["deal_id"], "key": d["key"], "tier": d["tier"], "star": d["star"],
          "confidence": d["confidence"], "effort": d["effort"], "cons_net_usd": d["cons_net_usd"],
          "status": d.get("status") or "new", "created_at": d["created_at"], "outcome": dict(DEFAULT_OUTCOME)}
    head = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).rstrip()
    star = " ★" if d["star"] else ""
    lst = d["listing"]
    fx = d.get("fx") or {}
    shots = d["screenshots"] or {}
    gone = d.get("status") == "gone"
    L = [f"---\n{head}\n---", f"# T{d['tier']}{star} {_esc(d['key'] or 'deal')}" + (" (GONE)" if gone else ""), "",
         *([f"**Status: gone**: {_esc(d.get('gone_reason') or 'the listing is gone or failed re-verification')}", ""]
           if gone else []),
         "## Headline", "",
         f"- Buy: {d['buy_price']} {d['buy_currency']} ({d['buy_pln']} PLN)",
         f"- Exit P25: {d['exit_p25_pln']} PLN, median: {d['exit_median_pln']} PLN",
         f"- Conservative net: {d['cons_net_pln']} PLN ({d['cons_net_usd']} USD)",
         f"- Expected net: {d['exp_net_pln']} PLN ({d['exp_net_usd']} USD)",
         f"- Confidence: {d['confidence']}; effort: {d['effort']}; capital: {d['capital_usd']} USD; "
         f"days to sell: {d['days_to_sell']}",
         f"- Tier: {d['tier']}{star}", "",
         "## Net calculation", "", "### Conservative (exit at P25)", ""]
    L += _table(d["breakdown"]["cons"]) + ["", "### Expected (exit at median)", ""]
    L += _table(d["breakdown"]["exp"]) + [""]
    usd = d.get("fx_usd") or {}
    L += [f"FX: {fx.get('rate')} PLN per {fx.get('currency')} (NBP mid, {fx.get('date')})"
          + (f"; USD figures at {usd.get('rate')} PLN per USD (NBP mid, {usd.get('date')})" if usd else ""), "",
          "## Evidence", "",
          f"- Listing: {_link(_esc(lst.get('title') or 'listing'), lst.get('url'))}",
          f"- Screenshot at discovery: {_esc(shots.get('discovery') or 'none')}",
          f"- Screenshot at verification: {_esc(shots.get('verification') or 'none')}",
          f"- Retail ceiling: {d['retail_ceiling_pln']} PLN; 30-day trend: {d['trend_30d']}", "",
          "### Comparables", ""]
    if any(c.get("kind") == "active_ask" for c in d["comps"]):
        L += [f"Prices in PLN. An active ask is shown at x{_ask_ratio()} of its asking price (the price it is expected "
              "to sell at), so it is lower than on the linked page.", ""]
    L += [f"- {c['kind']} {c['price']} PLN on {(c['date'] or '')[:10]}: {_link('link', c['url'])}"
          for c in d["comps"]] or ["- none"]
    tri = d.get("triage")
    tri_line = ("not run (tier 3 or below)" if tri is None else
                f"{_esc(tri['verdict'])}" + (f": {_esc('; '.join(tri['reasons']))}" if tri["reasons"] else ""))
    L += ["", "## Risk", "", f"- p_scam: {float(d['risk']['p_scam']):.3f}",
          f"- Flags: {_esc(', '.join(d['risk']['flags'])) or 'none'}",
          f"- Triage (senior model, finalists only): {tri_line}", "", "## Why this tier", ""]
    L += [f"- {_esc(r)}" for r in d["reasons"]] or ["- n/a"]
    L += ["", "## How to buy safely", ""] + [f"- [ ] {_esc(c)}" for c in d["checklist"]]
    L += ["", "## Outcome", "",
          "Edit the `outcome:` block in the front matter (status: bought, skipped or sold; price_pln; date; note).", ""]
    return "\n".join(L)


def write(deal, root: Path, rendered: tuple[str, dict] | None = None) -> Path:
    """Write `<root>/deals/YYYY-MM-DD/<slug>-<deal_id>.md` and `.json`; returns the .md path.
    Rebuilds the card from the stored Deal when `rendered` is not given. An existing card's
    `outcome:` block is kept (a rewrite after verification must not erase a mark)."""
    md, data = rendered or from_deal(deal, root)
    day = deal.created_at.astimezone(timezone.utc).strftime("%Y-%m-%d")
    folder = Path(root) / "deals" / day
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"{slug(deal.key)}-{deal.id}"
    p = folder / f"{stem}.md"
    if p.is_file():
        try:
            m = re.match(r"\A---\n(.*?)\n---\n", p.read_text(), re.S)
            old = (yaml.safe_load(m.group(1)) or {}).get("outcome") if m else None
            if isinstance(old, dict) and old != DEFAULT_OUTCOME:
                md = _swap_outcome(md, old)
                data = {**data, "outcome": old}
        except (OSError, yaml.YAMLError):
            pass
    _atomic_write(p, md)
    _atomic_write(p.with_suffix(".json"), json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    return p


def _swap_outcome(md: str, outcome: dict) -> str:
    m = re.match(r"\A---\n(.*?)\n---\n(.*)\Z", md, re.S)
    fm = yaml.safe_load(m.group(1))
    fm["outcome"] = outcome
    return "---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).rstrip() + "\n---\n" + m.group(2)


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def set_outcome(path_md: Path, outcome: dict) -> None:
    """Set the `outcome:` block of an existing card (.md front matter and the sibling .json).
    Each file is replaced atomically (temp file + os.replace), so a crash never leaves a half-written card."""
    path_md = Path(path_md)
    js = path_md.with_suffix(".json")
    new_md = _swap_outcome(path_md.read_text(), outcome)
    new_js = None
    try:
        data = json.loads(js.read_text())
        if isinstance(data, dict):
            new_js = json.dumps({**data, "outcome": outcome}, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    except (OSError, ValueError):
        pass
    _atomic_write(path_md, new_md)
    if new_js is not None:
        _atomic_write(js, new_js)
