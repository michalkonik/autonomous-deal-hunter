"""Task 25: photo pHash reuse check (images are generated in memory; no network)."""
from __future__ import annotations

import asyncio
import io
import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from PIL import Image

from dh import photos
from dh.fetch import FetchResult
from dh.models import Listing
from dh.store import Store

NOW = datetime(2026, 10, 29, 12, tzinfo=timezone.utc)
run = asyncio.run


def img(seed: int, size=256) -> Image.Image:
    rnd = random.Random(seed)
    cells = [[rnd.randrange(256) for _ in range(8)] for _ in range(8)]
    im = Image.new("L", (size, size))
    px = im.load()
    for y in range(size):
        for x in range(size):
            px[x, y] = cells[y * 8 // size][x * 8 // size]
    return im.convert("RGB")


def jpeg(im: Image.Image, q=95, size=None) -> bytes:
    if size:
        im = im.resize((size, size))
    b = io.BytesIO()
    im.save(b, "JPEG", quality=q)
    return b.getvalue()


class FakeFetcher:
    def __init__(self, bodies: dict[str, bytes], blocked=False):
        self.bodies, self.urls, self.blocked = bodies, [], blocked

    async def get_bytes(self, url, **kw):
        self.urls.append(url)
        if self.blocked:
            return FetchResult(0, "", url, blocked=True)
        body = self.bodies.get(url)
        return FetchResult(200 if body else 404, "", url, content=body or b"")


def lst(sid, seller, urls):
    return Listing("olx", sid, f"https://x/{sid}", "RTX 4090", "", Decimal("3000"), "PLN", "PL", None, None, None, seller,
                   None, None, None, None, False, None, list(urls), None, NOW)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


def test_reuse_across_sellers_detected_and_other_photo_not(store):
    a, b = img(1), img(2)
    f = FakeFetcher({"u/a1": jpeg(a), "u/a2": jpeg(a, q=50, size=200), "u/b": jpeg(b)})
    assert run(photos.check(lst("1", "sellerA", ["u/a1"]), f, store, now=NOW)) == []
    # the same picture re-encoded and resized under another seller: reuse
    assert run(photos.check(lst("2", "sellerB", ["u/a2"]), f, store, now=NOW)) == ["photo_reuse"]
    # a different picture: clean
    assert run(photos.check(lst("3", "sellerC", ["u/b"]), f, store, now=NOW)) == []
    assert photos.hamming(photos.phash_of(jpeg(a)), photos.phash_of(jpeg(b))) > photos.HAMMING_MAX


def test_same_seller_relisting_is_not_reuse(store):
    a = img(5)
    f = FakeFetcher({"u/1": jpeg(a), "u/2": jpeg(a)})
    run(photos.check(lst("1", "sellerA", ["u/1"]), f, store, now=NOW))
    assert run(photos.check(lst("2", "sellerA", ["u/2"]), f, store, now=NOW)) == []


def test_old_hashes_do_not_count_and_purge(store):
    a = img(7)
    f = FakeFetcher({"u/1": jpeg(a), "u/2": jpeg(a)})
    run(photos.check(lst("1", "sellerA", ["u/1"]), f, store, now=NOW - timedelta(days=91)))
    assert run(photos.check(lst("2", "sellerB", ["u/2"]), f, store, now=NOW)) == []
    assert photos.purge(store, now=NOW) == 1
    assert store.conn.execute("select count(*) from photo_hashes").fetchone()[0] == 1


def test_at_most_four_photos_and_hashes_only_stored(store):
    f = FakeFetcher({f"u/{i}": jpeg(img(10 + i)) for i in range(6)})
    run(photos.check(lst("1", "sellerA", [f"u/{i}" for i in range(6)]), f, store, now=NOW))
    assert f.urls == ["u/0", "u/1", "u/2", "u/3"]
    rows = store.conn.execute("select * from photo_hashes").fetchall()
    assert len(rows) == 4 and all(len(r["phash"]) == 16 and r["seller_hash"] == "sellerA" for r in rows)
    assert {c[1] for c in store.conn.execute("pragma table_info(photo_hashes)")} == {
        "id", "listing_id", "seller_hash", "phash", "seen_at"}


def test_second_check_does_not_download_again_but_sees_new_reuse(store):
    a = img(20)
    f = FakeFetcher({"u/1": jpeg(a), "u/2": jpeg(a)})
    first = lst("1", "sellerA", ["u/1"])
    assert run(photos.check(first, f, store, now=NOW)) == []
    run(photos.check(lst("2", "sellerB", ["u/2"]), f, store, now=NOW))
    n = len(f.urls)
    assert run(photos.check(first, f, store, now=NOW)) == ["photo_reuse"]  # re-evaluated from stored hashes
    assert len(f.urls) == n


def test_blocked_flat_and_garbage_images_are_harmless(store):
    flat = io.BytesIO()
    Image.new("RGB", (200, 200), "white").save(flat, "PNG")
    f = FakeFetcher({"u/flat": flat.getvalue(), "u/junk": b"not an image", "u/tiny": jpeg(img(3), size=32)})
    assert run(photos.check(lst("1", "sellerA", ["u/flat", "u/junk", "u/tiny", "u/gone"]), f, store, now=NOW)) == []
    assert store.conn.execute("select count(*) from photo_hashes").fetchone()[0] == 0
    fb = FakeFetcher({}, blocked=True)
    assert run(photos.check(lst("2", "sellerB", ["u/1", "u/2", "u/3"]), fb, store, now=NOW)) == []
    assert fb.urls == ["u/1"]  # stops at the first block


def test_no_seller_hash_never_flags(store):
    a = img(30)
    f = FakeFetcher({"u/1": jpeg(a), "u/2": jpeg(a)})
    run(photos.check(lst("1", "sellerA", ["u/1"]), f, store, now=NOW))
    assert run(photos.check(lst("2", None, ["u/2"]), f, store, now=NOW)) == []
