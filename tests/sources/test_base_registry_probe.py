from __future__ import annotations

import asyncio
import io
import os
import re
import stat
from pathlib import Path

import pytest

from dh import probe
from dh.config import load
from dh.sources import DROPPED, Query, registry
from dh.sources import base
from dh.sources.base import Adapter, get_salt, seller_hash

from helpers import fixture_text

ROOT = Path(__file__).resolve().parents[2]


def test_registry_default_and_enabled_filter():
    reg = registry(load(ROOT), ROOT)
    assert {"allegro_lokalnie", "sprzedajemy", "kleinanzeigen", "hardverapro", "marktplaats",
            "bazos_cz", "bazos_sk"} <= set(reg)
    assert "bazos_at" not in reg
    assert all(isinstance(a, Adapter) and a.domain and a.country and a.currency for a in reg.values())
    assert "olx" not in reg and "olx_pl" not in reg  # OLX moved to the mailbox channel
    assert "sbazar" not in reg and "sbazar" in DROPPED and "robots" in DROPPED["sbazar"]


def test_registry_respects_enabled_false(tmp_path):
    from dh.config import Config
    cfg = Config({"sources": {"adapters": {"hardverapro": {"enabled": False}, "bazos_cz": {"enabled": True, "pages": 3}}}})
    reg = registry(cfg)
    assert "hardverapro" not in reg and "marktplaats" in reg
    assert reg["bazos_cz"].pages == 3


def test_seller_hash_hmac_and_salt_file(tmp_path, monkeypatch):
    monkeypatch.delenv("DH_SALT_PATH")
    base._salt_cache.clear()
    root = tmp_path / "repo"
    salt = get_salt(root)
    p = root / "config/local/secret_salt"
    assert p.is_file() and stat.S_IMODE(p.stat().st_mode) == 0o600 and len(salt) >= 32
    assert get_salt(root) == salt  # persisted, not regenerated
    h = seller_hash("kleinanzeigen", 123, salt)
    assert re.fullmatch(r"[0-9a-f]{64}", h)
    assert h != seller_hash("marktplaats", 123, salt) and h != seller_hash("kleinanzeigen", 123, b"other")
    assert seller_hash("x", None, salt) is None and seller_hash("x", "", salt) is None
    assert "config/local/" in (ROOT / ".gitignore").read_text()


class _FakeStore:
    def __init__(self, *a, **k):
        import sqlite3
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("create table health (source, kind, detail)")

    def add_health(self, s, k, d):
        self.conn.execute("insert into health values (?,?,?)", (s, k, d))

    def close(self):
        pass


class _FakeFetcher:
    html = ""
    status = 200

    def __init__(self, *a, **k):
        self.urls = []

    async def get(self, url, *, kind="html", headers=None):
        from helpers import Res
        self.urls.append(url)
        if "/oferta/" in url and self.status == 200:  # detail page of the allegro_lokalnie fixture
            return Res(200, fixture_text("allegro_lokalnie", "detail.html"), url)
        return Res(self.status, self.html, url)

    async def aclose(self):
        pass


def _run_probe(monkeypatch, source, html, status=200, **kw):
    monkeypatch.setattr(probe, "Fetcher", _FakeFetcher)
    monkeypatch.setattr(probe, "Store", _FakeStore)
    _FakeFetcher.html, _FakeFetcher.status = html, status
    out = io.StringIO()
    rc = asyncio.run(probe.probe(source, "rtx 4090", out=out, **kw))
    return rc, out.getvalue()


def test_probe_prints_listings(monkeypatch):
    rc, out = _run_probe(monkeypatch, "allegro_lokalnie", fixture_text("allegro_lokalnie", "search.html"), limit=3)
    lines = out.strip().splitlines()
    assert rc == 0 and re.match(r"allegro_lokalnie: \d+ listing\(s\) for 'rtx 4090'", lines[0])
    assert len(lines) == 4 and re.search(r"\| \d+(\.\d+)? PLN \| https://allegrolokalnie\.pl/oferta/", lines[1])


def test_probe_detail_and_alive(monkeypatch):
    rc, out = _run_probe(monkeypatch, "allegro_lokalnie", fixture_text("allegro_lokalnie", "search.html"), limit=1,
                         detail=True, alive=True)
    assert rc == 0 and "detail: desc=" in out and "alive(" in out and "True" in out


def test_probe_reports_reason_when_empty_or_failed(monkeypatch):
    rc, out = _run_probe(monkeypatch, "marktplaats", "<html>boom</html>", status=500)
    assert rc == 1 and "0 listings; reason:" in out
    rc, out = _run_probe(monkeypatch, "hardverapro", "<html><body>no results</body></html>")
    assert rc == 0 and "0 listing(s)" in out and "reason:" in out
    rc, out = _run_probe(monkeypatch, "sbazar", "")
    assert rc == 2 and "dropped" in out and "robots" in out
    rc, out = _run_probe(monkeypatch, "nope", "")
    assert rc == 2 and "available:" in out


def test_cli_probe_subcommand_registered():
    from dh.cli import build_parser
    a = build_parser().parse_args(["probe", "--source", "marktplaats", "--query", "rtx 4090"])
    assert a.source == "marktplaats" and a.query == "rtx 4090" and callable(a.func)


def test_long_tail_dropped_not_registered():
    reg = registry(load(ROOT), ROOT)
    for name in ("willhaben", "olx_ro", "olx_bg", "olx_pt"):
        assert name not in reg and name in DROPPED
    assert "forbidden" in DROPPED["willhaben"]
