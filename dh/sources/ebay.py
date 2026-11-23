"""eBay Browse API (official, keyed). eBay web pages are never scraped.

Keys: Bitwarden items `ebay`, then `ebay2` … `ebay8`, fields `client_id` and `client_secret` (`dev_id` is stored
and never sent). A missing `client_id` ends the list. An item with an id and no secret is skipped. Without any
complete pair the adapter is disabled and records ONE health event ("no eBay keys"); nothing is requested.
With two or more pairs, HTTP 429 cools that application and the next pair is used at once. With one pair, 429
and 403 still back off 60 s doubling to 1 h and the client disables after 3 consecutive refusals.

ApiClient exception (documented in docs/ADDING-A-SOURCE.md): the OAuth token call is a POST and the API answers
unauthenticated GETs with a 403 error page that the web Fetcher would count as a bot block, so official API calls
go through this small client instead of `Fetcher`. It still paces requests per host (>= `min_interval_s`). It skips
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
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import httpx

from ..models import CompRow, Listing
from .base import Adapter, AdapterError, Query, filter_query, now_utc, price_of, seller_hash

UA = "DealHunter/0.1 (personal price research; +https://github.com/michal-koniu)"
TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
ITEM_URL = "https://api.ebay.com/buy/browse/v1/item/{}"
GROUP_URL = "https://api.ebay.com/buy/browse/v1/item/get_items_by_item_group?item_group_id={}"
SCOPE = "https://api.ebay.com/oauth/api_scope"
MARKETS = {"EBAY_DE": ("ebay_de", "DE", "EUR"), "EBAY_PL": ("ebay_pl", "PL", "PLN"), "EBAY_US": ("ebay_us", "US", "USD"),
           "EBAY_GB": ("ebay_gb", "GB", "GBP"), "EBAY_FR": ("ebay_fr", "FR", "EUR"),
           "EBAY_IT": ("ebay_it", "IT", "EUR"), "EBAY_ES": ("ebay_es", "ES", "EUR"),
           "EBAY_IE": ("ebay_ie", "IE", "EUR"), "EBAY_AT": ("ebay_at", "AT", "EUR"),
           "EBAY_CH": ("ebay_ch", "CH", "CHF"), "EBAY_BE": ("ebay_be", "BE", "EUR")}
# Foreign markets only return items the seller ships to Poland (verified filter: an unsupported filter value
# produces a warning while deliveryCountry is accepted; total counts show it genuinely filters).
SHIP_TO = "PL"
# US item location still needs shipToLocations. Europe / worldwide includes Poland unless Poland is excluded.
_POLAND_REGIONS = frozenset({"PL", "POLAND", "WORLDWIDE", "EUROPE", "EUROPEAN_UNION", "EU"})
_POLAND_BLOCK = frozenset({"PL", "POLAND", "WORLDWIDE"})


def _region_names(regions) -> set[str]:
    out: set[str] = set()
    for region in regions or []:
        if not isinstance(region, dict):
            continue
        name = str(region.get("regionName") or region.get("regionId") or "").upper().replace(" ", "_")
        typ = str(region.get("regionType") or "").upper()
        if name:
            out.add(name)
        if typ == "WORLDWIDE" or name in {"WORLDWIDE", "WORLD_WIDE"}:
            out.add("WORLDWIDE")
    return out


def poland_in_ship_to(item: dict) -> bool | None:
    """True/False when `shipToLocations` is present. None when this payload does not say."""
    if "shipToLocations" not in item:
        return None
    loc = item.get("shipToLocations") or {}
    included = _region_names(loc.get("regionIncluded"))
    excluded = _region_names(loc.get("regionExcluded"))
    if excluded & _POLAND_BLOCK:
        return False
    return bool(included & _POLAND_REGIONS)
FOR_PARTS_IDS = {"7000"}  # "For parts or not working"
ALLOWED_API_HOSTS = frozenset({"api.ebay.com"})
Request = Callable[..., Awaitable[tuple[int, str]]]
MAX_KEY_SETS = 8
_generation = 0


class QuotaExceeded(AdapterError):
    """One application's daily or burst quota (HTTP 429). Not a host-wide refusal when another key is ready."""

    def __init__(self, retry_after: float):
        self.retry_after = retry_after
        super().__init__(f"api quota ({retry_after:.0f}s)")


