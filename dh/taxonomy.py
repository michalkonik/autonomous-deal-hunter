"""Product taxonomy: GPU, RAM and SSD catalogues (data/taxonomy/*.yaml) and canonical product keys.

Keys:
  gpu:<id>:<vram_gb>            gpu:rtx-4090:24, gpu:rx-7900-xtx:24
  gpu-mobile:<id>:<vram_gb>     gpu-mobile:rtx-4090:16   (laptop chips: never the desktop key)
  ram:<ddr4|ddr5>:<total>:<kit> ram:ddr5:32:2x16         (speed is not part of the key)
  ram-sodimm:<...>              same shape, laptop memory
  ssd:<nvme|sata>:<capacity>    ssd:nvme:2tb
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "taxonomy"
GPU_CATEGORIES = ("gpu", "gpu-mobile")
RAM_CATEGORIES = ("ram", "ram-sodimm")


@lru_cache(maxsize=None)
def _load(name: str) -> Any:
    return yaml.safe_load((DATA_DIR / f"{name}.yaml").read_text())


def gpu_entries(category: str = "gpu") -> list[dict]:
    if category not in GPU_CATEGORIES:
        raise ValueError(f"not a GPU category: {category!r}")
    return _load("gpu")[category]


def ram_config() -> dict:
    return _load("ram")


def ssd_config() -> dict:
    return _load("ssd")


def _gpu_key(category: str, fields: dict) -> str:
    model, vram = str(fields.get("model", "")).lower(), fields.get("vram")
    ent = next((e for e in gpu_entries(category) if e["id"] == model), None)
    if ent is None:
        raise ValueError(f"unknown {category} model {model!r}")
    if vram not in ent["vram"]:
        raise ValueError(f"{category}:{model} has no {vram} GB variant (has {ent['vram']})")
    return f"{category}:{model}:{vram}"


def _ram_key(category: str, fields: dict) -> str:
    cfg = ram_config()
    typ = str(fields.get("type", "")).lower()
    if typ not in cfg["types"]:
        raise ValueError(f"unsupported RAM type {typ!r}")
    m = re.fullmatch(r"(\d+)x(\d+)", str(fields.get("kit", "")).lower())
    if not m:
        raise ValueError(f"bad RAM kit {fields.get('kit')!r} (want e.g. 2x16)")
    count, size = int(m.group(1)), int(m.group(2))
    if count not in cfg["module_counts"] or size not in cfg["module_gb"]:
        raise ValueError(f"unsupported RAM kit {count}x{size}")
    total = int(fields.get("total_gb", 0))
    if total != count * size:
        raise ValueError(f"RAM total {total} != kit {count}x{size}")
    return f"{category}:{typ}:{total}:{count}x{size}"


def capacity_str(value: str | int | float | Decimal) -> str:
    """Normalise an SSD capacity ('2TB', '1000 GB', '3,84 tb') to '2tb', '1tb', '3.84tb', '512gb'."""
    cfg = ssd_config()
    m = re.fullmatch(r"\s*(\d+(?:[.,]\d+)?)\s*(tb|gb)?\s*", str(value).lower())
    if not m:
        raise ValueError(f"bad SSD capacity {value!r}")
    try:
        num = Decimal(m.group(1).replace(",", "."))
    except InvalidOperation:  # pragma: no cover - the regex already guarantees a number
        raise ValueError(f"bad SSD capacity {value!r}") from None
    unit = m.group(2) or "gb"
    gb = num * 1000 if unit == "tb" else num
    gb = {Decimal(1024): Decimal(1000), Decimal(2048): Decimal(2000), Decimal(4096): Decimal(4000)}.get(gb, gb)
    tb = gb / 1000
    if gb == gb.to_integral_value() and int(gb) in cfg["capacities_gb"]:
        g = int(gb)
        return f"{g // 1000}tb" if g >= 1000 and g % 1000 == 0 else f"{g}gb"
    tb_s = format(tb.normalize(), "f")
    if tb_s in cfg["capacities_tb_decimal"] or tb_s.rstrip("0") in cfg["capacities_tb_decimal"]:
        return f"{tb_s}tb"
    raise ValueError(f"unsupported SSD capacity {value!r}")


def _ssd_key(fields: dict) -> str:
    iface = str(fields.get("iface", "")).lower()
    if iface not in ssd_config()["interfaces"]:
        raise ValueError(f"unsupported SSD interface {iface!r}")
    return f"ssd:{iface}:{capacity_str(fields.get('capacity', ''))}"


def canonical(category: str, fields: dict) -> str:
    """Canonical product key for a category and its fields; ValueError if the combination does not exist."""
    if category in GPU_CATEGORIES:
        return _gpu_key(category, fields)
    if category in RAM_CATEGORIES:
        return _ram_key(category, fields)
    if category == "ssd":
        return _ssd_key(fields)
    raise ValueError(f"unknown category {category!r}")


_UPPER_TOKENS = {"rtx", "gtx", "rx", "xt", "xtx", "gre", "fe", "oc"}
_WORD_TOKENS = {"ti": "Ti", "super": "Super", "titan": "Titan", "arc": "Arc", "radeon": "Radeon", "pro": "Pro",
                "quadro": "Quadro", "tesla": "Tesla"}


def _model_name(model_id: str) -> str:
    out = []
    for tok in model_id.split("-"):
        if tok in _WORD_TOKENS:
            out.append(_WORD_TOKENS[tok])
        elif tok in _UPPER_TOKENS or any(c.isdigit() for c in tok):
            out.append(tok.upper())
        else:
            out.append(tok.capitalize())
    return " ".join(out)


def display_name(key: str | None) -> str:
    """A human name for a product key (push titles and bodies): gpu:rtx-4090:24 -> "RTX 4090 24GB",
    ram:ddr5:32:2x16 -> "DDR5 32GB (2x16GB)", ssd:nvme:2tb -> "NVMe SSD 2TB". Anything else is returned as is."""
    if not key:
        return ""
    parts = str(key).split(":")
    try:
        if parts[0] in GPU_CATEGORIES and len(parts) == 3:
            laptop = " Laptop" if parts[0] == "gpu-mobile" else ""
            return f"{_model_name(parts[1])}{laptop} {int(parts[2])}GB"
        if parts[0] in RAM_CATEGORIES and len(parts) == 4:
            count, size = parts[3].lower().split("x")
            so = " SO-DIMM" if parts[0] == "ram-sodimm" else ""
            return f"{parts[1].upper()}{so} {int(parts[2])}GB ({int(count)}x{int(size)}GB)"
        if parts[0] == "ssd" and len(parts) == 3:
            iface = {"nvme": "NVMe", "sata": "SATA"}.get(parts[1].lower(), parts[1].upper())
            return f"{iface} SSD {parts[2].upper()}"
        if str(key) == "pc:bundle":
            return "PC bundle"
    except ValueError:
        pass
    return str(key)
