"""EU import costs: ISZTAR customs duty and the 2026 low-value-consignment / handling fees."""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable

import httpx

from .money import HttpGet, httpx_get
from .store import Store

ISZTAR_URL = ("https://ext-isztar4.mf.gov.pl/tariff/rest/goods-nomenclature/measures"
              "?nomenclatureCode={code}&date={day}&language=EN")
DEFAULT_CN = {"gpu": "8473302000", "ram": "8473302000", "ssd": "8471709800", "pc": "8471500000"}
CACHE_DAYS = 30
EU = {"AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT", "LV",
      "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE"}
_PCT = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*%\s*$")


class CustomsError(RuntimeError):
    pass


def _as_date(v) -> date:
    return v if isinstance(v, date) else date.fromisoformat(str(v))


def import_fees_eur(value_eur: Decimal, items: int, day: date, cfg) -> Decimal:
    """Low-value consignment fee per item (value <= threshold, from a date) plus a per-item handling fee
    (from a date). Every constant comes from `cfg.economics.import_costs` (config/economics.yaml); `cfg` is
    the root Config."""
    ic = cfg.economics.import_costs
    fee = Decimal(0)
    if value_eur <= Decimal(str(ic.consignment_max_eur)) and day >= _as_date(ic.consignment_fee_from):
        fee += Decimal(str(ic.consignment_fee_eur)) * items
    if day >= _as_date(ic.handling_fee_from):
        fee += Decimal(str(ic.handling_fee_eur)) * items
    return fee


def _pct(amount: str) -> Decimal | None:
    m = _PCT.match(amount or "")
    return Decimal(m.group(1)) / 100 if m else None


def _rate_from_payload(body: str, origin: str) -> Decimal:
    try:
        measures = json.loads(body)["tariffMeasures"]
        # Customs-union partners (AD, SM, TR...) pay their own duty. Preferential rates are ignored:
        # they need a proof of origin we cannot assume, so the conservative answer is the third-country duty.
        for want_country, want_desc in ((origin, "Customs Union Duty"), ("ERGA OMNES", "Third country duty")):
            for m in measures:
                if m.get("country", {}).get("code") == want_country and m.get("description") == want_desc:
                    r = _pct(m.get("dutyAmount", ""))
                    if r is None:
                        raise CustomsError(f"non ad-valorem duty {m.get('dutyAmount')!r} for origin {origin}")
                    return r
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        raise CustomsError("bad ISZTAR payload") from e
    raise CustomsError("no third-country duty measure in ISZTAR response")


class Customs:
    def __init__(self, store: Store, http: HttpGet | None = None,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.store, self.http, self.now = store, http or httpx_get, now
        store.ensure_table("""create table if not exists duty_cache (
            cn_code text not null, origin text not null, rate text not null, fetched_at text not null,
            primary key (cn_code, origin))""")

    async def duty_rate(self, cn_code: str, origin: str, day: date) -> Decimal:
        """Ad-valorem duty as a fraction (0.065 = 6.5%). Raises CustomsError when it cannot be determined.

        NOTE: the cache key is (cn_code, origin) and ignores `day`: within the 30-day TTL a cached rate is
        served for any requested day, even one before or after the day it was fetched for.
        """
        origin = origin.upper()
        if origin in EU:
            return Decimal(0)
        row = self.store.conn.execute("select rate, fetched_at from duty_cache where cn_code=? and origin=?",
                                      (cn_code, origin)).fetchone()
        if row and datetime.fromisoformat(row["fetched_at"]) > self.now() - timedelta(days=CACHE_DAYS):
            return Decimal(row["rate"])
        try:
            status, body = await self.http(ISZTAR_URL.format(code=cn_code, day=day.isoformat()))
        except httpx.HTTPError as e:
            raise CustomsError(f"ISZTAR {cn_code}: {type(e).__name__}: {e}") from e
        if status == 400 and '"error-code":17' in body.replace(" ", ""):
            raise CustomsError(f"CN code {cn_code} is not a final goods code (needs a full 10-digit code)")
        if status != 200:
            raise CustomsError(f"ISZTAR {cn_code}: HTTP {status}")
        rate = _rate_from_payload(body, origin)
        self.store.conn.execute("insert or replace into duty_cache values (?,?,?,?)",
                                (cn_code, origin, str(rate), self.now().isoformat()))
        self.store.conn.commit()
        return rate
