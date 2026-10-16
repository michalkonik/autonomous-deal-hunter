import shutil
from pathlib import Path

import pytest
import yaml

from dh import config
from dh.cli import build_parser, COMMANDS

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def root(tmp_path):
    shutil.copytree(REPO / "config", tmp_path / "config", ignore=shutil.ignore_patterns("local"))
    return tmp_path


def test_defaults_load(root):
    cfg = config.load(root)
    assert cfg.rules.floors.easy == 50
    assert cfg.rules.star_usd == 500
    assert cfg.rules.capital_cap_usd == 30000
    assert cfg.economics.exit_channels.enabled == ["allegro", "olx", "ebay_pl", "ebay_de"]
    assert cfg.models.roles.worker == "gemma-4-26b-a4b-pool"
    assert cfg.models.paid_api.enabled is False


def test_local_override_deep_merges(root):
    (root / "config/local").mkdir()
    (root / "config/local/rules.yaml").write_text(yaml.safe_dump({"floors": {"easy": 60}}))
    cfg = config.load(root)
    assert cfg.rules.floors.easy == 60
    assert cfg.rules.floors.moderate == 120
    assert cfg.rules.floors.heavy == 250
    assert cfg.rules.capital_cap_usd == 30000


def test_frozen_digest_detects_change(root):
    (root / "evals").mkdir()
    (root / "evals/a.json").write_text("{}")
    cfg = config.load(root)
    d = cfg.frozen_digest()
    assert "config/rules.yaml" in d and "evals/a.json" in d
    assert "dh/economics.py" not in d  # missing files skipped
    config.assert_frozen_unchanged(cfg, d)
    (root / "config/rules.yaml").write_text((root / "config/rules.yaml").read_text() + "\n# x\n")
    with pytest.raises(config.FrozenChanged):
        config.assert_frozen_unchanged(cfg, d)


def test_frozen_detects_new_frozen_file(root):
    cfg = config.load(root)
    d = cfg.frozen_digest()
    (root / "dh").mkdir()
    (root / "dh/tiering.py").write_text("x=1")
    with pytest.raises(config.FrozenChanged):
        config.assert_frozen_unchanged(cfg, d)


def test_cli_lists_subcommands(capsys):
    for c in COMMANDS:
        argv = [c, "1"] if c in ("show", "verify") else [c]
        assert build_parser().parse_args(argv).command == c
    from dh.cli import main
    assert main(["replay"]) == 0  # later milestone: still a stub
    assert "not implemented" in capsys.readouterr().out


def test_pickup_single_source(root):
    cfg = config.load(root)
    assert cfg.pickup.max_km == 60
    assert cfg.pickup.pln_per_trip == 40
    assert "pickup" not in cfg.rules.to_dict()


def test_nested_config_keeps_root(root):
    cfg = config.load(root)
    assert cfg.rules.frozen_digest() == cfg.frozen_digest()
    assert cfg["rules"].floors.frozen_digest() == cfg.frozen_digest()
    with pytest.raises(RuntimeError):
        config.Config({}).frozen_digest()


def test_local_frozen_override_edit_detected(root):
    (root / "config/local").mkdir()
    (root / "config/local/rules.yaml").write_text(yaml.safe_dump({"floors": {"easy": 60}}))
    cfg = config.load(root)
    d = cfg.frozen_digest()
    assert "config/local/rules.yaml" in d
    (root / "config/local/rules.yaml").write_text(yaml.safe_dump({"floors": {"easy": 1}}))
    with pytest.raises(config.FrozenChanged):
        config.assert_frozen_unchanged(cfg, d)


def test_local_economics_override_created_midrun_detected(root):
    cfg = config.load(root)
    d = cfg.frozen_digest()
    (root / "config/local").mkdir()
    (root / "config/local/economics.yaml").write_text("fx_spread: 0\n")
    with pytest.raises(config.FrozenChanged):
        config.assert_frozen_unchanged(cfg, d)


def test_frozen_paths_cover_data_and_customs():
    for p in ("data/fees.yaml", "data/shipping.yaml", "dh/customs.py"):
        assert p in config.FROZEN_PATHS
