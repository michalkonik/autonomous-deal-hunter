"""eBay Marketplace Insights (sold items), official API only. Limited-release: the application must have been granted
the scope `https://api.ebay.com/oauth/api_scope/buy.marketplace.insights`.

`EbayInsights.sold_comps(key)` returns `ebay_sold` CompRows (ingest with `valuation.ingest_comps`). Without keys, or
when eBay does not grant the scope (the token request answers 400 invalid_scope), the source disables itself for the session and records ONE
health event ("Marketplace Insights not granted", kind not_granted). Wrong keys (invalid_client / 401 on the token
request) disable it with kind bad_credentials. Refusals (403/429) of the search are not a scope signal.

It shares the per-host ApiClient of dh.sources.ebay (`api_client_for("api.ebay.com")`): common pacing, the same key
pool (`ebay`, `ebay2`, …), and backoff. A refused search counts on that shared client like any other refusal.
`invalid_scope` disables the source only when every stored key lacks the scope. HTTP 429 on one key switches
to the next key when more than one key is stored. 403/429 are not a scope signal.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable
from urllib.parse import urlencode

from ..models import CompRow
from . import ebay
from .base import AdapterError, price_of

SALES_URL = "https://api.ebay.com/buy/marketplace_insights/v1_beta/item_sales/search"
INSIGHTS_SCOPE = "https://api.ebay.com/oauth/api_scope/buy.marketplace.insights"
NOT_GRANTED = "Marketplace Insights not granted"
#: Marketplace Insights covers a few marketplaces only; (header id, channel, country)
MARKETS = {"EBAY_DE": ("ebay_de", "DE"), "EBAY_US": ("ebay_us", "US")}


class EbayInsights:
    def __init__(self, marketplace: str = "EBAY_DE", secret_fn: Callable[[str], str | None] | None = None,
                 client: "ebay.ApiClient | None" = None, store=None, limit: int = 50, weight: float | None = None):
        if marketplace not in MARKETS:
            raise ValueError(marketplace)
        self.marketplace, (self.channel, self.country) = marketplace, MARKETS[marketplace]
        self.limit, self.weight, self.store = limit, weight, store
        self._secret_fn = secret_fn or ebay._cached_bw_secret
        self._client = client
        self._token: tuple[str, float] | None = None
        self._token_index: int | None = None
        self.disabled_reason: str | None = None
        self._health_done = False

    @property
    def client(self):
        if self._client is None:
            self._client = ebay.api_client_for("api.ebay.com")
        return self._client

    @property
    def enabled(self) -> bool:
        return self.disabled_reason is None

    def _disable(self, kind: str, detail: str, fetcher=None) -> None:
        self.disabled_reason = detail
        if self._health_done:
            return
        self._health_done = True
        store = self.store or getattr(fetcher, "store", None)
        if store is not None:
            store.add_health("ebay_insights", kind, detail)

    def _sets(self) -> list[tuple[str, str]]:
        return self.client.ring_for(self._secret_fn).load()

    async def _auth(self) -> str | None:
        """Bearer token carrying the insights scope, None (and disabled) when keys or scope are missing."""
        if not self.enabled:
            return None
        sets = self._sets()
        if not sets:
            self._disable("no_keys", "no eBay keys (bw-secret ebay#client_id / ebay#client_secret, further sets ebay2 …): Marketplace Insights disabled")
            return None
        if len(sets) == 1:
            return await self._auth_one(sets[0])
        return await self._auth_pool(sets)

    async def _post_token(self, cid: str, sec: str, rotate: bool) -> tuple[int, dict]:
        import base64
        basic = base64.b64encode(f"{cid}:{sec}".encode()).decode()
        status, text = await self.client.call(
            "POST", ebay.TOKEN_URL, {"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
            urlencode({"grant_type": "client_credentials", "scope": INSIGHTS_SCOPE}), rotate_quota=rotate)
        try:
            j = json.loads(text)
        except ValueError:
            j = {}
        return status, j if isinstance(j, dict) else {}

    async def _auth_one(self, pair: tuple[str, str]) -> str | None:
        if self._token and self._token[1] > time.time() + 60:
            return self._token[0]
        status, j = await self._post_token(pair[0], pair[1], False)
        if status == 200 and j.get("access_token"):
            self._token = (j["access_token"], time.time() + float(j.get("expires_in", 7200)))
            return self._token[0]
        err = j.get("error")
        if err == "invalid_scope":  # the keys are fine, eBay has not granted the insights scope
            self._disable("not_granted", NOT_GRANTED)
            return None
        if err == "invalid_client" or status == 401:  # wrong keys: a different problem, a different health kind
            self._disable("bad_credentials", "eBay rejected the client credentials (invalid_client): Marketplace Insights disabled")
            return None
        raise AdapterError(f"ebay_insights: token request failed (status {status})")

    async def _auth_pool(self, sets: list[tuple[str, str]]) -> str | None:
        """Try each key. Scope missing on one key is not fatal while another key can still be asked."""
        ring = self.client.ring
        scope_miss = cred_miss = 0
        for idx, (cid, sec) in enumerate(sets):
            cached = ring.token(INSIGHTS_SCOPE, idx)
            if cached and ring.ready(idx):
                return cached
            if not ring.ready(idx):
                continue
            try:
                status, j = await self._post_token(cid, sec, True)
            except ebay.QuotaExceeded as e:
                ring.cool(idx, self.client.clock() + e.retry_after)
                ring.drop_token(INSIGHTS_SCOPE, idx)
                continue
            if status == 200 and j.get("access_token"):
                exp = time.time() + float(j.get("expires_in", 7200))
                ring.put_token(INSIGHTS_SCOPE, idx, j["access_token"], exp)
                self._token = (j["access_token"], exp)  # last good token; a 401 on search clears the ring copy
                self._token_index = idx
                return j["access_token"]
            err = j.get("error")
            if err == "invalid_scope":
                scope_miss += 1
                continue
            if err == "invalid_client" or status == 401:
                cred_miss += 1
                continue
            raise AdapterError(f"ebay_insights: token request failed (status {status})")
        if scope_miss and scope_miss == len(sets):
            self._disable("not_granted", NOT_GRANTED)
            return None
        if cred_miss and cred_miss + scope_miss == len(sets):
            self._disable("bad_credentials", "eBay rejected the client credentials (invalid_client): Marketplace Insights disabled")
            return None
        if ring.pick() is None:
            raise AdapterError("api backing off")
        raise AdapterError("ebay_insights: token request failed")

    async def _sales(self, q: str, tok: str, rotate: bool) -> tuple[int, str]:
        url = f"{SALES_URL}?{q}"
        base = {"X-EBAY-C-MARKETPLACE-ID": self.marketplace}
        if not rotate:
            return await self.client.call("GET", url, {"Authorization": f"Bearer {tok}", **base})
        ring = self.client.ring
        sets = ring.load()
        order: list[int] = []
        if self._token_index is not None:
            order.append(self._token_index)
        order += [i for i in range(len(sets)) if i not in order]
        saw_quota = False
        for idx in order:
            if not ring.ready(idx):
                continue
            bearer = ring.token(INSIGHTS_SCOPE, idx)
            if not bearer:
                status, j = await self._post_token(sets[idx][0], sets[idx][1], True)
                if not (status == 200 and j.get("access_token")):
                    continue
                bearer = j["access_token"]
                ring.put_token(INSIGHTS_SCOPE, idx, bearer, time.time() + float(j.get("expires_in", 7200)))
            try:
                status, text = await self.client.call(
                    "GET", url, {"Authorization": f"Bearer {bearer}", **base}, rotate_quota=True)
            except ebay.QuotaExceeded as e:
                ring.cool(idx, self.client.clock() + e.retry_after)
                ring.drop_token(INSIGHTS_SCOPE, idx)
                saw_quota = True
                continue
            self._token_index = idx
            self._token = (bearer, time.time() + 3600)
            return status, text
        if saw_quota:
            raise AdapterError("api backing off")
        raise AdapterError("ebay_insights: search failed")

    async def sold_comps(self, key: str, fetcher=None) -> list[CompRow]:
        if not self.enabled:
            return []
        if fetcher is not None and self.store is None:
            self.store = getattr(fetcher, "store", None)
        tok = await self._auth()
        if tok is None:
            return []
        q = urlencode({"q": ebay.query_for_key(key), "limit": self.limit})
        # A refusal (403/429) is raised by the shared ApiClient. With one key that backs off the host. With more
        # than one key a 429 cools that key and the next key is tried. Neither case is a scope signal.
        rotate = len(self._sets()) > 1
        status, text = await self._sales(q, tok, rotate)
        if status == 401:
            self._token = None
            if self._token_index is not None and self.client.ring is not None:
                self.client.ring.drop_token(INSIGHTS_SCOPE, self._token_index)
            raise AdapterError("ebay_insights: token rejected")
        if status != 200:
            raise AdapterError(f"ebay_insights: search status {status}")
        return parse_sales(text, key, self)


def _default_weight() -> float:
    try:
        from ..valuation import default_params
        return float(default_params().weights.get("ebay_sold", 1.0))
    except Exception:  # pragma: no cover - config unavailable
        return 1.0


def parse_sales(text: str, key: str, ad: EbayInsights) -> list[CompRow]:
    """Pure: item_sales/search body -> `ebay_sold` CompRows whose title re-extracts to exactly `key`."""
    from ..match import rules_extract
    try:
        j = json.loads(text, parse_float=Decimal)
    except ValueError as e:
        raise AdapterError("ebay_insights: invalid JSON") from e
    w = ad.weight if ad.weight is not None else _default_weight()
    out: list[CompRow] = []
    for it in j.get("itemSales") or []:
        p = it.get("lastSoldPrice") or {}
        pr = price_of(f"{p.get('value', '')} {p.get('currency', '')}", "EUR") if p.get("value") is not None and p.get("currency") else None
        sold = it.get("lastSoldDate")
        if pr is None or not it.get("itemId") or not sold:
            continue
        try:
            at = datetime.fromisoformat(sold.replace("Z", "+00:00"))
        except ValueError:
            continue
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        cond_id = str(it.get("conditionId", ""))
        ex = rules_extract(it.get("title") or "", f"condition: {it.get('condition') or ''}", ad.country.lower())
        if cond_id == "7000":
            continue  # for parts / not working
        if ex.key != key or ex.box_only or ex.for_parts or ex.wanted_ad or ex.bundle_items or ex.defects:
            continue
        cond = {"1000": "new", "1500": "new", "3000": "used"}.get(cond_id) or (ex.condition if ex.condition != "unknown" else None)
        out.append(CompRow(key=key, condition=cond, channel=ad.channel,
                           kind="ebay_sold", price=pr[0], currency=pr[1], at=at, weight=w,
                           ref=f"ebay_sold:{it['itemId']}:{sold}", country=ad.country))
    return out
