"""`dh probe --source X --query Q`: run one adapter search through a real, polite Fetcher and print the result.

Used for live smoke tests. Writes nothing to the real DB: it uses a throw-away store and snapshot dir.
"""
from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

from .config import load
from .fetch import Fetcher
from .sources import DROPPED, AdapterError, Query, registry
from .sources.base import ROOT
from .store import Store


def add_arguments(sp) -> None:
    sp.add_argument("--source", required=True, help="adapter name (see --list)")
    sp.add_argument("--query", default="rtx 4090")
    sp.add_argument("--limit", type=int, default=15, help="max listings to print")
    sp.add_argument("--detail", action="store_true", help="also fetch the detail page of the first listing")
    sp.add_argument("--alive", action="store_true", help="also run the alive check on the first listing")


def _health_lines(store: Store) -> list[str]:
    return [f"{r['kind']}: {r['detail']}" for r in store.conn.execute("select kind, detail from health order by rowid")]


async def probe(source: str, query: str, limit: int = 15, detail: bool = False, alive: bool = False,
                root: Path | None = None, out=None) -> int:
    out = out or sys.stdout
    root = Path(root or ROOT)
    cfg = load(root)
    reg = registry(cfg, root)
    if source not in reg:
        why = DROPPED.get(source)
        print(f"unknown or disabled source {source!r}" + (f" (dropped: {why})" if why else "")
              + f"; available: {', '.join(sorted(reg))}", file=out)
        return 2
    ad = reg[source]
    tmp = Path(tempfile.mkdtemp(prefix="dh-probe-"))
    store = Store(tmp / "probe.db")
    fetcher = Fetcher(cfg, store, tmp)
    try:
        try:
            listings = await ad.search(Query(text=query, category=None, min_price_pln=None, max_price_pln=None,
                                             arm_id="probe"), fetcher)
        except AdapterError as e:
            print(f"{source}: 0 listings; reason: {e}", file=out)
            return 1
        print(f"{source}: {len(listings)} listing(s) for {query!r}", file=out)
        for l in listings[:limit]:
            print(f"  {l.title[:70]} | {l.price} {l.currency} | {l.url}", file=out)
        if not listings:
            hl = _health_lines(store)
            print("  reason: " + ("; ".join(hl) if hl else "the source returned no matching ads"), file=out)
            return 0
        first = listings[0]
        rc = 0
        if detail:
            try:
                d = await ad.detail(first, fetcher)
                print(f"  detail: desc={len(d.description)} chars, photos={len(d.photos)}, seller_hash="
                      f"{(d.seller_hash or '-')[:12]}, age_days={d.seller_age_days}, ships={d.ships}, city={d.city},"
                      f" contact_redacted={d.contact_in_text}", file=out)
            except AdapterError as e:
                print(f"  detail: failed: {e}", file=out)
                rc = 1
        if alive:
            try:
                print(f"  alive({first.source_id}): {await ad.alive(first, fetcher)}", file=out)
            except AdapterError as e:
                print(f"  alive({first.source_id}): not confirmed: {e}", file=out)
                rc = 1
        return rc
    finally:
        await fetcher.aclose()
        store.close()
        shutil.rmtree(tmp, ignore_errors=True)


def run(args) -> int:
    return asyncio.run(probe(args.source, args.query, args.limit, args.detail, args.alive))
