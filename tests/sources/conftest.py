from __future__ import annotations

import pytest

from dh.sources import base


@pytest.fixture(autouse=True)
def _salt(tmp_path, monkeypatch):
    monkeypatch.setenv("DH_SALT_PATH", str(tmp_path / "secret_salt"))
    base._salt_cache.clear()
    yield
    base._salt_cache.clear()
