"""Books: energy and cost per round (spec §10 cost tracking)."""
import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from dh import books
from dh.config import Config
from dh.store import Store

ROCM = json.dumps({"card0": {"Average Graphics Package Power (W)": "250.0"},
                   "card1": {"Average Graphics Package Power (W)": "50.5"},
                   "card4": {"Current Socket Graphics Package Power (W)": "0.02"},
                   "system": {"Driver version": "6.1"}})


def test_parse_rocm_power_sums_cards():
    assert books.parse_rocm_power(ROCM) == pytest.approx(300.52)
    assert books.parse_rocm_power("not json") is None
    assert books.parse_rocm_power("{}") is None


def test_energy_pln_gpu_plus_base_times_tariff():
    # (avg 280 W GPU + 120 W base) x 1 h = 0.4 kWh x 1.10 = 0.44 PLN
    assert books.energy_pln(3600, [260.0, 300.0], 1.10) == Decimal("0.44")
    # no GPU samples: base only; 30 min x 120 W = 0.06 kWh x 1.10 = 0.066 -> 0.07
    assert books.energy_pln(1800, [], 1.10) == Decimal("0.07")
    assert books.energy_pln(0, [500.0], 1.10) == Decimal("0.00")


def test_record_round_and_summary(tmp_path):
    st = Store(tmp_path / "t.db")
    cfg = Config({"books": {"tariff_pln_kwh": 1.10, "base_w": 120}, "models": {"paid_api": {"enabled": False}}})
    rid = st.log_round(datetime(2026, 10, 29, 12, tzinfo=timezone.utc))
    out = books.record_round(st, rid, cfg, seconds=3600, gpu_watts=[280.0],
                             llm={"calls": 3, "prompt_tokens": 900, "completion_tokens": 300, "seconds": 4.5},
                             fetches=42)
    assert out["energy_pln"] == Decimal("0.44") and out["kwh"] == pytest.approx(0.4)
    rows = {r["kind"]: r for r in st.conn.execute("select * from costs where round_id=?", (rid,))}
    assert set(rows) == {"energy", "llm", "fetch", "paid_api"}
    assert Decimal(rows["energy"]["amount_pln"]) == Decimal("0.44")
    assert rows["llm"]["units"] == 1200 and rows["fetch"]["units"] == 42
    assert Decimal(rows["paid_api"]["amount_pln"]) == 0
    s = books.summary(st)
    assert s["total_pln"] == "0.44" and s["rounds"] == 1
    assert s["by_kind"]["energy"]["units"] == pytest.approx(0.4)
    assert s["by_kind"]["llm"]["units"] == 1200


def test_tariff_default_when_missing():
    assert books.tariff(Config({})) == 1.10
    assert books.tariff(Config({"books": {"tariff_pln_kwh": 0.9}})) == 0.9
