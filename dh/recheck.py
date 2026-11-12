"""`dh recheck`: re-run the CURRENT rules matcher over stored rules extractions (one-off, after a matcher fix).

Why: the comparables of a key are read live from `listings` + `extractions` (dh/ledger.py `_SALEABLE`). An
extraction stored by an older matcher keeps its verdict until the listing is evaluated again, which happens only
when it is seen again with a new price or after `reeval_hours`; a listing that is gone is never re-evaluated and
stays a comparable for up to `valuation.lookback_days` (90). The 30 October matcher fix (laptops and PCs naming a
GPU, water blocks) therefore did not clean the comparables already in a live database.

What it does, for every extraction made by the rules (`method='rules'`) that is a comparable today (a key, no bundle,
certainty >= ledger.COMP_MIN_CERTAINTY, not bait or for parts) and that the current matcher no longer accepts as one:
the stored extraction is replaced by the current rules result (so it stops being a comparable at once) and the
listing's evaluation is deleted (so the next round re-queues it, while it is still recent, and the LLM decides).
LLM extractions are left alone: the model saw the whole listing. Deals already logged are listed, never changed.

Dry by default; `--apply` writes. It takes the agent lock like any owner command: refused while the agent runs.
"""
from __future__ import annotations

from pathlib import Path

from . import ledger
from .match import rules_extract
from .models import Extraction


def _comparable(key, bundle, certainty, box, parts, wanted, condition) -> bool:
    return (bool(key) and bundle in (None, "", "[]") and float(certainty or 0) >= ledger.COMP_MIN_CERTAINTY
            and not box and not parts and not wanted and condition != "for_parts")


def _still_comparable(e: Extraction, old_key: str) -> bool:
    return (e.key == old_key and not e.bundle_items and e.certainty >= ledger.COMP_MIN_CERTAINTY
            and not (e.box_only or e.for_parts or e.wanted_ad))


def recheck(store, *, apply: bool = False, root: Path | None = None, examples: int = 20) -> dict:
    """{'checked', 'changed', 'by_key': {key: n}, 'examples': [...], 'deals': [deal ids on changed listings],
    'applied': bool}."""
    rows = store.conn.execute(
        """select e.listing_id, e.key, e.bundle_items, e.certainty, e.box_only, e.for_parts, e.wanted_ad, e.condition,
                  l.title, l.description, l.country
           from extractions e join listings l on l.id=e.listing_id where e.method='rules'""").fetchall()
    out = {"checked": 0, "changed": 0, "by_key": {}, "examples": [], "deals": [], "applied": apply}
    changed = []
    for r in rows:
        if not _comparable(r["key"], r["bundle_items"], r["certainty"], r["box_only"], r["for_parts"],
                           r["wanted_ad"], r["condition"]):
            continue
        out["checked"] += 1
        now = rules_extract(r["title"] or "", r["description"] or "", (r["country"] or "pl").lower(), root=root)
        if _still_comparable(now, r["key"]):
            continue
        changed.append((r["listing_id"], now))
        out["by_key"][r["key"]] = out["by_key"].get(r["key"], 0) + 1
        if len(out["examples"]) < examples:
            out["examples"].append({"listing_id": r["listing_id"], "title": (r["title"] or "")[:100],
                                    "was": r["key"], "now": now.key, "certainty": now.certainty,
                                    "bundle": now.bundle_items})
    out["changed"] = len(changed)
    if changed:
        ids = [lid for lid, _ in changed]
        q = ",".join("?" * len(ids))
        out["deals"] = [d[0] for d in store.conn.execute(f"select id from deals where listing_ref in ({q})", ids)]
    if apply and changed:
        from .scheduler import EVAL_SQL
        store.ensure_table(EVAL_SQL)
        for lid, now in changed:
            store.save_extraction(lid, now)
            store.conn.execute("delete from evaluations where listing_id=?", (lid,))
        store.conn.commit()
    return out


def cli(args) -> int:
    from . import runtime
    from .store import Store
    root = runtime.root_dir()
    path = runtime.db_path(root)
    if not path.is_file():
        print(f"no database yet at {path}")
        return 1
    lock = runtime.AgentLock(root, role="cli")
    if not lock.acquire():
        print(f"refused: the agent (or another dh command) holds {lock.path}; run `dh stop` first")
        return 1
    st = Store(path)
    try:
        res = recheck(st, apply=bool(args.apply), root=root)
    finally:
        st.close()
        lock.release()
    print(f"rules extractions that are comparables: {res['checked']}; no longer accepted by the current matcher: "
          f"{res['changed']}" + ("" if res["applied"] else " (dry run: add --apply to write)"))
    for k, n in sorted(res["by_key"].items(), key=lambda kv: -kv[1]):
        print(f"  {k}: {n}")
    for e in res["examples"]:
        print(f"  #{e['listing_id']} {e['title']!r}: {e['was']} -> {e['now']} ({e['certainty']}, {e['bundle']})")
    if res["deals"]:
        print(f"deals logged on these listings (not changed; review them): {res['deals']}")
    return 0
