"""Marketplace adapter base: Query, Adapter ABC, seller hashing, shared parsing helpers.

Adapters fetch ONLY through `Fetcher.get` (robots.txt, pacing, backoff). They never impersonate a browser.
Personal data: sellers are stored only as HMAC-SHA256(salt, f"{source}:{seller_id}"); names and phones
are never read into a Listing.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import unicodedata
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from ..models import Listing
from ..money import parse_price
from ..risk import PHONE_RE

log = logging.getLogger(__name__)

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
            tmp = p.with_name(f".{p.name}.{os.getpid()}.{secrets.token_hex(4)}")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(salt.decode() + "\n")
            try:
                os.link(tmp, p)  # atomic create-if-absent: a concurrent first use cannot overwrite the salt
            except FileExistsError:
                pass
            finally:
                os.unlink(tmp)
            salt = p.read_text().strip().encode()
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


_warned_ccy: set[str] = set()


def filter_query(listings: list[Listing], q: Query, to_pln: Callable[[Decimal, str], Decimal | None] | None = None,
                 *, text: bool = False) -> list[Listing]:
    """The single place where a Query narrows adapter results.

    Price bounds are in PLN. `to_pln(amount, ccy)` is a sync converter backed by cached NBP rates; PLN listings
    need none. If a non-PLN listing cannot be converted (no converter or no cached rate) it is KEPT and the gap is
    logged once per currency. `text=True` additionally requires every query word in title/description (for
    category-only sources whose search form robots.txt forbids).
    """
    out = []
    bounded = q.min_price_pln is not None or q.max_price_pln is not None
    for l in listings:
        if text and not matches_query(l, q):
            continue
        if bounded:
            pln = l.price if l.currency == "PLN" else (to_pln(l.price, l.currency) if to_pln else None)
            if pln is None:
                if l.currency not in _warned_ccy:
                    _warned_ccy.add(l.currency)
                    log.warning("no PLN rate for %s: price bounds not applied to %s listings", l.currency, l.currency)
            elif (q.min_price_pln is not None and pln < q.min_price_pln) or \
                    (q.max_price_pln is not None and pln > q.max_price_pln):
                continue
        out.append(l)
    return out


def product_ld(html: str) -> dict:
    """First schema.org Product JSON-LD object on a page ({} if none)."""
    for m in re.finditer(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S):
        try:
            d = json.loads(m.group(1))
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("@type") == "Product":
            return d
    return {}


def _fold(text: str) -> str:
    """Length-preserving lowercase/accent fold so risk's phone regex (written for normalised text) fits."""
    out = []
    for c in text:
        b = "".join(x for x in unicodedata.normalize("NFKD", c.lower().replace("ł", "l")) if not unicodedata.combining(x))
        out.append(b if len(b) == 1 else c)
    return "".join(out)


_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_TEL_LINK = re.compile(r"\btel:\S+", re.I)
REDACTED = "[redacted]"


def redact(text: str | None) -> tuple[str, bool]:
    """Replace phone numbers (risk._PHONE), e-mail addresses and tel: links by "[redacted]".

    Returns (clean text, whether anything was redacted). Callers record the boolean in `Listing.contact_in_text`
    because risk's off-platform detection can no longer see the number once it is gone.
    """
    if not text:
        return text or "", False
    spans = [m.span() for m in PHONE_RE.finditer(_fold(text))]
    spans += [m.span() for rx in (_EMAIL, _TEL_LINK) for m in rx.finditer(text)]
    if not spans:
        return text, False
    spans.sort()
    merged = [list(spans[0])]
    for a, b in spans[1:]:
        if a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    out, pos = [], 0
    for a, b in merged:
        out += [text[pos:a], REDACTED]
        pos = b
    out.append(text[pos:])
    return "".join(out), True


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


