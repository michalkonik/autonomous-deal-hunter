"""Weekly scoreboard (spec §9.7) and the improvement status, as Markdown for the log repo.

Per week (the last 168 watched hours, or the last 7 days before any round was recorded): expected net of deals by
tier; precision on owner-marked deals; false tier-1 count; missed-deal count; fetches, LLM tokens and kWh per deal;
cost against expected and realised profit; adapter health. Then the improvement loop: state, the week's proposals
with their predicted effects and outcomes, the prediction ledger (share of proposals that passed the gates over
30 days), and the line "working well; no changes proposed" when the playbook is frozen in maintenance mode.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from evals.labels import is_false_positive, is_true_positive

from .. import cards, economics
from .. import config as dhconfig
from ..scheduler import MemoFx
from . import gates, monitor
from .playbook import ROOT
from .replay import StoreFx

MAINTENANCE_LINE = "working well; no changes proposed"


def _q(x: Decimal) -> str:
    return f"{x.quantize(Decimal('0.01'))}"


def _window(store, now: datetime) -> tuple[str, str, float]:
    """(since ISO, until ISO, watched hours in the window)."""
    monitor.migrate(store)
    wh = monitor.watched_hours(store, until=now)
    r = store.conn.execute("select min(r.started_at) s from improve_rounds i join rounds r on r.id=i.round_id "
                           "where i.watched_h >= ?", (wh - gates.WEEKLY_REVIEW_WATCHED_HOURS,)).fetchone()
    since = r["s"] if r and r["s"] else (now - timedelta(days=7)).isoformat()
    return since, now.isoformat(), min(wh, gates.WEEKLY_REVIEW_WATCHED_HOURS)


async def _realised(store, deal_ids: set[int], cfg) -> tuple[Decimal, int]:
    """Realised conservative net of the window's SOLD deals: the stored net inputs with the sold price as the exit,
    through the same economics.net the books page uses (stored FX only). Returns (PLN, deals that could be priced)."""
    total, n = Decimal(0), 0
    mfx = MemoFx(StoreFx(store, max_back_days=30))
    for m in store.conn.execute("select deal_id, price_pln from marks where id in (select max(id) from marks "
                                "group by deal_id) and status='sold' and price_pln is not null"):
        if m["deal_id"] not in deal_ids:
            continue
        deal = store.get_deal(m["deal_id"])
        try:
            inp = cards.inputs_from_json(json.loads(deal.evidence_json or "{}")["net_inputs"])
            price = Decimal(m["price_pln"])
            res = await economics.net(replace(inp, exit_price_cons_pln=price, exit_price_exp_pln=price), cfg, mfx, None)
            total += res.cons_net_pln
            n += 1
        except Exception:  # noqa: BLE001 - missing evidence, FX or a duty lookup: not priced here
            continue
    return total, n


async def scoreboard_async(store, now: datetime | None = None, cfg=None) -> dict:
    sb = scoreboard(store, now)
    cfg = cfg if cfg is not None else dhconfig.load(ROOT)
    sb["realised_net_pln"], sb["realised_n"] = await _realised(store, sb.pop("_deal_ids"), cfg)
    return sb


def scoreboard(store, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    since, until, wh = _window(store, now)
    deals = store.conn.execute("select id, tier, exp_net_usd, cons_net_usd, exp_net_pln from deals "
                               "where created_at >= ? and created_at <= ?", (since, until)).fetchall()
    by_tier: dict[int, dict] = {}
    for d in deals:
        t = by_tier.setdefault(d["tier"], {"n": 0, "exp_usd": Decimal(0), "cons_usd": Decimal(0)})
        t["n"] += 1
        t["exp_usd"] += Decimal(d["exp_net_usd"])
        t["cons_usd"] += Decimal(d["cons_net_usd"])
    ids = {d["id"]: d for d in deals}
    tp = fp = false_t1 = 0  # per DEAL, from its latest mark (a deal marked twice counts once)
    for m in store.conn.execute("select deal_id, status, note from marks where id in "
                                "(select max(id) from marks group by deal_id)"):
        d = ids.get(m["deal_id"])
        if d is None:
            continue
        if is_true_positive(m["status"]):
            tp += 1
        elif is_false_positive(m["status"], m["note"]):
            fp += 1
            false_t1 += int(d["tier"] == 1)
    try:
        missed = store.conn.execute("select count(*) from improve_failures where cause='missed_deal' and at >= ?",
                                    (since,)).fetchone()[0]
    except Exception:  # noqa: BLE001 - no reflection yet
        missed = 0
    costs = {r["kind"]: (Decimal(r["pln"] or 0), float(r["units"] or 0)) for r in store.conn.execute(
        "select c.kind, sum(cast(c.amount_pln as real)) pln, sum(c.units) units from costs c join rounds r "
        "on r.id=c.round_id where r.started_at >= ? and r.started_at <= ? group by c.kind", (since, until))}
    n_deals = sum(t["n"] for k, t in by_tier.items() if k in (1, 2, 3)) or 0
    per = (lambda kind: round(costs.get(kind, (0, 0.0))[1] / n_deals, 2) if n_deals else None)
    health = [dict(r) for r in store.conn.execute(
        "select source, kind, count(*) n from health where at >= ? group by source, kind order by n desc",
        (since.replace("T", " ")[:19],))]
    cost_pln = sum((v[0] for v in costs.values()), Decimal(0))
    exp_pln = sum((Decimal(d["exp_net_pln"]) for d in deals if d["tier"] in (1, 2, 3)), Decimal(0))
    return {"since": since, "until": until, "watched_hours": round(wh, 1), "deals_by_tier": by_tier,
            "precision": round(tp / (tp + fp), 2) if tp + fp else None, "marked": tp + fp, "false_tier1": false_t1,
            "missed_deals": missed, "fetches_per_deal": per("fetch"), "llm_tokens_per_deal": per("llm"),
            "kwh_per_deal": per("energy"), "cost_pln": cost_pln, "expected_net_pln": exp_pln,
            "realised_net_pln": None, "realised_n": 0, "health": health, "_deal_ids": set(ids)}


def _improvement(store, since: str) -> dict:
    from .controller import STATE_SQL
    store.ensure_table(STATE_SQL)
    r = store.conn.execute("select value from improve_state where key='controller'").fetchone()
    st = json.loads(r["value"]) if r else {}
    props = []
    for p in store.conn.execute("select id, status, payload, created_at from proposals where created_at >= ? "
                                "order by id", (since.replace("T", " ")[:19],)):
        pl = json.loads(p["payload"] or "{}")
        d = pl.get("delta") or {}
        props.append({"id": p["id"], "status": p["status"], "stage": pl.get("stage"), "trigger": pl.get("trigger"),
                      "change": f"{d.get('op')} {(d.get('item') or {}).get('id')}",
                      "predicted": d.get("predicted_effect") or "", "hypothesis": d.get("hypothesis") or ""})
    month = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    rows = store.conn.execute("select status from proposals where created_at >= ?", (month,)).fetchall()
    passed = sum(1 for x in rows if x["status"] in ("shadow", "probation", "accepted"))
    return {"state": st, "proposals": props,
            "gate_pass_rate_30d": round(passed / len(rows), 2) if rows else None, "proposals_30d": len(rows)}


def weekly(store, now: datetime | None = None) -> str:
    """The weekly report (Markdown). Inside a running event loop use `await weekly_async(...)`."""
    return asyncio.run(weekly_async(store, now))


async def weekly_async(store, now: datetime | None = None, cfg=None) -> str:
    now = now or datetime.now(timezone.utc)
    sb = await scoreboard_async(store, now, cfg)
    imp = _improvement(store, sb["since"])
    st = imp["state"]
    lines = [f"# Deal Hunter weekly report, {now.date().isoformat()}", "",
             f"Window: {sb['since'][:16]} .. {sb['until'][:16]} ({sb['watched_hours']} watched hours).", "",
             "## Scoreboard", "", "| Tier | Deals | Expected net (USD) | Conservative net (USD) |", "|---|---|---|---|"]
    for t in sorted(k for k in sb["deals_by_tier"] if k is not None):
        v = sb["deals_by_tier"][t]
        lines.append(f"| {t} | {v['n']} | {_q(v['exp_usd'])} | {_q(v['cons_usd'])} |")
    if not sb["deals_by_tier"]:
        lines.append("| - | 0 | 0.00 | 0.00 |")
    prec = "n/a (no marks)" if sb["precision"] is None else f"{sb['precision']:.0%} of {sb['marked']} marked deals"
    lines += ["", f"- Precision on owner-marked deals: {prec}",
              f"- False tier-1 deals: {sb['false_tier1']}", f"- Missed deals: {sb['missed_deals']}",
              f"- Per deal (tier 1-3): fetches {sb['fetches_per_deal']}, LLM tokens {sb['llm_tokens_per_deal']}, "
              f"kWh {sb['kwh_per_deal']}",
              f"- Cost {_q(sb['cost_pln'])} PLN against expected net {_q(sb['expected_net_pln'])} PLN (tier 1-3) "
              f"and realised net {_q(sb['realised_net_pln'])} PLN ({sb['realised_n']} sold deals)",
              "- Adapter health: " + (", ".join(f"{h['source']} {h['kind']} x{h['n']}" for h in sb["health"][:8])
                                      or "no events"), "", "## Self-improvement", ""]
    phase = st.get("phase", "idle")
    if st.get("maintenance") and phase == "maintenance":
        lines.append(f"**{MAINTENANCE_LINE}.** Maintenance mode: {st.get('maintenance_reason')}. The playbook is "
                     "frozen; the bandit keeps adapting; alarms and the weekly review can reopen it.")
    else:
        lines.append(f"State: {phase}" + (f" (proposal {st.get('proposal_id')})" if st.get("proposal_id") else "")
                     + f"; consecutive rejections {st.get('consecutive_rejections', 0)}; active playbook version "
                       f"{st.get('champion')}.")
    if imp["proposals"]:
        lines += ["", "| # | Change | Trigger | Predicted effect | Outcome |", "|---|---|---|---|---|"]
        for p in imp["proposals"]:
            lines.append(f"| {p['id']} | {p['change']} | {p['trigger']} | {p['predicted'][:80]} | "
                         f"{p['status']} ({p['stage']}) |")
    else:
        lines.append("No proposals this week.")
    rate = imp["gate_pass_rate_30d"]
    lines += ["", f"Prediction ledger: {imp['proposals_30d']} proposals in 30 days, "
                  + ("n/a" if rate is None else f"{rate:.0%} passed the gates")
                  + (" (below 20%: reflect less often or use a stronger advisor model)" if rate is not None
                     and imp["proposals_30d"] >= 5 and rate < 0.2 else "") + "."]
    return "\n".join(lines) + "\n"
