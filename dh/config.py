"""Config loader: config/*.yaml deep-merged with config/local/*.yaml, plus frozen-layer guard."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml

FROZEN_PATHS = [
    "config/rules.yaml",
    "config/economics.yaml",
    "dh/economics.py",
    "dh/customs.py",
    "data/fees.yaml",
    "data/shipping.yaml",
    "dh/tiering.py",
    "dh/improve/gates.py",
    "evals",
]

# Local overrides of frozen config files are part of the frozen layer too.
LOCAL_FROZEN_PATHS = ["config/local/rules.yaml", "config/local/economics.yaml"]


class FrozenChanged(RuntimeError):
    pass


class Config:
    """Dict wrapper with attribute access."""

    def __init__(self, data: dict[str, Any], root: Path | None = None):
        object.__setattr__(self, "_data", data)
        object.__setattr__(self, "_root", root)

    def __getattr__(self, name: str) -> Any:
        try:
            v = self._data[name]
        except KeyError:
            raise AttributeError(name) from None
        return Config(v, self._root) if isinstance(v, dict) else v

    def __getitem__(self, name: str) -> Any:
        v = self._data[name]
        return Config(v, self._root) if isinstance(v, dict) else v

    def get(self, name: str, default: Any = None) -> Any:
        return self[name] if name in self._data else default

    def to_dict(self) -> dict[str, Any]:
        return self._data

    def frozen_digest(self) -> dict[str, str]:
        if self._root is None:
            raise RuntimeError("frozen_digest needs a Config created by config.load()")
        return frozen_digest(self._root)


def deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _read_yaml(p: Path) -> dict:
    return yaml.safe_load(p.read_text()) or {}


def load(root: Path) -> Config:
    root = Path(root)
    data: dict[str, Any] = {}
    for f in sorted((root / "config").glob("*.yaml")):
        data[f.stem] = _read_yaml(f)
    local = root / "config" / "local"
    if local.is_dir():
        for f in sorted(local.glob("*.yaml")):
            data[f.stem] = deep_merge(data.get(f.stem, {}), _read_yaml(f))
    return Config(data, root)


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def frozen_digest(root: Path) -> dict[str, str]:
    """SHA-256 of each existing frozen file; directories are hashed per file. Missing files are skipped."""
    root = Path(root)
    out: dict[str, str] = {}
    for rel in FROZEN_PATHS:
        p = root / rel
        if p.is_dir():
            for f in sorted(x for x in p.rglob("*") if x.is_file() and "__pycache__" not in x.parts):
                out[f.relative_to(root).as_posix()] = _sha(f)
        elif p.is_file():
            out[rel] = _sha(p)
    for rel in LOCAL_FROZEN_PATHS:
        p = root / rel
        if p.is_file():
            out[rel] = _sha(p)
    return out


def assert_frozen_unchanged(cfg: Config, digest: dict[str, str]) -> None:
    now = cfg.frozen_digest()
    changed = sorted(k for k in set(now) | set(digest) if now.get(k) != digest.get(k))
    if changed:
        raise FrozenChanged(f"frozen files changed: {', '.join(changed)}")
