"""Risk and scam scoring (spec §8): hard excludes, a logistic p_scam, and a safe-buying checklist.

The off-platform contact patterns and the hard-exclude rules are code (frozen behaviour); the soft scam
keyword lists are playbook items (kind scam_kw, target risk); the logistic weights are in
config/economics.yaml under `risk` (frozen).
"""
from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .models import Extraction, Listing
from .playbook_io import load_items

_ROOT = Path(__file__).resolve().parent.parent

# Off-platform contact cues (hard exclude). Matched on the title and description.
_PHONE = re.compile(
    r"(?<![\d.,])(?:(?:\+|00)\d{1,3}[\s.-]?)?\d{3}[\s.-]\d{3}[\s.-]\d{3}(?![\d])"   # 123 456 789, +48 123-456-789
    r"|(?<![\d.,])\+\d{1,3}[\s.-]?\d(?:[\s.-]?\d){7,11}(?!\d)"                         # +491701234567
    r"|(?<![\d.,])\d{9}(?!\d)")                                                          # 123456789
_WHATSAPP = re.compile(r"\bwh?ats?\s?-?app\b|\bwhatsap\b|\bwa\.me\b", re.I)
_TELEGRAM = re.compile(r"\btelegram\b|\bt\.me/", re.I)
_URL = re.compile(r"https?://|\bwww\.", re.I)
_PAYPAL_FF = re.compile(r"paypal.{0,30}(friends|family|freunde|znajomi|rodzin)|(friends|family|freunde|znajomi|rodzin).{0,30}paypal", re.I)
_TRANSFER = re.compile(r"przelew\w*\s+na\s+konto")

_FLAGSHIP_GPU = re.compile(r"^gpu:rtx-(\d{4})")
_STOCK_PHOTO_FLAGSHIP = {"4090", "5090"}


@dataclass
class Risk:
    p_scam: float
    hard_exclude: bool
    flags: list[str] = field(default_factory=list)
    checklist: list[str] = field(default_factory=list)


def _norm(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "").lower().replace("ł", "l").replace("ß", "ss")
    return "".join(c for c in unicodedata.normalize("NFKD", t) if not unicodedata.combining(c))


def _gpu_model(key: str | None) -> int | None:
    m = _FLAGSHIP_GPU.match(key or "")
    return int(m.group(1)) if m else None


def _price_z(listing: Listing, estimate: Any, price_pln: Decimal | None, sigma_floor: float) -> float | None:
    """Robust z-score of ln(price) against ln(median); sigma from the P25..P75 spread (IQR / 1.349)."""
    price = price_pln if price_pln is not None else (listing.price if listing.currency.upper() == "PLN" else None)
    med = getattr(estimate, "median_pln", None)
    if estimate is None or price is None or not med or price <= 0 or med <= 0:
        return None
    sigma = sigma_floor
    p25, p75 = getattr(estimate, "p25_pln", None), getattr(estimate, "p75_pln", None)
    if p25 and p75 and p25 > 0 and p75 > p25:
        sigma = max(sigma, math.log(float(p75) / float(p25)) / 1.349)
    return (math.log(float(price)) - math.log(float(med))) / sigma


def _offplatform(text: str) -> list[str]:
    out = []
    for flag, rx in (("offplatform_phone", _PHONE), ("offplatform_whatsapp", _WHATSAPP),
                     ("offplatform_telegram", _TELEGRAM), ("offplatform_url", _URL),
                     ("offplatform_paypal_ff", _PAYPAL_FF)):
        if rx.search(text):
            out.append(flag)
    return out


