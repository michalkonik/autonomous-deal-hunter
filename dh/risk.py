"""Risk and scam scoring (spec §8): hard excludes, a logistic p_scam, and a safe-buying checklist.

The off-platform contact patterns and the hard-exclude rules are code (frozen behaviour); the soft scam
keyword lists are playbook items (kind scam_kw, target risk); the logistic weights are in
config/economics.yaml under `risk` (frozen).

Final review I3 (controller ruling; this frozen-behaviour change is authorised by it): a bare phone number or
e-mail address is NOT a hard exclude. On phone-first classifieds (Bazos, HardverApró, Sprzedajemy, Marktplaats)
every seller writes one. It is the scam-score feature `contact_in_text` (weight `risk.weights.contact_in_text`).
Hard excludes stay: WhatsApp / Telegram (also wa.me / t.me links), external links, PayPal Friends & Family,
and "przelew na konto" from a new account. The payment line of the checklist depends on the platform.
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

# Contact data in the text (the `contact_in_text` score feature, not an exclude). Matched on the title and description.
# A phone number needs phone-like formatting: a +CC / 00CC prefix, spaced or dashed 3-3-3 groups, or a bare
# 9-digit run right after a "tel"/"telefon"/"zadzwon"/"kontakt"/"call" cue. A bare digit run (serial, part
# number) does not match.
PHONE_RE = re.compile(
    r"(?<![\w.,])(?:\+|00)\d{1,3}[\s.-]?\d(?:[\s.-]?\d){7,11}(?!\d)"                 # +48 600123456, +49 170 1234567
    r"|(?<![\w.,])\d{3}[\s.-]\d{3}[\s.-]\d{3}(?![\d])"                              # 600 123 456, 600-123-456
    r"|\b(?:tel|telefon\w*|zadzwon|dzwon|kom|komorka|kontakt|call|anruf\w*|handy|mobil\w*|rufnummer|volejte|"
    r"hivj\w*|elerhetoseg\w*|phone)\W{0,12}(?:\d{9}(?!\d)"                       # tel 600123456
    r"|0\d(?:[\s./-]?\d){8,10}(?![\d]))", re.I)                                   # Handy 0176 1234 5678, tel 06 30 123 4567
_PHONE = PHONE_RE  # backwards-compatible alias
_WHATSAPP = re.compile(r"\bwh?ats?\s?-?app\b|\bwhatsap\b|\bwa\.me\b", re.I)
_TELEGRAM = re.compile(r"\btelegram\b|\bt\.me/", re.I)
_URL = re.compile(r"https?://|\bwww\.", re.I)
_PAYPAL_FF = re.compile(r"paypal.{0,30}(friends|family|freunde|znajomi|rodzin)|(friends|family|freunde|znajomi|rodzin).{0,30}paypal", re.I)
_TRANSFER = re.compile(r"przelew\w*\s+na\s+konto")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}\b", re.I)

# Payment advice per platform (I3 ruling). Platforms with a protected checkout: pay only through it (Allegro Lokalnie
# and Kleinanzeigen only when the item is shipped: their protection is a delivery service; eBay always). Phone-first
# classifieds, and a pickup where the protection does not apply: meet, test, pay only after testing.
_PROTECTED = {"allegro_lokalnie": "Allegro Lokalnie with delivery", "kleinanzeigen": "Kleinanzeigen \"Sicher bezahlen\"",
              "olx": "Przesyłka OLX", "allegro": "Allegro checkout"}
_PROTECTED_SHIPPED_ONLY = {"allegro_lokalnie", "kleinanzeigen", "olx"}
_PHONE_FIRST = ("bazos", "hardverapro", "sprzedajemy", "marktplaats")

_GPU_ID = re.compile(r"^gpu:([a-z0-9-]+):")
# Desktop GPU ids the CN/HK-origin rule and the missing-photo rule apply to (taxonomy ids, explicit on purpose).
_CN_HK_MODELS = frozenset({"rtx-4080", "rtx-4080-super", "rtx-4090", "rtx-4090d", "rtx-5080", "rtx-5090"})
_NO_PHOTO_MODELS = frozenset({"rtx-4090", "rtx-4090d", "rtx-5090", "rtx-5090d"})


@dataclass
class Risk:
    p_scam: float
    hard_exclude: bool
    flags: list[str] = field(default_factory=list)
    checklist: list[str] = field(default_factory=list)


def _norm(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "").lower().replace("ł", "l").replace("ß", "ss")
    return "".join(c for c in unicodedata.normalize("NFKD", t) if not unicodedata.combining(c))


_KW_CACHE: dict[Path, tuple[int, list[tuple[str, str]]]] = {}


def _scam_keywords(root: Path) -> list[tuple[str, str]]:
    """(id, normalised phrase) of the playbook's scam_kw/risk items, cached per items.yaml mtime."""
    p = Path(root) / "playbook" / "items.yaml"
    mtime = p.stat().st_mtime_ns if p.is_file() else -1
    hit = _KW_CACHE.get(p)
    if hit and hit[0] == mtime:
        return hit[1]
    kws = [(it["id"], _norm(str(it["value"]))) for it in load_items(root)
           if it["kind"] == "scam_kw" and it["target"] == "risk"]
    _KW_CACHE[p] = (mtime, kws)
    return kws


