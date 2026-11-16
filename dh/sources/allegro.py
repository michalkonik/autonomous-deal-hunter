"""Allegro.pl public offers, official REST API only. www.allegro.pl HTML is never fetched.

Allegro blocks polite page fetches (DataDome). Search is `GET https://api.allegro.pl/offers/listing`
with a client-credentials token. Every request sends the User-Agent registered for this application.
A wrong or missing User-Agent can get the API key blocked.

Access to `/offers/listing` is limited to applications Allegro has verified. Since 2024 Allegro has not
been granting that access to new applications: the call returns HTTP 403 `VerificationRequired`. That is
recorded once as `not_verified` and the adapter stops for the session. It is not a reason to scrape the site.

Saved-search alert mail stays a separate path (`mailbox`). Allegro Lokalnie is a different site.
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
from decimal import Decimal
from typing import Awaitable, Callable
from urllib.parse import urlencode, urlsplit

import httpx

from ..models import Listing
from .base import Adapter, AdapterError, Query, filter_query, seller_hash
from .ebay import _cached_bw_secret

#: Validated on the Allegro developer User-Agent page for the deal-hunter application.
#: Do not "correct" the URL: Allegro whitelists this exact header.
DEFAULT_UA = "deal-hunter/1.0 (+https://github.com/michalkonik/family-finance-planner)"
ACCEPT = "application/vnd.allegro.public.v1+json"
TOKEN_URL = "https://allegro.pl/auth/oauth/token"
LISTING_URL = "https://api.allegro.pl/offers/listing"
ALLOWED_HOSTS = frozenset({"allegro.pl", "api.allegro.pl"})
NOT_VERIFIED = ("Allegro has not granted this application access to public offer search "
                "(GET /offers/listing VerificationRequired). The website is not fetched.")
NO_KEYS = "no Allegro keys (bw-secret allegro#client_id / allegro#client_secret): adapter disabled"

Request = Callable[..., Awaitable[tuple]]


async def _httpx_request(method: str, url: str, headers: dict, data: str | None, timeout: float) -> tuple:
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as c:
        r = await c.request(method, url, headers=headers, content=data)
        return r.status_code, r.text, {k.lower(): v for k, v in r.headers.items()}


class AllegroClient:
    """Paced client for the two official Allegro hosts. Refuses the website."""

    def __init__(self, request: Request | None = None, min_interval_s: float = 1.0, backoff_start_s: float = 60,
                 backoff_cap_s: float = 3600, disable_after: int = 3, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable] = asyncio.sleep, on_event: Callable[[str, str], None] | None = None,
                 user_agent: str = DEFAULT_UA):
        self._req = request or _httpx_request
        self.min_interval, self.b0, self.cap, self.disable_after = min_interval_s, backoff_start_s, backoff_cap_s, disable_after
        self.clock, self.sleep, self.on_event = clock, sleep, on_event or (lambda k, d: None)
        self.user_agent = user_agent
        self._last = -1e18
        self._backoff_until = 0.0
        self._next_backoff = backoff_start_s
        self.refusals = 0
        self.disabled = False
        self._lock = asyncio.Lock()

    async def call(self, method: str, url: str, headers: dict | None = None, data: str | None = None) -> tuple[int, str]:
        host = (urlsplit(url).hostname or "").lower()
        if host not in ALLOWED_HOSTS:
            raise AdapterError(f"Allegro client refuses host {host!r}: the website is not fetched")
        if self.disabled:
            raise AdapterError("allegro api disabled for this session after repeated refusals")
        async with self._lock:
            now = self.clock()
            if now < self._backoff_until:
                raise AdapterError(f"api backing off for {self._backoff_until - now:.0f}s more")
            wait = self._last + self.min_interval - now
            if wait > 0:
                await self.sleep(wait)
            h = {"User-Agent": self.user_agent, **(headers or {})}
            if "Accept" not in h:
                h["Accept"] = "application/json"
            try:
                res = await self._req(method, url, h, data, 30)
                status, text = res[0], res[1]
                rhead = res[2] if len(res) > 2 else {}
            except httpx.HTTPError as e:
                self._last = self.clock()
                raise AdapterError(f"api transport error: {type(e).__name__}") from e
            self._last = self.clock()
            if status == 429:
                self.refusals += 1
                wait_s = self.b0
                ra = str(rhead.get("retry-after", "")).strip()
                if ra.isdigit():
                    wait_s = min(max(float(ra), self.b0), self.cap)
                self._backoff_until = self._last + wait_s
                self.on_event("blocked", f"api status 429; backoff {wait_s:.0f}s; consecutive={self.refusals}")
                self._next_backoff = min(self._next_backoff * 2, self.cap)
                if self.refusals >= self.disable_after:
                    self.disabled = True
                    self.on_event("disabled", "allegro api disabled for the session after repeated refusals")
                raise AdapterError(f"api refused ({status})")
            if status not in (429,):
                if status < 500:
                    self.refusals, self._next_backoff = 0, self.b0
            return status, text


def verification_required(status: int, text: str) -> bool:
    if status != 403:
        return False
    try:
        j = json.loads(text)
    except ValueError:
        j = {}
    for err in j.get("errors") or []:
        if isinstance(err, dict) and err.get("code") == "VerificationRequired":
            return True
    return "verified application" in (text or "").lower()


class Allegro(Adapter):
    name = "allegro"
    domain = "api.allegro.pl"
    country = "PL"
    currency = "PLN"

    def __init__(self, secret_fn: Callable[[str], str | None] | None = None, client: AllegroClient | None = None,
                 limit: int = 60, user_agent: str = DEFAULT_UA):
        self._secret_fn = secret_fn
        self._keys: tuple[str, str] | None | bool = False
        self.user_agent = user_agent
        self.client = client or AllegroClient(user_agent=user_agent)
        self.client.user_agent = user_agent
        self.limit = max(1, min(int(limit), 60))
        self._token: tuple[str, float] | None = None
        self.disabled_reason: str | None = None
        self._health: set[str] = set()

    def _resolve(self) -> tuple[str, str] | None:
        if self._keys is False:
            fn = self._secret_fn or _cached_bw_secret
            cid, sec = fn("allegro#client_id"), fn("allegro#client_secret")
            self._keys = (cid, sec) if cid and sec else None
        return self._keys or None

    @property
    def enabled(self) -> bool:
        return self._resolve() is not None and self.disabled_reason is None

    def _disable_kind(self) -> str:
        text = self.disabled_reason or ""
        if "VerificationRequired" in text or "not granted" in text:
            return "not_verified"
        return "bad_credentials"

    def note_disabled(self, store) -> None:
        if self.disabled_reason:
            self._health_once(store, self._disable_kind(), self.disabled_reason)
            return
        if self._resolve() is None:
            self._health_once(store, "no_keys", NO_KEYS)

    def _health_once(self, store, kind: str, detail: str) -> None:
        if store is None or kind in self._health:
            return
        if store.conn.execute("select 1 from health where source='allegro' and kind=?", (kind,)).fetchone():
            self._health.add(kind)
            return
        store.add_health("allegro", kind, detail)
        self._health.add(kind)

    def _hook(self, fetcher) -> None:
        store = getattr(fetcher, "store", None)
        if store is not None:
            self.client.on_event = lambda k, d: self._health_once(store, k, d)

    async def _auth(self) -> str:
        if self._token and self._token[1] > time.time() + 60:
            return self._token[0]
        cid, sec = self._resolve()
        basic = base64.b64encode(f"{cid}:{sec}".encode()).decode()
        status, text = await self.client.call(
            "POST", TOKEN_URL,
            {"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
            urlencode({"grant_type": "client_credentials"}))
        try:
            j = json.loads(text)
        except ValueError:
            j = {}
        if status == 200 and j.get("access_token"):
            self._token = (j["access_token"], time.time() + float(j.get("expires_in", 43200)))
            return self._token[0]
        err = j.get("error")
        if err == "invalid_client" or status == 401:
            self.disabled_reason = "Allegro rejected the client credentials (invalid_client)"
            raise AdapterError(self.disabled_reason)
        raise AdapterError(f"allegro: token request failed (status {status})")

    def search_url(self, q: Query) -> str:
        # fallback=false: a miss stays empty instead of returning unrelated offers.
        # -startTime: newest fixed-price offers first. One page (max 60). No HTML.
        params = {
            "phrase": q.text, "searchMode": "REGULAR", "marketplaceId": "allegro-pl",
            "currency": "PLN", "shipping.country": "PL", "fallback": "false",
            "sort": "-startTime", "limit": self.limit, "offset": 0,
        }
        return f"{LISTING_URL}?{urlencode(params)}"

    async def search(self, query: Query, fetcher=None, to_pln=None) -> list[Listing]:
        store = getattr(fetcher, "store", None)
        if self._resolve() is None:
            self._health_once(store, "no_keys", NO_KEYS)
            return []
        if self.disabled_reason:
            self._health_once(store, self._disable_kind(), self.disabled_reason)
            return []
        if fetcher is not None:
            self._hook(fetcher)
        try:
            tok = await self._auth()
        except AdapterError:
            if self.disabled_reason:
                self._health_once(store, "bad_credentials", self.disabled_reason)
                return []
            raise
        status, text = await self.client.call("GET", self.search_url(query), {
            "Authorization": f"Bearer {tok}", "Accept": ACCEPT, "Accept-Language": "pl-PL"})
        if verification_required(status, text):
            self.disabled_reason = NOT_VERIFIED
            self._health_once(store, "not_verified", NOT_VERIFIED)
            return []
        if status == 401:
            self._token = None
            raise AdapterError("allegro: token rejected")
        if status != 200:
            raise AdapterError(f"allegro: search status {status}")
        return filter_query(parse_listing(text, self), query, to_pln or self.to_pln)

    def parse_detail(self, listing: Listing, html: str) -> Listing:
        return listing

    async def detail(self, listing: Listing, fetcher) -> Listing:
        # The search payload is the offer. Fetching allegro.pl would hit DataDome.
        return listing

    async def alive(self, listing: Listing, fetcher):
        raise AdapterError("allegro: alive checks are not supported (no per-offer public endpoint; the website is not fetched)")


def parse_listing(text: str, ad: Allegro) -> list[Listing]:
    """Buy-now offers from /offers/listing. Auctions, ads, empty stock and Allegro Lokalnie rows are dropped."""
    try:
        j = json.loads(text, parse_float=Decimal)
    except ValueError as e:
        raise AdapterError("allegro: invalid JSON") from e
    items = j.get("items") or {}
    rows = list(items.get("promoted") or []) + list(items.get("regular") or [])
    out: list[Listing] = []
    seen: set[str] = set()
    for it in rows:
        oid = str(it.get("id") or "")
        if not oid or oid in seen:
            continue
        vendor = str((it.get("vendor") or {}).get("id") or "")
        if vendor == "ALLEGRO_LOKALNIE":
            continue
        mode = it.get("sellingMode") or {}
        if mode.get("format") != "BUY_NOW":
            continue
        stock = (it.get("stock") or {}).get("available")
        if stock is not None and int(stock) <= 0:
            continue
        price = (mode.get("price") or {})
        amount, ccy = price.get("amount"), price.get("currency") or "PLN"
        if amount is None:
            continue
        try:
            dec = Decimal(str(amount))
        except Exception:
            continue
        if dec <= 0:
            continue
        sel = it.get("seller") or {}
        delivery = it.get("delivery") or {}
        parts = ["format: BUY_NOW"]
        if delivery.get("availableForFree"):
            parts.append("delivery: free")
        img = None
        for im in it.get("images") or []:
            url = (im or {}).get("url")
            if url and str(url).startswith("https://"):
                img = str(url)
                break
        company = sel.get("company")
        seen.add(oid)
        out.append(ad.make(
            source_id=oid, url=f"https://allegro.pl/oferta/{oid}", title=str(it.get("name") or "").strip(),
            description="; ".join(parts), price=dec, currency=str(ccy),
            seller_hash=seller_hash(ad.name, sel.get("id")),
            ships=True if delivery else None,
            is_business=bool(company) if company is not None else None,
            photos=[img] if img else []))
    return out