class Adapter(ABC):  # noqa: B024 (no abstract methods: see below)
    name: str = ""
    domain: str = ""
    country: str = ""
    currency: str = ""
    #: substrings (lowercase) of a detail page that mean the listing is gone, in addition to 404/410
    gone_markers: tuple[str, ...] = ()
    #: the listing id must still appear in the final URL (sites that redirect dead ads elsewhere)
    id_in_final_url: bool = False
    #: the final URL path must equal the listing URL path (ids that are not part of the URL)
    path_must_match: bool = False
    #: robots.txt forbids the site's search: only category pages are read and filtered by query words
    category_only: bool = False
    #: optional sync (amount, ccy) -> PLN|None backed by cached NBP rates; used by filter_query
    to_pln: Callable[[Decimal, str], Decimal | None] | None = None

    # ---- to implement
    # HTML adapters implement page_urls/parse_page/_parse_detail and inherit search/parse_detail/alive.
    # API/RSS adapters (eBay, RSS, retail) override search/parse_detail themselves.
    def page_urls(self, q: Query) -> list[str]:
        """URLs to fetch for a search (one for real search sites, several category pages otherwise)."""
        raise NotImplementedError

    def parse_page(self, text: str) -> list[Listing]:
        """Pure: search/category page body -> listings with price > 0 (unfiltered)."""
        raise NotImplementedError

    def _parse_detail(self, listing: Listing, html: str) -> Listing:
        """Pure: fill description/photos/seller fields from a detail page body."""
        raise NotImplementedError

    def detail_price(self, html: str) -> Decimal | None:
        """Pure: current price on a detail page, or None when it can't be read."""
        return None

    # ---- shared behaviour
    async def _get(self, fetcher, url: str, kind: str = "html"):
        res = await fetcher.get(url, kind=kind)
        if res.blocked:
            raise AdapterError(f"{self.name}: blocked ({url})")
        if res.disallowed:
            raise AdapterError(f"{self.name}: robots.txt disallows {url}")
        return res

    async def search(self, query: Query, fetcher, to_pln=None) -> list[Listing]:
        urls = self.page_urls(query)
        found: dict[str, Listing] = {}
        failed, last = 0, 0
        for url in urls:
            res = await self._get(fetcher, url)
            if res.status != 200:
                failed, last = failed + 1, res.status
                continue
            for l in self.parse_page(res.text):
                found.setdefault(l.source_id, l)
        if failed == len(urls):
            raise AdapterError(f"{self.name}: search status {last}")
        return filter_query(list(found.values()), query, to_pln or self.to_pln, text=self.category_only)

    def parse_detail(self, listing: Listing, html: str) -> Listing:
        return self.finish(self._parse_detail(listing, html))

    def finish(self, l: Listing) -> Listing:
        """Redact contact data from free text before it is stored; remember that we did."""
        l.title, t = redact(l.title)
        l.description, d = redact(l.description)
        l.contact_in_text = l.contact_in_text or t or d
        return l

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
        if self.id_in_final_url and listing.source_id not in res.url:
            return True
        if self.path_must_match and urlsplit(res.url).path != urlsplit(listing.url).path:
            return True
        return False

    def gone_on_transport_failure(self, res) -> bool:
        return False

    async def alive(self, listing: Listing, fetcher) -> tuple[bool, Decimal | None]:
        """Is the ad still on sale, and at what price?

        Contract (the verifier relies on it):
        - (False, None): the ad is gone (404/410, a known "gone" marker, or redirected away from its URL).
        - (True, price): the page loaded and the CURRENT price was read. Only this means "confirmed live".
        - AdapterError: blocked, disallowed by robots.txt, transport/5xx failure, or the page loaded with no gone
          marker but no readable price ("unverifiable"). The verifier must treat that as NOT confirmed.
        """
        res = await self._get(fetcher, listing.url)
        if self.is_gone_response(listing, res):
            return False, None
        if res.status != 200:
            raise AdapterError(f"{self.name}: alive check status {res.status} for {listing.url}")
        price = self.detail_price(res.text)
        if price is None:
            raise AdapterError(f"{self.name}: unverifiable (no readable price) for {listing.url}")
        return True, price

    def make(self, **kw) -> Listing:
        base = dict(source=self.name, country=self.country, currency=self.currency, description="", city=None,
                    lat=None, lon=None, seller_hash=None, seller_age_days=None, seller_ratings=None,
                    seller_score=None, ships=None, pickup_only=False, is_business=None, photos=[],
                    posted_at=None, seen_at=now_utc())
        base.update(kw)
        return self.finish(Listing(**base))
