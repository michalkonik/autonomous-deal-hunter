"""Seed `comps` with Bazoš.cz ask history from the public kukas/bazos_dataset (spec §6.2 cold start).

Source: https://github.com/kukas/bazos_dataset (two years, 2022-05-01..2024-07-31, from the Bazoš RSS feed).
The PC section is a zip of CSVs: `title, description, link, date, category`, where the price is the tail of
the title after the last "-" or ":" (the dataset's own bazos_postprocess.py uses the same rule). A post-processed
CSV with a header (`title`, `price` or `price_string`, `link`, `datetime`) is accepted too.

Only GPU, RAM and SSD titles that the rules matcher keys with certainty ≥ 0.9 are kept; wanted ads, box-only,
for-parts and bundles never are. Prices are CZK, converted to PLN at the NBP rate of the posting date.
Rows go in as kind `ask_history` (weight 0.2 in valuation), channel `bazos_cz`, condition `used` unless the title
says otherwise, `sold_at` = posting time, `ref` = the listing link. Re-running is idempotent (by ref).

Usage (on the rig):
    python -m dh.seed bazos --db var/dh.db                       # download the PC section, then import
    python -m dh.seed bazos --db var/dh.db --file section_pc.csv.zip

Note: the dataset ends 2024-07-31, so valuation (30-day comps, 60-day trend) does not use these rows for a
current estimate; they are price history for back-tests and long-run sanity checks.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import io
import sys
import zipfile
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterator

import httpx

from .match import rules_extract
from .money import Fx, parse_price, q2
from .store import Store

BAZOS_PC_URL = "https://jirkabalhar.cz/bazos/section_pc.csv.zip"
CHANNEL = "bazos_cz"
KIND = "ask_history"
MAX_BYTES = 200 * 1024 * 1024
USER_AGENT = "DealHunter/0.1 (personal price research; +https://github.com/michalkonik)"
CATEGORIES = ("gpu", "ram", "ssd")

csv.field_size_limit(10 * 1024 * 1024)


def split_title_price(title: str) -> tuple[str, str | None]:
    """'RTX 3080 10GB - 12 000' -> ('RTX 3080 10GB', '12 000'). The separator nearest the end wins."""
    cut = max(title.rfind(" - "), title.rfind("-"), title.rfind(":"))
    if cut < 0:
        return title.strip(), None
    return title[:cut].strip(), title[cut + 1:].strip() or None


def _parse_date(s: str) -> datetime | None:
    s = s.strip()
    for fmt in ("%a, %d %b %Y %H:%M:%S %z",):
        try:
            return datetime.strptime(s, fmt).astimezone(timezone.utc)
        except ValueError:
            pass
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _rows_from_text(f: io.TextIOBase) -> Iterator[tuple[str, str | None, str, datetime]]:
    reader = csv.reader(f)
    header: list[str] | None = None
    for row in reader:
        if header is None and row and row[0].strip().lower() == "title":
            header = [c.strip().lower() for c in row]
            continue
        if header is not None:
            d = dict(zip(header, row))
            title = d.get("title", "")
            price = d.get("price_string") or d.get("price") or None
            link, when = d.get("link", ""), _parse_date(d.get("datetime") or d.get("date") or "")
        else:
            if len(row) != 5:
                continue
            raw_title, _desc, link, raw_date, _cat = row
            title, price = split_title_price(raw_title)
            when = _parse_date(raw_date)
        if title and link and when is not None:
            yield title, price, link, when


def iter_rows(path: Path) -> Iterator[tuple[str, str | None, str, datetime]]:
    """(title, price text, link, posted_at UTC) from a .csv or a .zip of CSVs."""
    path = Path(path)
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as z:
            for name in sorted(n for n in z.namelist() if n.endswith(".csv")):
                with z.open(name) as raw:
                    yield from _rows_from_text(io.TextIOWrapper(raw, encoding="utf-8", errors="replace", newline=""))
    else:
        with open(path, encoding="utf-8", errors="replace", newline="") as f:
            yield from _rows_from_text(f)


async def import_bazos(store: Store, path: Path, fx, *, categories: tuple[str, ...] = CATEGORIES,
                       min_certainty: float = 0.9) -> dict[str, int]:
    """Import matching rows as ask_history comps. `fx` needs `async rate(ccy, day) -> Decimal` (money.Fx)."""
    counts = {"rows": 0, "imported": 0, "duplicate": 0, "no_price": 0, "no_key": 0, "excluded": 0}
    rates: dict[tuple[str, date], Decimal] = {}
    for title, price_text, link, when in iter_rows(path):
        counts["rows"] += 1
        parsed = parse_price(price_text or "", "CZK")
        if parsed is None:
            counts["no_price"] += 1
            continue
        amount, ccy = parsed
        e = rules_extract(title, "", "cs")
        if e.wanted_ad or e.box_only or e.for_parts or e.bundle_items:
            counts["excluded"] += 1
            continue
        if e.key is None or e.category not in categories or e.certainty < min_certainty:
            counts["no_key"] += 1
            continue
        if store.conn.execute("select 1 from comps where kind=? and ref=?", (KIND, link)).fetchone():
            counts["duplicate"] += 1
            continue
        d = when.date()
        if ccy == "PLN":
            pln = q2(amount)
        else:
            if (ccy, d) not in rates:
                rates[(ccy, d)] = await fx.rate(ccy, d)
            pln = q2(amount * rates[(ccy, d)])
        condition = e.condition if e.condition in ("new", "like_new") else "used"
        store.add_comp(e.key, condition, CHANNEL, pln, when, KIND, link)
        counts["imported"] += 1
    return counts


async def download(url: str, dest: Path, max_bytes: int = MAX_BYTES) -> Path:
    """Stream `url` to `dest`; refuses files larger than `max_bytes`."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=60, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as c:
        async with c.stream("GET", url) as r:
            r.raise_for_status()
            size = int(r.headers.get("content-length") or 0)
            if size > max_bytes:
                raise RuntimeError(f"{url}: {size} bytes > limit {max_bytes}")
            n = 0
            with open(dest, "wb") as f:
                async for chunk in r.aiter_bytes():
                    n += len(chunk)
                    if n > max_bytes:
                        raise RuntimeError(f"{url}: more than {max_bytes} bytes")
                    f.write(chunk)
    return dest


async def _main(args: argparse.Namespace) -> int:
    if args.file:
        path = Path(args.file)
    else:
        try:
            path = await download(args.url, Path(args.cache))
        except (httpx.HTTPError, RuntimeError) as e:
            print(f"download failed: {args.url}: {type(e).__name__}: {e}", file=sys.stderr)
            return 2
    store = Store(args.db)
    try:
        counts = await import_bazos(store, path, Fx(store))
    finally:
        store.close()
    print(" ".join(f"{k}={v}" for k, v in counts.items()))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m dh.seed", description="Seed comps with ask history")
    sub = p.add_subparsers(dest="source", required=True)
    b = sub.add_parser("bazos", help="kukas/bazos_dataset PC section (CZ)")
    b.add_argument("--db", default="var/dh.db")
    b.add_argument("--file", help="local .csv or .zip instead of downloading")
    b.add_argument("--url", default=BAZOS_PC_URL)
    b.add_argument("--cache", default="var/seed/section_pc.csv.zip")
    args = p.parse_args(argv)
    return asyncio.run(_main(args))


if __name__ == "__main__":
    sys.exit(main())
