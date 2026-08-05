"""Price parsing and NBP exchange rates. All money is Decimal; PLN results are 2 dp, ROUND_HALF_UP."""
from __future__ import annotations

import json
import re
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Awaitable, Callable

import httpx

from .store import Store

# An async getter returns (status_code, body_text). Tests inject a fake; production uses httpx.
HttpGet = Callable[[str], Awaitable[tuple[int, str]]]

NBP_URL = "https://api.nbp.pl/api/exchangerates/rates/a/{ccy}/{day}/?format=json"
CENT = Decimal("0.01")


def q2(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_HALF_UP)


async def httpx_get(url: str) -> tuple[int, str]:
    async with httpx.AsyncClient(timeout=30, follow_redirects=True,
                                 headers={"User-Agent": "deal-hunter/0.1"}) as c:
        r = await c.get(url)
        return r.status_code, r.text


# ---------------------------------------------------------------- price parsing

_CCY_PATTERNS = [
    ("PLN", re.compile(r"zł|zl\b|\bpln\b", re.I)),
    ("EUR", re.compile(r"€|\beur\b|\beuro\b", re.I)),
    ("CZK", re.compile(r"kč|\bkc\b|\bczk\b", re.I)),
    ("HUF", re.compile(r"\bft\b|\bhuf\b", re.I)),
    ("USD", re.compile(r"\$|\busd\b", re.I)),
    ("GBP", re.compile(r"£|\bgbp\b", re.I)),
]
_NUM = re.compile(r"\d{1,3}(?:[   ]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)*")
_THOUSANDS = re.compile(r"\b(?:tys|tis|ezer)\b\.?|\bk\b", re.I)


def _to_decimal(tok: str, thousand_multiplier: bool) -> Decimal | None:
    tok = re.sub(r"[   ]", "", tok)
    seps = [c for c in tok if c in ".,"]
    if not seps:
        return Decimal(tok)
    if "." in tok and "," in tok:
        dec = "." if tok.rfind(".") > tok.rfind(",") else ","
        thou = "," if dec == "." else "."
        if tok.count(dec) > 1:
            return None
        tok = tok.replace(thou, "").replace(dec, ".")
    else:
        s = seps[0]
        parts = tok.split(s)
        if len(parts) > 2:  # repeated separator: thousands ("1.234.567")
            if any(len(p) != 3 for p in parts[1:]):
                return None
            tok = "".join(parts)
        elif len(parts[1]) == 3 and not thousand_multiplier and not parts[0].startswith("0"):
            # "3.500" / "3,500": thousands. A leading-zero integer part ("0,500") is a decimal fraction.
            tok = "".join(parts)
        else:
            tok = parts[0] + "." + parts[1]
    return Decimal(tok)


def parse_price(text: str, default_currency: str) -> tuple[Decimal, str] | None:
    """Parse a listing price string. None if no positive amount can be read."""
    if not text:
        return None
    ccy = default_currency.upper()
    for code, pat in _CCY_PATTERNS:
        if pat.search(text):
            ccy = code
            break
    m = _NUM.search(text)
    if not m:
        return None
    before = text[: m.start()].rstrip()
    if before.endswith("-") or before.endswith("−"):
        return None
    mult = _THOUSANDS.search(text[m.end():]) is not None
    try:
        amount = _to_decimal(m.group(0), mult)
    except InvalidOperation:
        return None
    if amount is None:
        return None
    if mult:
        amount *= 1000
    if amount <= 0:
        return None
    return amount, ccy


# ---------------------------------------------------------------- NBP FX

class FxError(RuntimeError):
    pass


class Fx:
    """PLN per unit of currency, NBP table A mid rate, cached in the `fx` table."""

    def __init__(self, store: Store, http: HttpGet | None = None, today: Callable[[], date] = date.today,
                 max_back_days: int = 10):
        self.store, self.http, self.today, self.max_back = store, http or httpx_get, today, max_back_days
        cols = [r["name"] for r in store.conn.execute("pragma table_info(fx)")]
        if "effective_date" not in cols:
            store.ensure_table("alter table fx add column effective_date text")

    async def rate(self, ccy: str, day: date) -> Decimal:
        return (await self.rate_with_date(ccy, day))[0]

    async def rate_with_date(self, ccy: str, day: date) -> tuple[Decimal, date]:
        """(PLN per unit, NBP effective date). The effective date is earlier than `day` on weekends/holidays."""
        ccy = ccy.upper()
        if ccy == "PLN":
            return Decimal("1"), day
        row = self.store.conn.execute("select rate_pln, effective_date from fx where date=? and currency=?",
                                      (day.isoformat(), ccy)).fetchone()
        if row:
            return Decimal(row["rate_pln"]), date.fromisoformat(row["effective_date"] or day.isoformat())
        d = day
        for _ in range(self.max_back + 1):
            try:
                status, body = await self.http(NBP_URL.format(ccy=ccy.lower(), day=d.isoformat()))
            except httpx.HTTPError as e:
                raise FxError(f"NBP {ccy} {d}: {type(e).__name__}: {e}") from e
            if status == 200:
                try:
                    mid = json.loads(body, parse_float=Decimal)["rates"][0]["mid"]
                except (ValueError, KeyError, IndexError, TypeError) as e:
                    raise FxError(f"bad NBP payload for {ccy} {d}") from e
                rate = Decimal(mid)
                # A fallback for today/future may be superseded once NBP publishes: do not pin it.
                if d == day or day < self.today():
                    self._put(day, ccy, rate, d)
                return rate, d
            if status != 404:
                raise FxError(f"NBP {ccy} {d}: HTTP {status}")
            d -= timedelta(days=1)
        raise FxError(f"no NBP rate for {ccy} within {self.max_back} days before {day}")

    def _put(self, day: date, ccy: str, rate: Decimal, effective: date) -> None:
        self.store.conn.execute(
            "insert or replace into fx (date, currency, rate_pln, effective_date) values (?,?,?,?)",
            (day.isoformat(), ccy, str(rate), effective.isoformat()))
        self.store.conn.commit()

    async def to_pln(self, amount: Decimal, ccy: str, day: date) -> Decimal:
        return q2(amount * await self.rate(ccy, day))

    async def pln_to_usd(self, amount_pln: Decimal, day: date) -> Decimal:
        return q2(amount_pln / await self.rate("USD", day))
