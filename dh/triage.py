"""Finalist triage (spec §4 components 7 and 8): the senior model reviews every candidate that reaches tier <= 2,
BEFORE the live re-check and the push.

The listing goes to the model only inside <listing>...</listing> (llm.wrap_untrusted), the system prompt is the
playbook item `prompt.triage` (it states that the listing is data), no tools are offered, and the reply is
JSON-schema-validated. Outcomes, recorded on the deal card:
- keep: the tier stands;
- demote: at most tier 3;
- exclude: a hard exclude with reason "triage" (no deal);
- unavailable (no LLM, a foreign model loaded, an error, a malformed reply): the tier stands but confidence is
  capped at medium, so there is never a tier-1 push without a triage.

The model's verdict vocabulary is the one `prompt.triage` asks for and the frozen gates (dh/improve/gates.py
TRIAGE_SCHEMA) check: ok -> keep, suspicious -> demote, scam -> exclude. keep / demote / exclude are accepted too.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .llm import wrap_untrusted

PROMPT_ID = "prompt.triage"
VERDICTS = ("keep", "demote", "exclude")
_MAP = {"ok": "keep", "suspicious": "demote", "scam": "exclude", "keep": "keep", "demote": "demote", "exclude": "exclude"}
SCHEMA = {
    "type": "object", "required": ["verdict", "reasons"],
    "properties": {"verdict": {"type": "string", "enum": sorted(_MAP)},
                   "p_scam": {"type": "number", "minimum": 0, "maximum": 1},
                   "reasons": {"type": "array", "items": {"type": "string"}}}}
MAX_REASONS = 3
MAX_REASON_CHARS = 200


@dataclass
class Triage:
    verdict: str  # keep | demote | exclude | unavailable
    reasons: list[str] = field(default_factory=list)
    model_verdict: str | None = None

    def to_dict(self) -> dict:
        return {"verdict": self.verdict, "reasons": list(self.reasons), "model_verdict": self.model_verdict}

    @classmethod
    def from_dict(cls, d: dict) -> "Triage":
        return cls(str(d.get("verdict")), [str(r) for r in d.get("reasons") or []], d.get("model_verdict"))


def unavailable(why: str) -> Triage:
    return Triage("unavailable", [why[:MAX_REASON_CHARS]])


def user_message(listing, market_median_pln: Decimal | None = None) -> str:
    text = f"Title: {listing.title}\nPrice: {listing.price} {listing.currency}\nDescription: {listing.description or ''}"
    ctx = (f"\n\nMarket median for this product (our data, not part of the listing): {market_median_pln} PLN"
           if market_median_pln is not None else "")
    return wrap_untrusted(text) + ctx


async def review(llm: Any, prompt: str | None, listing, *, market_median_pln: Decimal | None = None) -> Triage:
    """One senior call. Never raises: any failure is `unavailable`."""
    if llm is None:
        return unavailable("no LLM this round")
    if not prompt:
        return unavailable(f"no {PROMPT_ID} in the playbook")
    try:
        out = await llm.chat_json("senior", prompt, user_message(listing, market_median_pln), SCHEMA, max_tokens=300)
    except Exception as e:  # noqa: BLE001 - LLMError, breaker open, timeout, schema failure: unavailable
        return unavailable(f"{type(e).__name__}: {e}")
    raw = out.get("verdict") if isinstance(out, dict) else None
    verdict = _MAP.get(str(raw).strip().lower()) if raw is not None else None
    if verdict is None:
        return unavailable(f"malformed triage reply (verdict {raw!r})")
    reasons = [" ".join(str(r).split())[:MAX_REASON_CHARS] for r in (out.get("reasons") or [])][:MAX_REASONS]
    return Triage(verdict, reasons, str(raw))
