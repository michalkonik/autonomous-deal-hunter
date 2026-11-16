"""Photo reuse check (scam signal): the same picture on listings of different sellers.

`await check(listing, fetcher, store)` downloads at most 4 photos of a CANDIDATE listing (the caller only passes tier <= 3
candidates) through `Fetcher.get_bytes` (robots.txt, pacing and block backoff apply), computes a perceptual hash
(imagehash pHash, 64 bit), stores the hash in `photo_hashes(listing_id, seller_hash, phash, seen_at)` and returns
["photo_reuse"] when a photo is within Hamming distance 6 of a photo of a DIFFERENT seller hash seen in the last 90 days.

Only hashes are stored: the image bytes are dropped right after hashing. `purge(store)` removes hashes older than 90 days.
A listing that already has hashes is not downloaded again (its flag is recomputed from the stored hashes against the
current table). Flat or tiny images (placeholders, blanks: grey-level stddev < 3) are ignored, they would match everything.
Feed the flag to `risk.assess(..., stock_photo="photo_reuse" in flags)`.
"""
from __future__ import annotations

import io
import logging
from datetime import datetime, timedelta, timezone

import imagehash
from PIL import Image, ImageStat, UnidentifiedImageError

from .models import Listing

log = logging.getLogger(__name__)

MAX_PHOTOS = 4
HAMMING_MAX = 6
WINDOW_DAYS = 90
MIN_SIDE = 64
MIN_STDDEV = 3.0
FLAG = "photo_reuse"
_FLAT = {0, (1 << 64) - 1}

TABLE = """create table if not exists photo_hashes (
  id integer primary key, listing_id integer not null, seller_hash text, phash text not null, seen_at text not null,
  unique (listing_id, phash))"""
INDEX = "create index if not exists idx_photo_hashes on photo_hashes(seen_at)"


def ensure_schema(store) -> None:
    store.ensure_table(TABLE)
    store.ensure_table(INDEX)


def phash_of(data: bytes) -> int | None:
    """64-bit pHash of an image, None when it is not a decodable image, too small or flat."""
    try:
        with Image.open(io.BytesIO(data)) as im:
            if min(im.size) < MIN_SIDE:
                return None
            g = im.convert("L")
            if ImageStat.Stat(g).stddev[0] < MIN_STDDEV:
                return None  # flat image: placeholder, logo or blank
            h = int(str(imagehash.phash(g)), 16)
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        return None
    return None if h in _FLAT else h


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def _listing_id(store, listing: Listing) -> int:
    r = store.conn.execute("select id from listings where source=? and source_id=?",
                           (listing.source, listing.source_id)).fetchone()
    return r["id"] if r else store.upsert_listing(listing)


def _reused(store, listing_id: int, seller: str | None, hashes: list[int], since: str) -> bool:
    if seller is None or not hashes:
        return False  # without a seller hash "a different seller" cannot be decided
    rows = store.conn.execute(
        "select phash from photo_hashes where seen_at >= ? and listing_id != ? and seller_hash is not null "
        "and seller_hash != ?", (since, listing_id, seller)).fetchall()
    others = [int(r["phash"], 16) for r in rows]
    return any(hamming(h, o) <= HAMMING_MAX for h in hashes for o in others)


async def check(listing: Listing, fetcher, store, *, now: datetime | None = None) -> list[str]:
    """Flags for `listing`: [] or ["photo_reuse"]. Never raises on network trouble (the fetcher records health)."""
    ensure_schema(store)
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=WINDOW_DAYS)).isoformat()
    lid = _listing_id(store, listing)
    seller = listing.seller_hash
    have = [int(r["phash"], 16) for r in store.conn.execute("select phash from photo_hashes where listing_id=?", (lid,))]
    if not have:
        for url in listing.photos[:MAX_PHOTOS]:
            res = await fetcher.get_bytes(url)
            if res.blocked or res.disallowed:
                break  # the domain is closed to us right now: do not hammer the other photos
            if res.status != 200 or not res.content:
                continue
            h = phash_of(res.content)
            del res  # image bytes are never kept
            if h is not None:
                have.append(h)
        for h in dict.fromkeys(have):
            store.conn.execute("insert or ignore into photo_hashes (listing_id, seller_hash, phash, seen_at) values (?,?,?,?)",
                               (lid, seller, f"{h:016x}", now.isoformat()))
        store.conn.commit()
    return [FLAG] if _reused(store, lid, seller, have, since) else []


def purge(store, now: datetime | None = None, days: int = WINDOW_DAYS) -> int:
    """Delete hashes older than `days` (default 90; sources.retention.photos_days); returns the rows removed."""
    ensure_schema(store)
    cutoff = ((now or datetime.now(timezone.utc)) - timedelta(days=days)).isoformat()
    n = store.conn.execute("delete from photo_hashes where seen_at < ?", (cutoff,)).rowcount
    store.conn.commit()
    return n
