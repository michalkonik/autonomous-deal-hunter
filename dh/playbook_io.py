"""Playbook loader: playbook/items.yaml is a YAML list of items the improvement loop may edit through the gates."""
from __future__ import annotations

import copy
from pathlib import Path

import yaml

KINDS = {"query", "alias", "scam_kw", "prompt", "cadence"}
_DEFAULTS = {"provenance": "seed", "helpful": 0, "harmful": 0, "sentinel": False}
_REQUIRED = ("id", "kind", "target", "value")


_CACHE: dict[Path, tuple[int, list[dict]]] = {}


def load_items(root: Path) -> list[dict]:
    """Items of <root>/playbook/items.yaml, defaults filled in. Missing file -> []. Bad shape -> ValueError."""
    p = Path(root) / "playbook" / "items.yaml"
    if not p.is_file():
        return []
    mtime = p.stat().st_mtime_ns
    hit = _CACHE.get(p)
    if hit and hit[0] == mtime:  # parsed once per file version; callers get copies
        return copy.deepcopy(hit[1])
    raw = yaml.safe_load(p.read_text()) or []
    if not isinstance(raw, list):
        raise ValueError(f"{p}: expected a YAML list")
    out, seen = [], set()
    for it in raw:
        if not isinstance(it, dict):
            raise ValueError(f"{p}: item is not a mapping: {it!r}")
        missing = [k for k in _REQUIRED if k not in it]
        if missing:
            raise ValueError(f"{p}: item {it.get('id')!r} missing {missing}")
        if it["kind"] not in KINDS:
            raise ValueError(f"{p}: item {it['id']!r} has unknown kind {it['kind']!r}")
        if it["id"] in seen:
            raise ValueError(f"{p}: duplicate id {it['id']!r}")
        seen.add(it["id"])
        out.append({**_DEFAULTS, **it})
    _CACHE[p] = (mtime, out)
    return copy.deepcopy(out)


def get_item(root: Path, item_id: str) -> dict:
    for it in load_items(root):
        if it["id"] == item_id:
            return it
    raise KeyError(item_id)