def _checklist(extraction: Extraction, listing: Listing, flags: list[str]) -> list[str]:
    items = ["Pay only through the platform's own payment or buyer protection; never transfer outside it.",
             "Never move the chat to WhatsApp, Telegram or e-mail, and never open links the seller sends."]
    cat = extraction.category or ""
    if cat.startswith("gpu"):
        items += ["At pickup run GPU-Z and check the device ID matches the claimed model.",
                  "Run a VRAM test (for example OCCT or memtestG80) and a short load test.",
                  "Inspect the 12V-2x6 / power connector for melting or burn marks.",
                  "Ask for photos of the die side and the serial number before paying."]
    elif cat.startswith("ram"):
        items += ["At pickup run MemTest86 or check the SPD and capacity with CPU-Z.",
                  "Ask for a photo of the module label and serial number."]
    elif cat.startswith("ssd"):
        items += ["At pickup check capacity and SMART health (CrystalDiskInfo) and run a quick write test.",
                  "Ask for a photo of the label and serial number."]
    else:
        items += ["Ask for photos of the item and its serial number before paying."]
    if "too_cheap" in flags:
        items.append("The price is far below the market: assume a scam until a live video call proves the item.")
    if "new_account" in flags or "no_ratings" in flags:
        items.append("Seller account is new or unrated: insist on pickup with cash only after testing.")
    return items


def assess(listing: Listing, extraction: Extraction, estimate: Any, cfg: Any = None, *,
           price_pln: Decimal | None = None, stock_photo: bool = False, root: Path | None = None) -> Risk:
    """Hard excludes, p_scam in 0..1, flags and a safe-buying checklist.

    `cfg` is the root Config (default: loaded from the repo). `estimate` is an ExitEstimate (or None);
    `price_pln` is the listing price converted to PLN when the listing is not in PLN; `stock_photo` is the
    M2 photo-check hint; `root` is where playbook/items.yaml lives.
    """
    if cfg is None:
        from . import config as _config
        cfg = _config.load(_ROOT)
    rk = cfg.economics.risk
    w = rk.weights
    root = Path(root) if root is not None else (cfg._root or _ROOT)
    text = f"{listing.title}\n{listing.description or ''}"
    ntext = _norm(text)
    flags: list[str] = []
    new_age = int(rk.new_account_days)
    new_account = listing.seller_age_days is not None and listing.seller_age_days < new_age

    # ------------------------------------------------------------ hard excludes
    hard: list[str] = []
    if extraction.box_only:
        hard.append("box_only")
    if extraction.wanted_ad:
        hard.append("wanted_ad")
    if extraction.for_parts and not cfg.rules.repair.enabled:
        hard.append("for_parts")
    hard += _offplatform(text)
    if _TRANSFER.search(ntext) and new_account:
        hard.append("transfer_new_account")
    flags += hard

    # ------------------------------------------------------------ logistic features
    z = _price_z(listing, estimate, price_pln, float(rk.z_sigma_floor))
    feats: dict[str, float] = {}
    if z is not None and z < float(rk.z_threshold):
        feats["too_cheap"] = 1.0
        flags.append("too_cheap")
    if new_account:
        feats["new_account"] = 1.0
        flags.append("new_account")
    if listing.seller_ratings == 0:
        feats["no_ratings"] = 1.0
        flags.append("no_ratings")
    if stock_photo:
        feats["stock_photo"] = 1.0
        flags.append("stock_photo")
    model = _gpu_model(extraction.key)
    if model is not None and model >= 4080 and (listing.country or "").upper() in {"CN", "HK"}:
        feats["origin_cn_hk"] = 1.0
        flags.append("origin_cn_hk")
    if model is not None and str(model) in _STOCK_PHOTO_FLAGSHIP and not listing.photos:
        feats["no_photos_flagship"] = 1.0
        flags.append("no_photos_flagship")
    hits = []
    for it in load_items(root):
        if it["kind"] == "scam_kw" and it["target"] == "risk" and _norm(str(it["value"])) in ntext:
            hits.append(it["id"])
    if hits:
        feats["scam_kw"] = float(min(len(hits), int(rk.scam_kw_max_hits)))
        flags += hits

    x = float(w.bias) + sum(float(getattr(w, name)) * v for name, v in feats.items())
    p = 1.0 / (1.0 + math.exp(-x))
    return Risk(p_scam=p, hard_exclude=bool(hard), flags=flags, checklist=_checklist(extraction, listing, flags))
