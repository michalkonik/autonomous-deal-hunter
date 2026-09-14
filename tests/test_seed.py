"""Bazoš ask-history seed (kukas/bazos_dataset), against a small fixture in the dataset's raw CSV format."""
from __future__ import annotations

import asyncio
import zipfile
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from dh import seed
from dh.store import Store

FIX = Path(__file__).parent / "fixtures" / "bazos" / "section_pc_sample.csv"


class FakeFx:
    def __init__(self):
        self.calls = []

    async def rate(self, ccy, day):
        self.calls.append((ccy, day))
        return Decimal("0.1700")


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "dh.db")
    yield s
    s.close()


def test_split_title_price():
    assert seed.split_title_price("Prodám RTX 3080 10GB - 12 000") == ("Prodám RTX 3080 10GB", "12 000")
    assert seed.split_title_price("Kingston 32GB DDR5: 2 400") == ("Kingston 32GB DDR5", "2 400")
    assert seed.split_title_price("Ryzen 5-3600 - 2 500") == ("Ryzen 5-3600", "2 500")
    assert seed.split_title_price("No price here") == ("No price here", None)


def test_import_bazos_fixture(store):
    fx = FakeFx()
    counts = asyncio.run(seed.import_bazos(store, FIX, fx))
    assert counts == {"rows": 10, "imported": 4, "duplicate": 0, "no_price": 1, "no_key": 1, "excluded": 4}
    rows = {r["key"]: r for r in store.conn.execute("select * from comps").fetchall()}
    assert set(rows) == {"gpu:rtx-3080:10", "gpu:rtx-4090:24", "ram:ddr5:32:2x16", "ssd:nvme:2tb"}
    r = rows["gpu:rtx-3080:10"]
    assert (r["kind"], r["channel"], r["condition"]) == ("ask_history", "bazos_cz", "used")
    assert r["price_pln"] == "2040.00"  # 12 000 CZK × 0.17
    assert r["sold_at"] == datetime(2024, 7, 1, 8, 0, tzinfo=timezone.utc).isoformat()
    assert r["ref"] == "https://pc.bazos.cz/inzerat/100000001/rtx-3080.php"
    assert ("CZK", date(2024, 7, 1)) in fx.calls
    # idempotent
    again = asyncio.run(seed.import_bazos(store, FIX, fx))
    assert again["imported"] == 0 and again["duplicate"] == 4


def test_import_from_zip(store, tmp_path):
    z = tmp_path / "section_pc.csv.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.write(FIX, "section_pc/part1.csv")
    counts = asyncio.run(seed.import_bazos(store, z, FakeFx()))
    assert counts["imported"] == 4


def test_postprocessed_csv_with_header(store, tmp_path):
    p = tmp_path / "pp.csv"
    p.write_text("title,price,price_string,link,datetime\n"
                 "RTX 3080 10GB,12000,12 000,https://pc.bazos.cz/inzerat/1/a.php,2024-07-01 10:00:00+02:00\n")
    counts = asyncio.run(seed.import_bazos(store, p, FakeFx()))
    assert counts["imported"] == 1


@pytest.mark.live
def test_dataset_url_reachable(tmp_path):
    """Live (rig): the PC section downloads within the 200 MB cap."""
    path = asyncio.run(seed.download(seed.BAZOS_PC_URL, tmp_path / "pc.zip"))
    assert zipfile.is_zipfile(path)