def _gpu_model(key: str | None) -> str | None:
    """Desktop GPU taxonomy id of a key (gpu-mobile keys never match)."""
    m = _GPU_ID.match(key or "")
    return m.group(1) if m else None


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
    """Hard-exclude cues: the seller moves the deal off the platform (messenger, link, PayPal F&F)."""
    out = []
    for flag, rx in (("offplatform_whatsapp", _WHATSAPP), ("offplatform_telegram", _TELEGRAM),
                     ("offplatform_url", _URL), ("offplatform_paypal_ff", _PAYPAL_FF)):
        if rx.search(text):
            out.append(flag)
    return out


def _contact(listing: Listing, ntext: str, text: str) -> bool:
    """A phone number or e-mail address in the text, or one the adapter redacted before storage."""
    return bool(_PHONE.search(ntext) or _EMAIL.search(text) or getattr(listing, "contact_in_text", False))


def payment_mode(listing: Listing) -> tuple[str, str | None]:
    """("protected", checkout name) or ("in_person", None) for the listing's platform."""
    src = (listing.source or "").lower()
    if src.startswith("ebay"):
        return "protected", "eBay checkout"
    if src.startswith(_PHONE_FIRST):
        return "in_person", None
    if src in _PROTECTED:
        shipped = not listing.pickup_only and listing.ships is not False
        if src in _PROTECTED_SHIPPED_ONLY and not shipped:
            return "in_person", None
        return "protected", _PROTECTED[src]
    return "protected", None  # shops, feeds, unknown: their own checkout


def _checklist(extraction: Extraction, listing: Listing, flags: list[str]) -> list[str]:
    cat = extraction.category or ""
    mode, name = payment_mode(listing)
    if mode == "protected":
        where = f" ({name})" if name else ""
        items = [f"Pay only through the platform's protected checkout{where}; never transfer money outside it.",
                 "Never move the chat to WhatsApp, Telegram or e-mail, and never open links the seller sends."]
    else:
        tests = " (GPU-Z, VRAM test)" if cat.startswith("gpu") else " (see the checks below)"
        items = [f"Meet in person and test before paying{tests}; pay cash or instant transfer only after testing; "
                 "never pay in advance, and never for shipping alone.",
                 "Calling the seller is normal on this platform, but never switch to WhatsApp or Telegram, never open "
                 "links the seller sends and never share codes or card details."]
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
        items.append("Seller account is new or unrated: test the item before paying, and pay only through the "
                     "platform's protected checkout." if mode == "protected" else
                     "Seller account is new or unrated: see the item working in person before any money changes hands.")
    if "contact_in_text" in flags and mode == "protected":
        items.append("The listing shows a phone number or e-mail: keep the conversation and the payment on the platform.")
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
    if (extraction.for_parts or extraction.condition == "for_parts") and not cfg.rules.repair.enabled:
        hard.append("for_parts")
    hard += _offplatform(ntext)  # normalised: lowercase, no diacritics
    if _TRANSFER.search(ntext) and new_account:
        hard.append("transfer_new_account")
    flags += hard

    # ------------------------------------------------------------ logistic features
    z = _price_z(listing, estimate, price_pln, float(rk.z_sigma_floor))
    feats: dict[str, float] = {}
    if z is not None and z < float(rk.z_threshold):
        feats["too_cheap"] = 1.0
        flags.append("too_cheap")
        price = price_pln if price_pln is not None else listing.price
        if price < Decimal(str(rk.extreme_price_frac)) * Decimal(str(estimate.median_pln)):
            feats["extreme_price"] = 1.0
            flags.append("extreme_price")
    if new_account:
        feats["new_account"] = 1.0
        flags.append("new_account")
    if _contact(listing, ntext, text):  # a phone or e-mail (or one the adapter redacted): a feature, not an exclude
        feats["contact_in_text"] = 1.0
        flags.append("contact_in_text")
    if listing.seller_ratings == 0:
        feats["no_ratings"] = 1.0
        flags.append("no_ratings")
    if stock_photo:
        feats["stock_photo"] = 1.0
        flags.append("stock_photo")
    model = _gpu_model(extraction.key)
    if model in _CN_HK_MODELS and (listing.country or "").upper() in {"CN", "HK"}:
        feats["origin_cn_hk"] = 1.0
        flags.append("origin_cn_hk")
    if model in _NO_PHOTO_MODELS and not listing.photos:
        feats["no_photos_flagship"] = 1.0
        flags.append("no_photos_flagship")
    hits = [iid for iid, needle in _scam_keywords(root) if needle in ntext]
    if hits:
        feats["scam_kw"] = float(min(len(hits), int(rk.scam_kw_max_hits)))
        flags += hits

    x = float(w.bias) + sum(float(getattr(w, name)) * v for name, v in feats.items())
    p = 1.0 / (1.0 + math.exp(-x))
    return Risk(p_scam=p, hard_exclude=bool(hard), flags=flags, checklist=_checklist(extraction, listing, flags))
