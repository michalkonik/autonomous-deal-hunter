"""llama-swap (OpenAI-compatible) JSON client with schema validation, retries and a prompt-injection guard."""
from __future__ import annotations

import json
import logging
import math
import re
import time
from typing import Any, Callable

import httpx

MAX_UNTRUSTED_CHARS = 4000
_TAG = re.compile(r"<\s*/?\s*listing\b[^>]*>?", re.I)
_JSON_ONLY = "\n\nReply with a single JSON object only, no prose and no code fences."


log = logging.getLogger(__name__)
MAX_TOKENS_CAP = 1600


class LLMError(RuntimeError):
    pass


class LLMUnavailable(LLMError):
    """The server is unreachable; raised immediately while the circuit breaker is open."""


class PaidCapReached(LLMUnavailable):
    """A paid-endpoint role was asked for while paid use is off (cap 0) or the month's cap is spent."""


def wrap_untrusted(text: str) -> str:
    """Strip any listing tags from untrusted text, truncate to 4000 chars and wrap it in ONE <listing> block."""
    t = text or ""
    while True:  # loop so that "<<listing>/listing>"-style splicing cannot reassemble a tag
        n = _TAG.sub("", t)
        if n == t:
            break
        t = n
    return f"<listing>\n{t[:MAX_UNTRUSTED_CHARS]}\n</listing>"


_TYPES = {"string": str, "boolean": bool, "array": list, "object": dict}


def _type_ok(v: Any, t: str) -> bool:
    if t == "null":
        return v is None
    if t == "number":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    if t == "integer":
        return isinstance(v, int) and not isinstance(v, bool)
    return isinstance(v, _TYPES[t])


