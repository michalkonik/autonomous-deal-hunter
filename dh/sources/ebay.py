"""eBay Browse API (official, keyed). eBay web pages are never scraped.

Keys: `bw-secret ebay#client_id` / `ebay#client_secret`. Without them the adapter is disabled and records ONE
health event ("no eBay keys"); nothing is requested.

ApiClient exception (documented in docs/ADDING-A-SOURCE.md): the OAuth token call is a POST and the API answers
unauthenticated GETs with a 403 error page that the web Fetcher would count as a bot block, so official API calls
go through this small client instead of `Fetcher`. It still paces requests per host (>= `min_interval_s`), honours
429/403 with a 60 s-doubling backoff (cap 1 h) and disables itself after 3 consecutive refusals. It skips
robots.txt ONLY for this documented, keyed API (api.ebay.com serves no robots.txt: 404). It sends the honest UA.
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from datetime import datetime
from decimal import Decimal
from typing import Awaitable, Callable
from urllib.parse import urlencode, urlsplit

import httpx

from ..models import CompRow, Listing
from .base import Adapter, AdapterError, Query, filter_query, now_utc, price_of, seller_hash

UA = "DealHunter/0.1 (personal price research; +https://github.com/michalkonik)"
TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
SCOPE = "https://api.ebay.com/oauth/api_scope"
MARKETS = {"EBAY_DE": ("ebay_de", "DE", "EUR"), "EBAY_PL": ("ebay_pl", "PL", "PLN"), "EBAY_US": ("ebay_us", "US", "USD")}
FOR_PARTS_IDS = {"7000"}  # "For parts or not working"
ALLOWED_API_HOSTS = frozenset({"api.ebay.com"})
Request = Callable[..., Awaitable[tuple[int, str]]]


async def _httpx_request(method: str, url: str, headers: dict, data: str | None, timeout: float) -> tuple[int, str]:
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as c:
        r = await c.request(method, url, headers=headers, content=data)
        return r.status_code, r.text, {k.lower(): v for k, v in r.headers.items()}


class ApiClient:
    """Paced, back-off-aware client for one official API host."""

    def __init__(self, request: Request | None = None, min_interval_s: float = 1.0, backoff_start_s: float = 60,
                 backoff_cap_s: float = 3600, disable_after: int = 3, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable] = asyncio.sleep, on_event: Callable[[str, str], None] | None = None):
        self._req = request or _httpx_request
        self.min_interval, self.b0, self.cap, self.disable_after = min_interval_s, backoff_start_s, backoff_cap_s, disable_after
        self.clock, self.sleep, self.on_event = clock, sleep, on_event or (lambda k, d: None)
        self._last = -1e18
        self._backoff_until = 0.0
        self._next_backoff = backoff_start_s
        self.refusals = 0
        self.disabled = False
        self._lock = asyncio.Lock()

    async def call(self, method: str, url: str, headers: dict | None = None, data: str | None = None) -> tuple[int, str]:
        host = (urlsplit(url).hostname or "").lower()
        if host not in ALLOWED_API_HOSTS:  # the robots skip is for the documented eBay API only
            raise AdapterError(f"ApiClient refuses host {host!r}: not in ALLOWED_API_HOSTS")
        if self.disabled:
            raise AdapterError("api disabled for this session after repeated refusals")
        async with self._lock:
            now = self.clock()
            if now < self._backoff_until:
                raise AdapterError(f"api backing off for {self._backoff_until - now:.0f}s more")
            wait = self._last + self.min_interval - now
            if wait > 0:
                await self.sleep(wait)
            h = {"User-Agent": UA, "Accept": "application/json", **(headers or {})}
            try:
                res = await self._req(method, url, h, data, 30)
                status, text = res[0], res[1]
                rhead = res[2] if len(res) > 2 else {}
            except httpx.HTTPError as e:
                self._last = self.clock()
                raise AdapterError(f"api transport error: {type(e).__name__}") from e
            self._last = self.clock()
            if status in (429, 403):
                self.refusals += 1
                wait_s = self._next_backoff
                ra = str(rhead.get("retry-after", "")).strip()
                if status == 429 and ra.isdigit():  # honour the server's Retry-After (never below our own floor)
                    wait_s = min(max(float(ra), self.b0), self.cap)
                self._backoff_until = self._last + wait_s
                self.on_event("blocked", f"api status {status}; backoff {wait_s:.0f}s; consecutive={self.refusals}")
                self._next_backoff = min(self._next_backoff * 2, self.cap)
                if self.refusals >= self.disable_after:
                    self.disabled = True
                    self.on_event("disabled", "api disabled for the session after repeated refusals")
                raise AdapterError(f"api refused ({status})")
            self.refusals, self._next_backoff = 0, self.b0
            return status, text


_clients: dict[str, ApiClient] = {}


def api_client_for(host: str) -> ApiClient:
    """The one shared ApiClient of an allowed API host (shared pacing, backoff and refusal count)."""
    host = host.lower()
    if host not in ALLOWED_API_HOSTS:
        raise AdapterError(f"no API client for host {host!r}: not in ALLOWED_API_HOSTS")
    if host not in _clients:
        _clients[host] = ApiClient()
    return _clients[host]


_secrets: dict[str, str | None] = {}


def _cached_bw_secret(ref: str) -> str | None:
    """bw_secret once per process and ref (a missing key is cached too); `clear_secret_cache()` resets."""
    if ref not in _secrets:
        from ..notify import bw_secret
        _secrets[ref] = bw_secret(ref)
    return _secrets[ref]


def clear_secret_cache() -> None:
    _secrets.clear()


def query_for_key(key: str) -> str:
    """Search text for a canonical product key ("gpu:rtx-4090:24" -> "rtx 4090"; results are re-checked by key)."""
    cat, *rest = key.split(":")
    if cat.startswith("gpu"):
        return rest[0].replace("-", " ") if rest else key
    if cat == "ram" and len(rest) >= 2:
        return f"{rest[0]} {rest[1]}gb"
    if cat == "ssd" and len(rest) >= 2:
        return f"ssd {rest[0]} {rest[1]}"
    return " ".join(r.replace("-", " ") for r in rest) or key


class EbayBrowse(Adapter):
    domain = "api.ebay.com"

    def __init__(self, marketplace: str = "EBAY_DE", secret_fn: Callable[[str], str | None] | None = None,
                 client: ApiClient | None = None, limit: int = 50, weight: float | None = None):
        if marketplace not in MARKETS:
            raise ValueError(marketplace)
        self.marketplace = marketplace
        self.name, self.country, self.currency = MARKETS[marketplace]
        self.limit = limit
        self.weight = weight
        self._secret_fn = secret_fn
        self._keys: tuple[str, str] | None | bool = False  # False = not resolved yet
        self.client = client or api_client_for("api.ebay.com")
        self._token: tuple[str, float] | None = None

    # ---------------------------------------------------------------- keys / health
    def _resolve(self) -> tuple[str, str] | None:
        if self._keys is False:
            fn = self._secret_fn or _cached_bw_secret
            cid, sec = fn("ebay#client_id"), fn("ebay#client_secret")
            self._keys = (cid, sec) if cid and sec else None
        return self._keys or None

    @property
    def enabled(self) -> bool:
        return self._resolve() is not None

    def _note_disabled(self, fetcher) -> None:
        store = getattr(fetcher, "store", None)
        if store is None:
            return
        # one event in total for all marketplaces (they share the same keys)
        if store.conn.execute("select 1 from health where kind='no_keys' and source like 'ebay%'").fetchone():
            return
        store.add_health("ebay", "no_keys", "no eBay keys (bw-secret ebay#client_id / ebay#client_secret): adapters disabled")

    def _hook(self, fetcher):
        store = getattr(fetcher, "store", None)
        if store is not None:
            self.client.on_event = lambda k, d: store.add_health(self.name, k, d)

    # ---------------------------------------------------------------- API
    async def _auth(self) -> str:
        if self._token and self._token[1] > time.time() + 60:
            return self._token[0]
        cid, sec = self._resolve()
        basic = base64.b64encode(f"{cid}:{sec}".encode()).decode()
        status, text = await self.client.call(
            "POST", TOKEN_URL, {"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
            urlencode({"grant_type": "client_credentials", "scope": SCOPE}))
        try:
            j = json.loads(text)
            tok = j["access_token"]
        except (ValueError, KeyError, TypeError) as e:
            raise AdapterError(f"{self.name}: token request failed (status {status})") from e
        self._token = (tok, time.time() + float(j.get("expires_in", 7200)))
        return tok

    def search_url(self, q: Query) -> str:
        flt = f"itemLocationCountry:{self.country},buyingOptions:{{FIXED_PRICE}}"
        return f"{SEARCH_URL}?{urlencode({'q': q.text, 'filter': flt, 'limit': self.limit})}"

    async def search(self, query: Query, fetcher=None, to_pln=None) -> list[Listing]:
        if not self.enabled:
            self._note_disabled(fetcher)
            return []
        if fetcher is not None:
            self._hook(fetcher)
        tok = await self._auth()
        status, text = await self.client.call("GET", self.search_url(query), {
            "Authorization": f"Bearer {tok}", "X-EBAY-C-MARKETPLACE-ID": self.marketplace})
        if status == 401:
            self._token = None
            raise AdapterError(f"{self.name}: token rejected")
        if status != 200:
            raise AdapterError(f"{self.name}: search status {status}")
        return filter_query(parse_search(text, self), query, to_pln or self.to_pln)

    async def active_comps(self, key: str, fetcher=None) -> list[CompRow]:
        """Active fixed-price asks for `key` on this marketplace as `active_ask` CompRows (ingest with
        `valuation.ingest_comps`). Items are re-extracted and kept only when their key matches exactly."""
        from ..match import rules_extract
        ls = await self.search(Query(text=query_for_key(key)), fetcher)
        w = self.weight if self.weight is not None else _default_weight()
        out, now = [], now_utc()
        for l in ls:
            ex = rules_extract(l.title, l.description, self.country.lower())
            if ex.key != key or ex.box_only or ex.for_parts or ex.wanted_ad or ex.bundle_items or ex.defects:
                continue
            out.append(CompRow(key=key, condition=ex.condition if ex.condition != "unknown" else None,
                               channel=self.name, kind="active_ask", price=l.price, currency=l.currency, at=now,
                               weight=w, ref=f"ebay:{l.source_id}", country=self.country))
        return out

    def parse_detail(self, listing: Listing, html: str) -> Listing:
        return listing

    async def alive(self, listing: Listing, fetcher):
        raise AdapterError(f"{self.name}: alive checks are not supported (API only)")


def _default_weight() -> float:
    try:
        from ..valuation import default_params
        return float(default_params().weights.get("active_ask", 0.3))
    except Exception:  # pragma: no cover - config unavailable
        return 0.3


def parse_search(text: str, ad: EbayBrowse) -> list[Listing]:
    try:
        j = json.loads(text, parse_float=Decimal)
    except ValueError as e:
        raise AdapterError(f"{ad.name}: invalid JSON") from e
    out: list[Listing] = []
    for it in j.get("itemSummaries") or []:
        opts = it.get("buyingOptions") or []
        if opts and "FIXED_PRICE" not in opts:  # auctions: a bid is not an ask
            continue
        p = it.get("price") or {}
        pr = price_of(f"{p.get('value', '')} {p.get('currency', ad.currency)}", ad.currency) if p.get("value") is not None else None
        if pr is None or not it.get("itemId") or not it.get("itemWebUrl"):
            continue
        sel = it.get("seller") or {}
        cond = it.get("condition") or ""
        parts = [f"condition: {cond}"] if cond else []
        if str(it.get("conditionId")) in FOR_PARTS_IDS:
            parts.append("for parts or not working")
        loc = it.get("itemLocation") or {}
        try:
            score = float(sel["feedbackPercentage"]) if sel.get("feedbackPercentage") is not None else None
        except (TypeError, ValueError):
            score = None
        created = None
        try:
            created = datetime.fromisoformat(it["itemCreationDate"].replace("Z", "+00:00")) if it.get("itemCreationDate") else None
        except ValueError:
            pass
        acct = (sel.get("sellerAccountType") or "").upper()
        img = (it.get("image") or {}).get("imageUrl")
        out.append(ad.make(
            source_id=str(it["itemId"]), url=it["itemWebUrl"], title=re.sub(r"\s+", " ", it.get("title") or "").strip(),
            description="; ".join(parts), price=pr[0], currency=pr[1], country=(loc.get("country") or ad.country),
            city=loc.get("city") or loc.get("postalCode"),
            seller_hash=seller_hash(ad.name, sel.get("username")), seller_ratings=sel.get("feedbackScore"),
            seller_score=score, ships=True if it.get("shippingOptions") else None,
            is_business=(acct == "BUSINESS") if acct else None, photos=[img] if img else [], posted_at=created))
    return out
