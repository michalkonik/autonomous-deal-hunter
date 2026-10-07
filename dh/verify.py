"""Live verifier: is the listing still up at the same price, does the stored net recompute, screenshots.

No circumvention: pages are fetched only through the polite `Fetcher` (robots, pacing, backoff) via the
adapter's `alive()`. Screenshots use headless system Chrome only for URLs the fetcher's robots decision
allows, on domains that are not blocked, and a challenge/blocked page is discarded, never worked around.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

from . import economics
from .cards import inputs_from_json
from .fetch import is_challenge
from .models import Listing
from .sources.base import AdapterError

CHROME = "/usr/bin/google-chrome"
VIEWPORT = {"width": 1280, "height": 2000}
TIMEOUT_MS = 30_000


@dataclass
class VerifyResult:
    ok: bool
    alive: bool
    price_now: Decimal | None
    net_now_cons_pln: Decimal | None
    net_stored_cons_pln: Decimal
    diffs: list[str] = field(default_factory=list)


async def recheck(target, adapter, fetcher) -> tuple[bool, Decimal | None]:
    """(alive, current price) of a listing (or an object with `.listing`). An adapter failure (blocked,
    robots, transport) returns (False, None): not verified, never assumed alive."""
    listing = getattr(target, "listing", target)
    try:
        return await adapter.alive(listing, fetcher)
    except AdapterError:
        return False, None


def load_listing(store, listing_ref: int) -> Listing | None:
    r = store.conn.execute("select * from listings where id=?", (listing_ref,)).fetchone()
    if r is None:
        return None
    ts = lambda v: datetime.fromisoformat(v) if v else None  # noqa: E731
    flag = lambda v: None if v is None else bool(v)  # noqa: E731
    return Listing(
        source=r["source"], source_id=r["source_id"], url=r["url"], title=r["title"], description=r["description"] or "",
        price=Decimal(r["price"]), currency=r["currency"], country=r["country"], city=r["city"], lat=r["lat"],
        lon=r["lon"], seller_hash=r["seller_hash"], seller_age_days=r["seller_age_days"],
        seller_ratings=r["seller_ratings"], seller_score=r["seller_score"], ships=flag(r["ships"]),
        pickup_only=bool(r["pickup_only"]), is_business=flag(r["is_business"]), photos=json.loads(r["photos"] or "[]"),
        posted_at=ts(r["posted_at"]), seen_at=ts(r["last_seen_at"]) or datetime.now(timezone.utc),
        raw_path=r["raw_path"], contact_in_text=bool(r["contact_in_text"]) if "contact_in_text" in r.keys() else False)


async def verify_deal(store, deal_id: int, cfg, fx, customs, adapters, fetcher) -> VerifyResult:
    """Recompute the net from the stored evidence with the same `economics.net`, and re-check the listing.
    `ok` only when the listing is alive, the price is unchanged and the conservative net is identical."""
    deal = store.get_deal(deal_id)
    if deal is None:
        raise KeyError(f"no deal {deal_id}")
    stored = deal.cons_net_pln
    diffs: list[str] = []
    ev = json.loads(deal.evidence_json or "{}")
    if "net_inputs" not in ev:
        return VerifyResult(False, False, None, None, stored, ["no stored net inputs in the evidence"])
    inp = inputs_from_json(ev["net_inputs"])
    replay = await economics.net(inp, cfg, fx, customs)
    if replay.cons_net_pln != stored:
        diffs.append(f"stored conservative net {stored} PLN does not recompute from the evidence: {replay.cons_net_pln} PLN")
    listing = load_listing(store, deal.listing_ref)
    adapter = adapters.get(listing.source) if listing else None
    if listing is None or adapter is None:
        diffs.append("no listing or adapter to re-check")
        return VerifyResult(False, False, None, replay.cons_net_pln, stored, diffs)
    alive, price_now = await recheck(listing, adapter, fetcher)
    net_now = replay.cons_net_pln
    if not alive:
        diffs.append("listing not confirmed alive")
    elif price_now is None:
        diffs.append("price not confirmed")  # high confidence needs the same price re-seen live
    elif price_now != inp.buy_price:
        diffs.append(f"price changed: {inp.buy_price} -> {price_now} {inp.buy_ccy}")
        from dataclasses import replace
        net_now = (await economics.net(replace(inp, buy_price=price_now), cfg, fx, customs)).cons_net_pln
        diffs.append(f"conservative net now {net_now} PLN vs stored {stored} PLN")
    return VerifyResult(not diffs, alive, price_now, net_now, stored, diffs)


def record_verification(store, deal_id: int, ok: bool, screenshot: Path | str | None = None) -> None:
    """Stamp `verified_at` (only when ok) and the verification screenshot path into the evidence."""
    deal = store.get_deal(deal_id)
    ev = json.loads(deal.evidence_json or "{}")
    if screenshot is not None:
        ev.setdefault("screenshots", {})["verification"] = str(screenshot)
    store.conn.execute("update deals set evidence_json=?, verified_at=coalesce(?, verified_at) where id=?",
                       (json.dumps(ev), datetime.now(timezone.utc).isoformat() if ok else None, deal_id))
    store.conn.commit()


async def _shoot(url: str, path: Path, fetcher) -> bool:
    """Headless system Chrome, 1280x2000, 30 s. False (nothing saved) on a bad status, a challenge page, or a
    final URL robots forbids; a block is reported to the fetcher's backoff bookkeeping."""
    from playwright.async_api import async_playwright
    domain = urlsplit(url).netloc.lower()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path=CHROME, headless=True)
        try:
            page = await browser.new_page(viewport=VIEWPORT)
            page.set_default_timeout(TIMEOUT_MS)
            resp = await page.goto(url, timeout=TIMEOUT_MS, wait_until="load")
            status = resp.status if resp else 0
            final = page.url
            if status in (403, 429) or is_challenge(status, await page.content()):
                fetcher.report_block(urlsplit(final).netloc.lower() or domain, f"screenshot got status {status}")
                return False
            if status == 0 or status >= 400:
                return False
            if final != url and not await fetcher.allowed(final):  # redirected somewhere robots forbids
                return False
            path.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(path))
            return path.is_file() and path.stat().st_size > 0
        finally:
            await browser.close()


async def screenshot(url: str, path: Path | str, fetcher) -> bool:
    """Evidence screenshot. Only for URLs `fetcher.allowed()` permits (robots) on a domain that is neither
    blocked nor disabled, paced like any other request to that domain. Never raises; failure returns False."""
    path = Path(path)
    try:
        domain = urlsplit(url).netloc.lower()
        if fetcher.blocked(domain) or fetcher.disabled(domain) or not await fetcher.allowed(url):
            return False
        if not await fetcher.pace(domain):
            return False
        ok = bool(await _shoot(url, path, fetcher))
        if not ok and path.exists():
            path.unlink(missing_ok=True)
        return ok
    except Exception:
        return False