def validate(value: Any, schema: dict, path: str = "$") -> None:
    """Minimal JSON-schema subset: type, enum, required, properties, additionalProperties:false, items, min/max."""
    types = schema.get("type")
    if types is not None:
        types = [types] if isinstance(types, str) else types
        if not any(_type_ok(value, t) for t in types):
            raise ValueError(f"{path}: expected {types}, got {type(value).__name__}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path}: {value!r} not in {schema['enum']}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(value):
            raise ValueError(f"{path}: non-finite number")
        if "minimum" in schema and value < schema["minimum"]:
            raise ValueError(f"{path}: below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            raise ValueError(f"{path}: above maximum")
    if isinstance(value, dict):
        for k in schema.get("required", []):
            if k not in value:
                raise ValueError(f"{path}: missing {k!r}")
        if schema.get("additionalProperties") is False:
            extra = sorted(set(value) - set(schema.get("properties", {})))
            if extra:
                raise ValueError(f"{path}: unexpected {extra}")
        for k, sub in schema.get("properties", {}).items():
            if k in value:
                validate(value[k], sub, f"{path}.{k}")
    if isinstance(value, list) and "items" in schema:
        for i, v in enumerate(value):
            validate(v, schema["items"], f"{path}[{i}]")


def _parse_json(text: str) -> Any:
    t = (text or "").strip()
    m = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", t, re.S)
    return json.loads(m.group(1) if m else t)


class Client:
    """chat_json(role, system, user, schema) against the role's model on cfg.models.base_url.

    cfg is a dh.config.Config (needs models.base_url and models.roles; optional models.timeouts / models.breaker).
    `transport` and `clock` are for tests. `on_usage(role, prompt_tokens, completion_tokens, seconds)` is called after
    every successful HTTP reply; its exceptions are logged, never raised.

    Failure bounds: connect timeout 5 s, read timeout 60 s. After `failures` (3) consecutive transport errors the
    client raises LLMUnavailable immediately for `cooldown_s` (120 s); one success resets the count.
    """

    def __init__(self, cfg, on_usage: Callable[[str, int, int, float], None] | None = None,
                 transport: httpx.AsyncBaseTransport | None = None, retries: int = 2,
                 clock: Callable[[], float] = time.monotonic,
                 paid_spend: Callable[[], float] | None = None,
                 on_paid: Callable[[str, float], None] | None = None):
        models = cfg.models
        pa = models.get("paid_api", {}) or {}
        pa = pa.to_dict() if hasattr(pa, "to_dict") else pa
        # paid roles: only with paid_api.enabled; each maps a role to a model on paid_api.base_url. The monthly cap
        # (USD, default 0 = no paid call is ever made) is checked before EVERY paid call against the books'
        # month-to-date paid spend (`paid_spend`) plus the not-yet-booked spend of this client (`paid_booked` clears what the books took).
        self.paid = dict(pa) if pa.get("enabled") else {}
        self.paid_roles = dict(self.paid.get("roles") or {})
        self._paid_spend, self._on_paid, self._paid_session = paid_spend, on_paid, 0.0
        self.base_url = str(models.base_url).rstrip("/")
        self.roles = dict(models.roles.to_dict() if hasattr(models.roles, "to_dict") else models.roles)
        to = models.get("timeouts", {}) or {}
        to = to.to_dict() if hasattr(to, "to_dict") else to
        br = models.get("breaker", {}) or {}
        br = br.to_dict() if hasattr(br, "to_dict") else br
        self.breaker_failures = int(br.get("failures", 3))
        self.breaker_cooldown = float(br.get("cooldown_s", 120))
        self.on_usage = on_usage
        self.retries = retries
        self._clock = clock
        self._fails = 0
        self._open_until = 0.0
        read = float(to.get("read_s", 60))
        self._http = httpx.AsyncClient(
            transport=transport, timeout=httpx.Timeout(connect=float(to.get("connect_s", 5)), read=read,
                                                       write=read, pool=read))

    async def aclose(self) -> None:
        await self._http.aclose()

    def _transport_failed(self, err: Exception) -> LLMUnavailable:
        self._fails += 1
        if self._fails >= self.breaker_failures:
            self._open_until = self._clock() + self.breaker_cooldown
        return LLMUnavailable(f"transport error: {type(err).__name__}: {err}")

    def paid_gate(self, role: str) -> None:
        """Raise PaidCapReached unless a paid call for `role` is within the monthly cap (USD)."""
        cap = float(self.paid.get("monthly_cap_usd", 0) or 0)
        if cap <= 0:
            raise PaidCapReached(f"role {role!r} uses the paid API but models.paid_api.monthly_cap_usd is 0")
        spent = (float(self._paid_spend()) if self._paid_spend else 0.0) + self._paid_session
        if spent >= cap:
            raise PaidCapReached(f"paid API cap reached: ${spent:.2f} of ${cap:.2f} this month")

    def paid_booked(self, usd: float) -> None:
        """The books recorded `usd` of this client's paid spend (a round's cost row): it is part of the month-to-date
        total now, so it leaves the unbooked session counter (never counted twice)."""
        self._paid_session = max(0.0, self._paid_session - float(usd))

    def _paid_headers(self) -> dict:
        ref = self.paid.get("api_key_secret")
        if not ref:
            return {}
        from .notify import bw_secret
        key = bw_secret(str(ref))
        return {"Authorization": f"Bearer {key}"} if key else {}

    async def _post(self, role: str, body: dict) -> tuple[str, str | None]:
        t0 = time.monotonic()
        paid = role in self.paid_roles
        url, headers = self.base_url, {}
        if paid:
            self.paid_gate(role)
            url, headers = str(self.paid["base_url"]).rstrip("/"), self._paid_headers()
        try:
            r = await self._http.post(f"{url}/chat/completions", json=body, headers=headers)
        except httpx.TransportError as e:  # connect/read timeouts, connection refused, ...
            raise self._transport_failed(e) from None
        self._fails = 0
        if r.status_code >= 400:
            raise _HTTPFail(r.status_code, r.text[:300])
        try:
            data = r.json()
            usage = data.get("usage") or {}
            choice = data["choices"][0]
            content, finish = choice["message"]["content"] or "", choice.get("finish_reason")
        except (ValueError, KeyError, IndexError, TypeError, AttributeError) as e:
            raise ValueError(f"malformed completion: {e}") from None
        if paid:
            usd = (int(usage.get("prompt_tokens", 0)) * float(self.paid.get("usd_per_mtok_in", 0))
                   + int(usage.get("completion_tokens", 0)) * float(self.paid.get("usd_per_mtok_out", 0))) / 1e6
            self._paid_session += usd
            if self._on_paid:
                try:
                    self._on_paid(role, usd)
                except Exception:  # noqa: BLE001
                    log.exception("on_paid callback failed")
        if self.on_usage:
            try:
                self.on_usage(role, int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0)),
                              time.monotonic() - t0)
            except Exception:  # noqa: BLE001 - accounting must never fail a call
                log.exception("on_usage callback failed")
        return content, finish

    async def chat_json(self, role: str, system: str, user: str, schema: dict, max_tokens: int = 600) -> dict:
        if role not in self.roles and role not in self.paid_roles:
            raise LLMError(f"unknown role {role!r}")
        if self._clock() < self._open_until:
            raise LLMUnavailable("circuit breaker open: LLM server failed repeatedly")
        model = self.paid_roles[role] if role in self.paid_roles else self.roles[role]
        last = ""
        strict = True  # per call: a 400/422 only drops response_format for this call
        length_retried = False
        attempts = 0
        while attempts <= self.retries:
            prompt = user if not last else (
                f"{user}\n\nYour previous reply was rejected: {last[:200]}. "
                "Reply again with one valid JSON object that follows the format.")
            body: dict[str, Any] = {
                "model": model, "max_tokens": max_tokens, "temperature": 0,
                # extraction needs no chain of thought; on reasoning models it would eat the token budget
                "chat_template_kwargs": {"enable_thinking": False},
                "messages": [{"role": "system", "content": system + ("" if strict else _JSON_ONLY)},
                             {"role": "user", "content": prompt}],
            }
            if strict:
                body["response_format"] = {"type": "json_schema",
                                           "json_schema": {"name": "reply", "strict": True, "schema": schema}}
            try:
                text, finish = await self._post(role, body)
            except _HTTPFail as e:
                if strict and e.status in (400, 422):  # fallback attempt is free: it does not use the retry budget
                    strict = False
                    continue
                raise LLMError(f"HTTP {e.status}: {e.body}") from None
            except ValueError as e:  # malformed completion envelope
                attempts += 1
                last = str(e)
                continue
            attempts += 1
            if finish == "length":
                last = "output was cut off (too long)"
                if not length_retried:
                    length_retried = True
                    max_tokens = min(max_tokens * 2, MAX_TOKENS_CAP)
                continue
            try:
                obj = _parse_json(text)
                if not isinstance(obj, dict):
                    raise ValueError("reply is not a JSON object")
                validate(obj, schema)
                return obj
            except ValueError as e:  # json.JSONDecodeError is a ValueError
                last = str(e)
        raise LLMError(f"no valid JSON from {model} after {self.retries + 1} attempts: {last}")


class _HTTPFail(Exception):
    def __init__(self, status: int, body: str):
        self.status, self.body = status, body
