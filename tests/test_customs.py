import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from dh import config
from dh.customs import DEFAULT_CN, Customs, CustomsError, import_fees_eur
from dh.store import Store

FIX = Path(__file__).parent / "fixtures" / "isztar"
D = date(2026, 10, 29)


class FakeHttp:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    async def __call__(self, url):
        self.calls.append(url)
        for frag, resp in self.routes.items():
            if frag in url:
                return resp
        return 404, "not found"


def fx_(name):
    return (FIX / name).read_text()


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


def test_default_map():
    assert DEFAULT_CN == {"gpu": "8473302000", "ram": "8473302000", "ssd": "8471709800", "pc": "8471500000"}


CFG = config.load(Path(__file__).parent.parent)


@pytest.mark.parametrize("value,items,day,expected", [
    (100, 1, date(2026, 10, 30), 3),
    (100, 1, date(2026, 11, 2), 5),
    (900, 1, date(2026, 11, 2), 2),
    (150, 2, date(2026, 7, 1), 6),
    (150.01, 1, date(2026, 8, 1), 0),
    (100, 1, date(2026, 6, 30), 0),
    (100, 3, date(2026, 11, 1), 15),
])
def test_import_fees(value, items, day, expected):
    assert import_fees_eur(Decimal(str(value)), items, day, CFG) == Decimal(expected)


async def test_duty_from_real_fixture_gpu_cn(store):
    http = FakeHttp({"nomenclatureCode=8473302000": (200, fx_("8473302000.json"))})
    c = Customs(store, http)
    assert await c.duty_rate("8473302000", "CN", D) == Decimal("0")
    assert "nomenclatureCode=8473302000" in http.calls[0] and "date=2026-10-29" in http.calls[0]
    assert "language=EN" in http.calls[0]


async def test_duty_ssd_nonfinal_code_raises_with_real_400(store):
    http = FakeHttp({"nomenclatureCode=8471700000": (400, fx_("8471700000.json"))})
    with pytest.raises(CustomsError, match="not a final"):
        await Customs(store, http).duty_rate("8471700000", "CN", D)


async def test_duty_final_ssd_code(store):
    http = FakeHttp({"8471709800": (200, fx_("8471709800.json"))})
    assert await Customs(store, http).duty_rate("8471709800", "CN", D) == Decimal("0")


def payload(*measures):
    return json.dumps({"nomenclature": {"code": "x"}, "tariffMeasures": list(measures)})


def m(code, desc, amt, group=False):
    return {"country": {"code": code, "group": group}, "description": desc, "dutyAmount": amt}


async def test_duty_third_country_nonzero_and_preference_ignored(store):
    body = payload(m("ERGA OMNES", "Third country duty", "6.5%", True),
                   m("ERGA OMNES", "Low-value consignment customs duty", "3 EUR", True),
                   m("VN", "Tariff preference", "0%"))
    c = Customs(store, FakeHttp({"nomenclatureCode=1": (200, body)}))
    assert await c.duty_rate("1", "CN", D) == Decimal("0.065")
    # preference needs a proof of origin: conservative = full third-country duty
    assert await c.duty_rate("1", "VN", D) == Decimal("0.065")


async def test_duty_eu_origin_is_zero_and_no_http(store):
    http = FakeHttp({})
    assert await Customs(store, http).duty_rate("1", "DE", D) == Decimal("0")
    assert http.calls == []


async def test_duty_customs_union_origin(store):
    body = payload(m("ERGA OMNES", "Third country duty", "6.5%", True), m("TR", "Customs Union Duty", "0%"))
    assert await Customs(store, FakeHttp({"nomenclatureCode=1": (200, body)})).duty_rate("1", "TR", D) == Decimal("0")


async def test_duty_compound_or_missing_raises(store):
    body = payload(m("ERGA OMNES", "Third country duty", "3.7% + 22 EUR/100kg", True))
    with pytest.raises(CustomsError):
        await Customs(store, FakeHttp({"nomenclatureCode=1": (200, body)})).duty_rate("1", "CN", D)
    with pytest.raises(CustomsError):
        await Customs(store, FakeHttp({"nomenclatureCode=2": (200, payload())})).duty_rate("2", "CN", D)
    with pytest.raises(CustomsError):
        await Customs(store, FakeHttp({})).duty_rate("3", "CN", D)  # 404


async def test_duty_cached_30_days(store):
    t = [datetime(2026, 10, 29, tzinfo=timezone.utc)]
    http = FakeHttp({"nomenclatureCode=8473302000": (200, fx_("8473302000.json"))})
    c = Customs(store, http, now=lambda: t[0])
    await c.duty_rate("8473302000", "CN", D)
    t[0] += timedelta(days=29)
    await c.duty_rate("8473302000", "CN", D)
    assert len(http.calls) == 1
    t[0] += timedelta(days=2)
    await c.duty_rate("8473302000", "CN", D)
    assert len(http.calls) == 2
    # a fresh instance reads the DB cache
    t[0] += timedelta(days=1)
    c2 = Customs(store, FakeHttp({}), now=lambda: t[0])
    assert await c2.duty_rate("8473302000", "CN", D) == Decimal("0")


@pytest.mark.live
async def test_live_isztar_gpu(store):
    from datetime import date as dd
    r = await Customs(store).duty_rate("8473302000", "CN", dd.today())
    print("LIVE duty 8473302000 CN", dd.today(), r)
    assert r == Decimal("0")


async def test_network_error_becomes_customs_error(store):
    import httpx

    async def boom(url):
        raise httpx.ConnectError("down")
    with pytest.raises(CustomsError):
        await Customs(store, boom).duty_rate("1", "CN", D)


def test_import_fees_read_config_not_constants():
    over = config.deep_merge(CFG.to_dict(), {"economics": {"import_costs": {
        "consignment_fee_eur": 7, "handling_fee_eur": 1, "consignment_max_eur": 50,
        "consignment_fee_from": "2026-01-01", "handling_fee_from": "2026-02-01"}}})
    cfg = config.Config(over)
    assert import_fees_eur(Decimal("40"), 2, date(2026, 3, 1), cfg) == Decimal("16")
    assert import_fees_eur(Decimal("60"), 2, date(2026, 3, 1), cfg) == Decimal("2")
    assert import_fees_eur(Decimal("40"), 2, date(2026, 1, 15), cfg) == Decimal("14")


def test_import_fee_constants_live_only_in_config():
    src = (Path(__file__).parent.parent / "dh" / "customs.py").read_text()
    for needle in ("2026, 7", "2026, 11", "<= 150", "3 * items", "2 * items"):
        assert needle not in src


async def test_malformed_measures_is_customs_error(store):
    for bad in ('{"tariffMeasures": ["oops"]}', '{"tariffMeasures": [{"country": "CN"}]}'):
        with pytest.raises(CustomsError):
            await Customs(store, FakeHttp({"nomenclatureCode=9": (200, bad)})).duty_rate("9", "CN", D)
