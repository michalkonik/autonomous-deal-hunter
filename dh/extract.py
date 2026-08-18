"""LLM product extraction for titles the rules matcher is not certain about.

Guards: the listing goes to the model only inside one <listing> block (llm.wrap_untrusted), the system prompt says it
is data, the reply is JSON-schema validated, and the key is re-validated against the taxonomy (unknown -> None).
Rules flags are sticky: the LLM can never clear box_only / wanted_ad / for_parts / defects found by the rules.
"""
from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

from . import taxonomy
from .llm import Client, LLMError, wrap_untrusted
from .models import Extraction, Listing
from .playbook_io import get_item

ROOT = Path(__file__).resolve().parent.parent
RULES_CERTAIN = 0.9
LLM_MAX_CERTAINTY = 0.85  # an LLM-only mapping never reaches the 0.9 "high confidence" bar
BUNDLE_MAX_CERTAINTY = 0.5  # same cap the rules matcher applies to bundles

CONDITIONS = ["new", "like_new", "used", "for_parts", "unknown"]
EXTRACT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["key", "certainty", "condition", "defects", "box_only", "for_parts", "wanted_ad", "bundle_items"],
    "properties": {
        "key": {"type": ["string", "null"]},
        "certainty": {"type": "number", "minimum": 0, "maximum": 1},
        "condition": {"type": "string", "enum": CONDITIONS},
        "defects": {"type": "array", "items": {"type": "string"}},
        "box_only": {"type": "boolean"},
        "for_parts": {"type": "boolean"},
        "wanted_ad": {"type": "boolean"},
        "bundle_items": {"type": "array", "items": {"type": "string"}},
    },
}


CONFIRM_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["same", "key", "certainty"],
    "properties": {
        "same": {"type": "boolean"},
        "key": {"type": ["string", "null"]},
        "certainty": {"type": "number", "minimum": 0, "maximum": 1},
    },
}
CONFIRM_MAX_CERTAINTY = 0.95  # a key confirmed by the independent senior pass


def valid_key(key) -> tuple[str, str] | None:
    """(key, category) when `key` is exactly a canonical taxonomy key, else None."""
    if not isinstance(key, str):
        return None
    k = key.strip().lower()
    parts = k.split(":")
    try:
        cat = parts[0]
        if cat in taxonomy.GPU_CATEGORIES and len(parts) == 3:
            canon = taxonomy.canonical(cat, {"model": parts[1], "vram": int(parts[2])})
        elif cat in taxonomy.RAM_CATEGORIES and len(parts) == 4:
            canon = taxonomy.canonical(cat, {"type": parts[1], "total_gb": int(parts[2]), "kit": parts[3]})
        elif cat == "ssd" and len(parts) == 3:
            canon = taxonomy.canonical("ssd", {"iface": parts[1], "capacity": parts[2]})
        else:
            return None
    except (ValueError, TypeError):
        return None
    return (canon, cat) if canon == k else None


def _short(s: str) -> str:
    return re.sub(r"\s+", " ", str(s)).strip()[:80]


async def _confirm(listing: Listing, key: str, client: Client, root: Path) -> dict:
    system = get_item(root, "prompt.confirm")["value"]
    user = wrap_untrusted(f"Title: {listing.title}\nDescription: {listing.description}")
    return await client.chat_json("senior", system, f"{user}\n\nCandidate key: {key}", CONFIRM_SCHEMA, max_tokens=300)


async def extract(listing: Listing, rules: Extraction, client: Client, root: Path = ROOT) -> Extraction:
    """`rules` unchanged when its certainty >= 0.9, or when the rules already saw a box-only / wanted ad.

    Otherwise ask the worker and merge with the rules flags. An LLM key with raw certainty >= 0.85 must be confirmed by
    an independent second pass with the senior model (prompt.confirm): same key -> min(c1, c2, 0.95), anything else ->
    unmapped. Without that confirmation LLM-only certainty stays capped at 0.85. When the LLM key equals the rules
    key the certainty is max(rules, llm): two independent matchers agree, so the stronger evidence stands.
    If the LLM is unreachable or keeps returning invalid output, the rules result comes back with the note
    "llm_unavailable" (method stays "rules").
    """
    if rules.certainty >= RULES_CERTAIN or rules.box_only or rules.wanted_ad:
        return rules
    system = get_item(root, "prompt.extract")["value"]
    user = wrap_untrusted(f"Title: {listing.title}\nDescription: {listing.description}")
    try:
        out = await client.chat_json("worker", system, user, EXTRACT_SCHEMA)
    except LLMError:
        return replace(rules, notes=[*rules.notes, "llm_unavailable"])

    box_only = rules.box_only or out["box_only"]
    wanted = rules.wanted_ad or out["wanted_ad"]
    for_parts = rules.for_parts or out["for_parts"]
    defects = sorted(set(rules.defects) | {_short(d) for d in out["defects"] if _short(d)})
    bundle = sorted(set(rules.bundle_items) | {_short(b).lower() for b in out["bundle_items"] if _short(b)})
    condition = "for_parts" if for_parts else (out["condition"] if out["condition"] != "unknown" else rules.condition)
    notes = list(rules.notes)

    key = cat = None
    cert = 0.0
    vk = valid_key(out["key"])
    if vk and rules.key and rules.key != vk[0]:
        vk = None  # the two matchers disagree: leave it unmapped rather than pick one
    if vk and not (box_only or wanted):
        raw = float(out["certainty"])
        cert = min(raw, LLM_MAX_CERTAINTY)
        if raw >= LLM_MAX_CERTAINTY:
            try:
                c2 = await _confirm(listing, vk[0], client, root)
            except LLMError:
                notes.append("confirm_unavailable")
            else:
                second = valid_key(c2["key"]) if c2["key"] else None
                if c2["same"] and (second is None or second[0] == vk[0]):
                    cert = min(raw, float(c2["certainty"]), CONFIRM_MAX_CERTAINTY)
                else:
                    vk = None
        if vk:
            key, cat = vk
            if bundle:
                cert = min(cert, BUNDLE_MAX_CERTAINTY)
            if rules.key == key:
                cert = max(cert, rules.certainty)
        else:
            cert = 0.0
    return Extraction(key=key, category=cat, certainty=cert, condition=condition, defects=defects,
                      box_only=box_only, for_parts=for_parts, wanted_ad=wanted, bundle_items=bundle, method="llm",
                      notes=notes)
