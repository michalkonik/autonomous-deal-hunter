"""dh deals / show / books / round wiring against a temporary DH_ROOT (no network, no service)."""
import json
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from dealfix import make_deal  # noqa: E402

from dh import cli, runtime  # noqa: E402
from dh.store import Store  # noqa: E402


REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def droot(tmp_path, monkeypatch):
    shutil.copytree(REPO / "config", tmp_path / "config", ignore=shutil.ignore_patterns("local"))
    shutil.copytree(REPO / "playbook", tmp_path / "playbook")
    (tmp_path / "var").mkdir()
    monkeypatch.setenv("DH_ROOT", str(tmp_path))
    st = Store(tmp_path / "var" / "dh.db")
    d, _, _ = make_deal(st)
    st.close()
    return tmp_path, d


def test_deals_json_and_text(droot, capsys):
    _, d = droot
    assert cli.main(["deals", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["id"] == d.id and rows[0]["tier"] == 2 and rows[0]["listing"]["source"] == "kleinanzeigen"
    assert cli.main(["deals", "--tier", "1"]) == 0 and "no deals" in capsys.readouterr().out


def test_show_card(droot, capsys):
    _, d = droot
    assert cli.main(["show", str(d.id), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["deal_id"] == d.id
    assert cli.main(["show", "999"]) == 1


def test_books_json(droot, capsys):
    root, _ = droot
    st = Store(root / "var" / "dh.db")
    from datetime import datetime, timezone
    from decimal import Decimal
    rid = st.log_round(datetime.now(timezone.utc))
    st.add_cost(rid, "energy", Decimal("0.44"), 0.4)
    st.close()
    assert cli.main(["books", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["today"]["total_pln"] == "0.44" and out["deals"] == {"2": 1}


def test_round_refused_when_ram_low(droot, capsys, monkeypatch):
    monkeypatch.setattr(runtime, "is_active", lambda run=None: False)
    monkeypatch.setattr(runtime, "mem_available_gb", lambda meminfo="/proc/meminfo": 4.0)
    assert cli.main(["round", "--once", "--dry", "--no-llm"]) == 1
    assert "free RAM 4.0 GB < 12 GB" in capsys.readouterr().out


def test_no_db_message(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DH_ROOT", str(tmp_path))
    assert cli.main(["deals"]) == 1
    assert "no database yet" in capsys.readouterr().err
