"""Marketplace adapter base: Query, Adapter ABC, seller hashing, shared parsing helpers.

Adapters fetch ONLY through `Fetcher.get` (robots.txt, pacing, backoff). They never impersonate a browser.
Personal data: sellers are stored only as HMAC-SHA256(salt, f"{source}:{seller_id}"); names and phones
are never read into a Listing.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from ..models import Listing
from ..money import parse_price

ROOT = Path(__file__).resolve().parents[2]
SALT_REL = Path("config/local/secret_salt")


@dataclass
class Query:
    text: str
    category: str | None = None
    min_price_pln: Decimal | None = None
    max_price_pln: Decimal | None = None
    arm_id: str | None = None


class AdapterError(RuntimeError):
    """Source unavailable right now (blocked, disallowed by robots, transport error, unexpected status)."""


# ---------------------------------------------------------------- seller hash
_salt_cache: dict[Path, bytes] = {}


def get_salt(root: Path | None = None) -> bytes:
    """Local HMAC salt; created randomly (0600) on first use. `config/local/` is gitignored."""
    p = Path(os.environ.get("DH_SALT_PATH") or (Path(root or ROOT) / SALT_REL))
    if p not in _salt_cache:
        if p.is_file():
            salt = p.read_text().strip().encode()
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            salt = secrets.token_hex(32).encode()
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(salt.decode() + "\n")
        _salt_cache[p] = salt
    return _salt_cache[p]


def seller_hash(source: str, seller_id: str | int | None, salt: bytes | None = None) -> str | None:
    if seller_id is None or str(seller_id).strip() == "":
        return None
    return hmac.new(salt or get_salt(), f"{source}:{seller_id}".encode(), hashlib.sha256).hexdigest()


# ---------------------------------------------------------------- parsing helpers
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip()


def price_of(text: str | None, default_ccy: str) -> tuple[Decimal, str] | None:
    """parse_price, or None when the text has no positive amount ("do negocjacji", "0 zł", "VB", ...)."""
    r = parse_price(norm(text), default_ccy) if text else None
    if r is None or r[0] <= 0:
        return None
    return r


def years_to_days(n: int | float) -> int:
    return int(n * 365)


def age_days(since: datetime, now: datetime | None = None) -> int:
    now = now or now_utc()
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    return max(0, (now - since).days)


def local_dt(naive: datetime, tz: str) -> datetime:
    return naive.replace(tzinfo=ZoneInfo(tz)).astimezone(timezone.utc)


def relative_day(word: str, hh: int, mm: int, tz: str, today_words: tuple[str, ...], yday_words: tuple[str, ...],
                 now: datetime | None = None) -> datetime | None:
    now = (now or now_utc()).astimezone(ZoneInfo(tz))
    w = word.lower()
    if w in today_words:
        d = now
    elif w in yday_words:
        d = now - timedelta(days=1)
    else:
        return None
    return d.replace(hour=hh, minute=mm, second=0, microsecond=0).astimezone(timezone.utc)


def in_range(price: Decimal, ccy: str, q: Query) -> bool:
    """PLN bounds only apply to PLN listings (no FX here); other currencies pass through."""
    if ccy != "PLN":
        return True
    if q.min_price_pln is not None and price < q.min_price_pln:
        return False
    if q.max_price_pln is not None and price > q.max_price_pln:
        return False
    return True


# Product kinds, used by adapters that can only browse category pages (robots.txt forbids their search).
KIND_PATTERNS: list[tuple[str, str]] = [
    ("gpu", r"rtx|gtx|geforce|radeon|\brx ?\d{3,4}|\bgpu\b|graf|graphic|\bvga\b|karta gr|arc ?a\d"),
    ("laptop", r"macbook|laptop|notebook|thinkpad|ideapad|zenbook|vivobook|latitude"),
    ("cpu", r"ryzen|intel core|\bi[3579]-\d|\bcpu\b|procesor|prozessor|threadripper|xeon"),
    ("ram", r"\bddr[345]\b|\bram\b|pam[eě]ť|pami[eę][cć]|speicher"),
    ("disk", r"\bssd\b|\bhdd\b|nvme|dysk|disk|festplatte"),
    ("monitor", r"monitor|\blcd\b|\boled\b"),
    ("console", r"playstation|\bps[45]\b|xbox|konsol"),
    ("tablet", r"tablet|ipad"),
]


def kinds_for(q: Query, limit: int = 2) -> list[str]:
    text = f"{q.text} {q.category or ''}".lower()
    return [k for k, rx in KIND_PATTERNS if re.search(rx, text)][:limit]


def matches_query(listing: Listing, q: Query) -> bool:
    """Every query word (or the words glued together, "rtx4090") appears in title/description."""
    hay = f"{listing.title} {listing.description}".lower()
    flat = re.sub(r"[\s\-]", "", hay)
    words = [w for w in re.split(r"\s+", q.text.lower().strip()) if w]
    if not words:
        return True
    return all(w in hay or w in flat for w in words) or "".join(words) in flat


def abs_url(base: str, href: str) -> str:
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("http"):
        return href
    return base.rstrip("/") + "/" + href.lstrip("/")


class Adapter(ABC):
    name: str = ""
    domain: str = ""
    country: str = ""
    currency: str = ""
    #: substrings (lowercase) of a detail page that mean the listing is gone, in addition to 404/410
    gone_markers: tuple[str, ...] = ()
    #: the listing id must still appear in the final URL (sites that redirect dead ads elsewhere)
    id_in_final_url: bool = False

    @abstractmethod
    async def search(self, query: Query, fetcher) -> list[Listing]: ...

    @abstractmethod
    def parse_detail(self, listing: Listing, html: str) -> Listing:
        """Pure: fill description/photos/seller fields from a detail page body."""

    def detail_price(self, html: str) -> Decimal | None:
        """Pure: current price on a detail page, or None when it can't be read."""
        return None

    async def _get(self, fetcher, url: str, kind: str = "html"):
        res = await fetcher.get(url, kind=kind)
        if res.blocked:
            raise AdapterError(f"{self.name}: blocked ({url})")
        if res.disallowed:
            raise AdapterError(f"{self.name}: robots.txt disallows {url}")
        return res

    async def detail(self, listing: Listing, fetcher) -> Listing:
        res = await self._get(fetcher, listing.url)
        if res.status != 200:
            raise AdapterError(f"{self.name}: detail status {res.status} for {listing.url}")
        return self.parse_detail(listing, res.text)

    def is_gone_response(self, listing: Listing, res) -> bool:
        if res.status in (404, 410):
            return True
        if res.status == 0:
            return self.gone_on_transport_failure(res)
        low = res.text.lower()
        if any(m in low for m in self.gone_markers):
            return True
        return self.id_in_final_url and listing.source_id not in res.url

    def gone_on_transport_failure(self, res) -> bool:
        return False

    async def alive(self, listing: Listing, fetcher) -> tuple[bool, Decimal | None]:
        res = await self._get(fetcher, listing.url)
        if self.is_gone_response(listing, res):
            return False, None
        if res.status != 200:
            raise AdapterError(f"{self.name}: alive check status {res.status} for {listing.url}")
        return True, self.detail_price(res.text)

    def make(self, **kw) -> Listing:
        base = dict(source=self.name, country=self.country, currency=self.currency, description="", city=None,
                    lat=None, lon=None, seller_hash=None, seller_age_days=None, seller_ratings=None,
                    seller_score=None, ships=None, pickup_only=False, is_business=None, photos=[],
                    posted_at=None, seen_at=now_utc())
        base.update(kw)
        return Listing(**base)
