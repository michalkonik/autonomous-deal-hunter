import pytest


def pytest_addoption(parser):
    parser.addoption("--live", action="store_true", default=False, help="run tests marked live (real network)")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--live"):
        return
    skip = pytest.mark.skip(reason="live test: pass --live to run")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _hermetic_config(monkeypatch):
    """The suite must pass on a host where the repository has an owner's config/local/ (a role mapping, a unit
    name, a quick round interval): the repository's own config is loaded WITHOUT config/local, DH_ROOT is cleared
    and DH_UNIT_NAME is a dummy ("deal-hunter-test"). Tests that need local overrides build them under tmp_path."""
    from pathlib import Path

    from dh import config
    repo = Path(__file__).resolve().parents[1]
    real = config.load

    def load(root, *, local=True):
        return real(root, local=local and Path(root).resolve() != repo)
    monkeypatch.setattr(config, "load", load)
    monkeypatch.delenv("DH_ROOT", raising=False)
    # never the live unit's name: a test that reaches systemctl by mistake asks about a unit that does not exist
    monkeypatch.setenv("DH_UNIT_NAME", "deal-hunter-test")


@pytest.fixture(autouse=True)
def _no_password_manager(monkeypatch):
    """Tests never ask the real `bw-secret` (seconds per call on the rig, and the owner's vault): every secret is
    missing unless a test sets its own; the per-process eBay key cache starts empty in every test."""
    from dh import notify
    from dh.sources import ebay
    monkeypatch.setattr(notify, "bw_secret", lambda ref: None)
    monkeypatch.setattr(ebay, "_secrets", {})
