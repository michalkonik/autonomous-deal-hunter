"""`dh eval --add-from-marks`: the OWNER extends evals/golden.jsonl from verified marks. FROZEN (config.FROZEN_PATHS).

This is a human-invoked command, never called by the improvement loop (dh/improve has no write path to evals/;
see tests/improve/test_improve_frozen.py). The golden set is append-only: a mark already turned into a case is
skipped. Each case carries the listing as stored, the deal's market (P25 / median / effective sold comps from the
deal's evidence) and the verified expectation:
- Bought / Sold -> the key the deal had and "a deal" (tier 1-3);
- Skipped with a wrongness reason (evals.labels.FP_REASON) -> "not tier 1-3".
"""
from __future__ import annotations

import json
from pathlib import Path

from .labels import is_false_positive, is_true_positive

GOLDEN = Path(__file__).resolve().parent / "golden.jsonl"
_SOLD_WEIGHT = {"owner_sale": 1.0, "ebay_sold": 1.0, "probably_sold": 0.5}  # as config/valuation.yaml


def _listing(r) -> dict:
    return {k: r[k] for k in ("source", "title", "description", "price", "currency", "country", "city", "lat", "lon",
                              "seller_age_days", "seller_ratings", "ships", "pickup_only", "is_business")} | {
        "photos": len(json.loads(r["photos"] or "[]")), "contact_in_text": bool(r["contact_in_text"])}


def _market(deal) -> dict | None:
    ev = json.loads(deal["evidence_json"] or "{}")
    est = ev.get("estimate") or {}
    ni = ev.get("net_inputs") or {}
    p25, med = est.get("p25_pln") or ni.get("exit_price_cons_pln"), est.get("median_pln") or ni.get("exit_price_exp_pln")
    if not p25 or not med:
        return None
    comps = est.get("comps") or []
    # the card keeps the newest comps only, so this undercounts: the case is conservative about confidence
    sold = sum(_SOLD_WEIGHT.get(c.get("kind"), 0.0) for c in comps)
    return {"key": deal["key"], "p25_pln": str(p25), "median_pln": str(med), "p75_pln": str(est.get("p75_pln") or med),
            "n_sold_effective": round(sold, 2), "n_effective": float(len(comps)),
            "channels": [ni.get("exit_channel") or "allegro"]}


def add_from_marks(store, path: Path = GOLDEN) -> int:
    """Append one golden case per new verified mark; returns the number added."""
    path = Path(path)
    have = set()
    if path.is_file():
        for line in path.read_text().splitlines():
            if line.strip():
                have.add(json.loads(line).get("mark_id"))
    added = []
    rows = store.conn.execute("select m.id mid, m.status, m.note, d.*, l.*, l.id lid from marks m "
                              "join deals d on d.id=m.deal_id join listings l on l.id=d.listing_ref order by m.id")
    for r in rows:
        if r["mid"] in have:
            continue
        if is_true_positive(r["status"]):
            expect = {"key": r["key"], "max_tier": 3}
        elif is_false_positive(r["status"], r["note"]):
            expect = {"min_tier": 4}
        else:
            continue
        added.append({"id": f"mark-{r['mid']}", "mark_id": r["mid"], "why": f"owner mark {r['status']}: {r['note'] or ''}"[:200],
                      "listing": _listing(r), "market": _market(r), "expect": expect})
    if added:
        with open(path, "a", encoding="utf-8") as fh:
            for c in added:
                fh.write(json.dumps(c, ensure_ascii=False) + "\n")
    return len(added)