def item_name(n: int) -> str:
    """0 -> ebay, 1 -> ebay2, … so a gap in `ebay2` stops the walk before `ebay3`."""
    return "ebay" if n == 0 else f"ebay{n + 1}"


def candidate_refs() -> tuple[str, ...]:
    refs: list[str] = []
    for n in range(MAX_KEY_SETS):
        item = item_name(n)
        refs.append(f"{item}#client_id")
        refs.append(f"{item}#client_secret")
    refs.extend(("allegro#client_id", "allegro#client_secret"))
    return tuple(refs)


def load_key_sets(secret_fn: Callable[[str], str | None]) -> list[tuple[str, str]]:
    """Complete (client_id, client_secret) pairs in item order. Stop at the first missing client id."""
    out: list[tuple[str, str]] = []
    for n in range(MAX_KEY_SETS):
        item = item_name(n)
        cid = secret_fn(f"{item}#client_id")
        if not cid:
            break
        sec = secret_fn(f"{item}#client_secret")
        if sec:
            out.append((cid, sec))
    return out


class KeyRing:
    """Key sets plus per-index quota cooldowns and bearer tokens, on one ApiClient."""

    def __init__(self, secret_fn: Callable[[str], str | None], clock: Callable[[], float]):
        self.secret_fn, self.clock = secret_fn, clock
        self.sets: list[tuple[str, str]] | None = None
        self.cool_until: dict[int, float] = {}
        self.tokens: dict[tuple[str, int], tuple[str, float]] = {}
        self.gen = -1

    def load(self) -> list[tuple[str, str]]:
        if self.sets is None or self.gen != _generation:
            self.sets = load_key_sets(self.secret_fn)
            self.cool_until.clear()
            self.tokens.clear()
            self.gen = _generation
        return self.sets

    def ready(self, index: int) -> bool:
        return self.clock() >= self.cool_until.get(index, 0.0)

    def cool(self, index: int, until: float) -> None:
        self.cool_until[index] = until

    def pick(self) -> int | None:
        sets = self.load()
        for i in range(len(sets)):
            if self.ready(i):
                return i
        return None

    def soonest(self) -> float:
        sets = self.load()
        if not sets:
            return 0.0
        return min(self.cool_until.get(i, 0.0) for i in range(len(sets)))

    def token(self, scope: str, index: int) -> str | None:
        t = self.tokens.get((scope, index))
        if t and t[1] > time.time() + 60:
            return t[0]
        return None

    def put_token(self, scope: str, index: int, tok: str, exp: float) -> None:
        self.tokens[(scope, index)] = (tok, exp)

    def drop_token(self, scope: str, index: int) -> None:
        self.tokens.pop((scope, index), None)


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
        self.ring: KeyRing | None = None
        self._ring_gen = -1
        self._ring_fn: Callable[[str], str | None] | None = None

    def ring_for(self, secret_fn: Callable[[str], str | None]) -> KeyRing:
        if self.ring is None or self._ring_gen != _generation or self._ring_fn is not secret_fn:
            self.ring = KeyRing(secret_fn, self.clock)
            self._ring_gen = _generation
            self._ring_fn = secret_fn
        return self.ring

    async def call(self, method: str, url: str, headers: dict | None = None, data: str | None = None,
                   rotate_quota: bool = False) -> tuple[int, str]:
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
            if status == 429 and rotate_quota:
                # Another stored application can take this call. Do not cool the host or burn a refusal.
                wait_s = self.b0
                ra = str(rhead.get("retry-after", "")).strip()
                if ra.isdigit():
                    wait_s = min(max(float(ra), self.b0), self.cap)
                raise QuotaExceeded(wait_s)
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
        from .. import notify
        _secrets[ref] = notify.bw_secret(ref)
    return _secrets[ref]


def clear_secret_cache() -> None:
    global _generation
    _secrets.clear()
    _generation += 1


KEY_REFS = candidate_refs()


def prefetch_secrets(refs: tuple[str, ...] = KEY_REFS) -> None:
    """Resolve the uncached refs now, in parallel threads, so no round ever waits on `bw-secret` (about 5 s per
    call when the item is missing) inside its event loop: `EbayBrowse.enabled` is read from the cache. Called
    once per run, before the first round (runtime.build_context)."""
    from concurrent.futures import ThreadPoolExecutor

    from .. import notify
    todo = [r for r in refs if r not in _secrets]
    if not todo:
        return
    with ThreadPoolExecutor(max_workers=len(todo)) as ex:
        for ref, val in zip(todo, ex.map(notify.bw_secret, todo)):
            _secrets[ref] = val


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
        self._keys: list[tuple[str, str]] | None | bool = False  # False = not resolved yet
        self._gen = -1
        self.client = client or api_client_for("api.ebay.com")
        self._token: tuple[str, float] | None = None
        #: source_id -> item group id for variation listings found by the last parse_search
        self.variations: dict[str, str] = {}
        #: source_id -> confirmed ship-to-Poland, so a later round does not call getItem again
        self._pl_ship: dict[str, bool] = {}

    # ---------------------------------------------------------------- keys / health
    def _secret(self) -> Callable[[str], str | None]:
        return self._secret_fn or _cached_bw_secret

    def _resolve(self) -> list[tuple[str, str]] | None:
        if self._keys is False or self._gen != _generation:
            sets = self.client.ring_for(self._secret()).load()
            self._keys = sets or None
            self._gen = _generation
        return self._keys or None

    def _multi(self) -> bool:
        sets = self._resolve()
        return bool(sets) and len(sets) > 1

    @property
    def enabled(self) -> bool:
        return self._resolve() is not None

    def note_disabled(self, store) -> None:
        """One `no_keys` event for every marketplace. The scheduler calls this when it skips the arms."""
        self._note_store(store)

    def _note_disabled(self, fetcher) -> None:
        store = getattr(fetcher, "store", None)
        if store is not None:
            self._note_store(store)

    def _note_store(self, store) -> None:
        # one event in total for all marketplaces (they share the same keys)
        if store.conn.execute("select 1 from health where kind='no_keys' and source like 'ebay%'").fetchone():
            return
        store.add_health("ebay", "no_keys",
                         "no eBay keys (bw-secret ebay#client_id / ebay#client_secret, further sets ebay2 …): adapters disabled")

    def _hook(self, fetcher):
        store = getattr(fetcher, "store", None)
        if store is not None:
            self.client.on_event = lambda k, d: store.add_health(self.name, k, d)

    def _note_quota(self, index: int, n: int) -> None:
        self.client.on_event("quota", f"eBay key {index + 1} of {n} hit a quota; switched to the next key")

    # ---------------------------------------------------------------- API
    async def _auth(self) -> str:
        """Bearer for the single stored application. Cached on this adapter so a second search does not re-auth."""
        if self._token and self._token[1] > time.time() + 60:
            return self._token[0]
        cid, sec = self._resolve()[0]
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

    async def _token_for(self, index: int, cid: str, sec: str) -> str:
        ring = self.client.ring
        cached = ring.token(SCOPE, index) if ring is not None else None
        if cached:
            return cached
        basic = base64.b64encode(f"{cid}:{sec}".encode()).decode()
        status, text = await self.client.call(
            "POST", TOKEN_URL, {"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
            urlencode({"grant_type": "client_credentials", "scope": SCOPE}), rotate_quota=True)
        try:
            j = json.loads(text)
            tok = j["access_token"]
        except (ValueError, KeyError, TypeError) as e:
            raise AdapterError(f"{self.name}: token request failed (status {status})") from e
        if status != 200:
            raise AdapterError(f"{self.name}: token request failed (status {status})")
        ring.put_token(SCOPE, index, tok, time.time() + float(j.get("expires_in", 7200)))
        return tok

    async def _rotating(self, method: str, url: str, headers: dict) -> tuple[int, str]:
        """Try each ready key. A 429 cools that key only; a 403 stays a host refusal inside ApiClient.call."""
        ring = self.client.ring
        sets = ring.load()
        tried = 0
        while tried < len(sets):
            idx = ring.pick()
            if idx is None:
                wait = max(0.0, ring.soonest() - self.client.clock())
                raise AdapterError(f"api backing off for {wait:.0f}s more")
            cid, sec = sets[idx]
            try:
                tok = await self._token_for(idx, cid, sec)
                status, text = await self.client.call(
                    method, url, {**headers, "Authorization": f"Bearer {tok}"}, rotate_quota=True)
            except QuotaExceeded as e:
                ring.cool(idx, self.client.clock() + e.retry_after)
                ring.drop_token(SCOPE, idx)
                self._note_quota(idx, len(sets))
                tried += 1
                continue
            if status == 401:
                ring.drop_token(SCOPE, idx)
                try:
                    tok = await self._token_for(idx, cid, sec)
                except QuotaExceeded as e:
                    ring.cool(idx, self.client.clock() + e.retry_after)
                    self._note_quota(idx, len(sets))
                    tried += 1
                    continue
                status, text = await self.client.call(
                    method, url, {**headers, "Authorization": f"Bearer {tok}"}, rotate_quota=True)
                if status == 401:
                    raise AdapterError(f"{self.name}: token rejected")
            return status, text
        wait = max(0.0, ring.soonest() - self.client.clock())
        raise AdapterError(f"api backing off for {wait:.0f}s more")

    def search_url(self, q: Query) -> str:
        parts = [f"itemLocationCountry:{self.country}"]
        if self.country != SHIP_TO:
            parts.append(f"deliveryCountry:{SHIP_TO}")
        parts.append("buyingOptions:{FIXED_PRICE}")
        return f"{SEARCH_URL}?{urlencode({'q': q.text, 'filter': ','.join(parts), 'limit': self.limit})}"

    async def search(self, query: Query, fetcher=None, to_pln=None) -> list[Listing]:
        if not self.enabled:
            self._note_disabled(fetcher)
            return []
        if fetcher is not None:
            self._hook(fetcher)
        headers = {"X-EBAY-C-MARKETPLACE-ID": self.marketplace}
        if self._multi():
            status, text = await self._rotating("GET", self.search_url(query), headers)
        else:
            tok = await self._auth()
            status, text = await self.client.call("GET", self.search_url(query), {
                "Authorization": f"Bearer {tok}", **headers})
            if status == 401:
                self._token = None
                raise AdapterError(f"{self.name}: token rejected")
        if status != 200:
            raise AdapterError(f"{self.name}: search status {status}")
        ls = filter_query(parse_search(text, self), query, to_pln or self.to_pln)
        await self.price_variations(ls, self._token[0] if self._token else "")
        return ls

    async def confirm_ships_to_poland(self, listing: Listing) -> bool:
        """One getItem for a US listing. True only when shipToLocations includes Poland. Cached per item id.

        A non-200, a transport error, or a payload with no Poland/Europe/worldwide region is False. The search
        filter is not re-checked here; this is the listing-level confirmation the filter does not provide.
        """
        sid = str(listing.source_id)
        if sid in self._pl_ship:
            return self._pl_ship[sid]
        url = ITEM_URL.format(quote(sid, safe=""))
        headers = {"X-EBAY-C-MARKETPLACE-ID": self.marketplace}
        try:
            if self._multi():
                status, text = await self._rotating("GET", url, headers)
            else:
                tok = await self._auth()
                status, text = await self.client.call(
                    "GET", url, {"Authorization": f"Bearer {tok}", **headers})
        except AdapterError:
            self._pl_ship[sid] = False
            return False
        ok = False
        if status == 200:
            try:
                ok = poland_in_ship_to(json.loads(text)) is True
            except ValueError:
                ok = False
        self._pl_ship[sid] = ok
        return ok

    async def price_variations(self, ls: list[Listing], tok: str) -> None:
        """Variation listings show the price of their CHEAPEST option while the title advertises the strongest
        one (observed: an 'RTX 5090' PC listed at 2399 whose 5090 option costs 6699; 999 buys the RTX 3050).
        Ask the group endpoint and price at the HIGHEST option: a deal at that price is a real deal.
        400 means the id is not a group after all; the warning note stays and the price is left untouched."""
        for l in ls:
            gid = self.variations.get(l.source_id)
            if not gid:
                continue
            try:
                if self._multi():
                    status, text = await self._rotating(
                        "GET", GROUP_URL.format(gid), {"X-EBAY-C-MARKETPLACE-ID": self.marketplace})
                else:
                    status, text = await self.client.call("GET", GROUP_URL.format(gid), {
                        "Authorization": f"Bearer {tok}", "X-EBAY-C-MARKETPLACE-ID": self.marketplace})
            except AdapterError:
                continue
            if status != 200:
                continue
            try:
                items = json.loads(text, parse_float=Decimal).get("items") or []
            except ValueError:
                continue
            same = [Decimal(str(i["price"]["value"])) for i in items
                    if (i.get("price") or {}).get("value") is not None
                    and (i["price"].get("currency") or l.currency) == l.currency]
            if not same:
                continue
            hi = max(same)
            if hi > l.price:
                l.description += f"; variation listing verified: {len(same)} options, price set to the highest option {hi} {l.currency}"
                l.price = hi
            else:
                l.description += f"; variation listing verified: {len(same)} options, listed price is already the highest"

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


def _group_id(it: dict) -> str:
    href = it.get("itemGroupHref") or ""
    gid = (parse_qs(urlsplit(href).query).get("item_group_id") or [""])[0]
    if not gid.isdigit():
        segs = str(it.get("itemId") or "").split("|")
        gid = segs[1] if len(segs) > 2 and segs[1].isdigit() else ""
    return gid


def parse_search(text: str, ad: EbayBrowse) -> list[Listing]:
    try:
        j = json.loads(text, parse_float=Decimal)
    except ValueError as e:
        raise AdapterError(f"{ad.name}: invalid JSON") from e
    ad.variations = {}
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
        if it.get("itemGroupType"):
            gid = _group_id(it)
            if gid:
                parts.append("variation listing: price may be for the cheapest option, not the one in the title")
                ad.variations[str(it["itemId"])] = gid
        if str(it.get("conditionId")) in FOR_PARTS_IDS:
            parts.append("for parts or not working")
        loc = it.get("itemLocation") or {}
        country = loc.get("country") or ad.country
        if str(country).upper() == "US":
            # Domestic US shippingOptions are not evidence of a shipment to Poland.
            ship_to = poland_in_ship_to(it)
            if ship_to is True:
                parts.append("ships to Poland")
                ships_flag: bool | None = True
            elif ship_to is False:
                parts.append("does not ship to Poland")
                ships_flag = False
            else:
                parts.append("US location: ship-to-Poland not confirmed")
                ships_flag = None
        else:
            ships_flag = True if it.get("shippingOptions") else None
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
            description="; ".join(parts), price=pr[0], currency=pr[1], country=country,
            city=loc.get("city") or loc.get("postalCode"),
            seller_hash=seller_hash(ad.name, sel.get("username")), seller_ratings=sel.get("feedbackScore"),
            seller_score=score, ships=ships_flag,
            is_business=(acct == "BUSINESS") if acct else None, photos=[img] if img else [], posted_at=created))
    return out
