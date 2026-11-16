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


class ProviderConflict(LLMError):
    """One role is listed on two remote providers. The call would not know which bill to charge."""


def _mapping(v: Any) -> dict:
    if v is None:
        return {}
    if hasattr(v, "to_dict"):
        v = v.to_dict()
    return dict(v) if isinstance(v, dict) else {}


class RemoteRoute:
    """One role on one provider. An API key stays in bw-secret. A grok session stays in ~/.grok/auth.json."""

    def __init__(self, provider: str, model: str, base_url: str, cap_usd: float, usd_in: float, usd_out: float,
                 api_key_secret: str | None, pln_per_usd: float, auth: str = "api_key", wire: str = "chat"):
        self.provider, self.model = provider, model
        self.base_url = base_url.rstrip("/")
        self.cap_usd, self.usd_in, self.usd_out = cap_usd, usd_in, usd_out
        self.api_key_secret, self.pln_per_usd = api_key_secret, pln_per_usd
        self.auth, self.wire = auth, wire


def responses_body(chat: dict) -> dict:
    """Chat-completions body -> xAI Responses body. One shot, no stored conversation."""
    messages = chat.get("messages") or []
    instructions, inp = "", []
    for msg in messages:
        if msg.get("role") == "system":
            instructions = str(msg.get("content") or "")
        else:
            inp.append({"role": msg.get("role") or "user", "content": str(msg.get("content") or "")})
    out: dict[str, Any] = {
        "model": chat["model"], "input": inp, "temperature": chat.get("temperature", 0),
        "max_output_tokens": chat.get("max_tokens", 600), "store": False,
        # Short JSON tasks. grok-4.7 defaults to high, which spends thousands of tokens per call.
        "reasoning": {"effort": "low"},
    }
    if instructions:
        out["instructions"] = instructions
    fmt = (chat.get("response_format") or {}).get("json_schema")
    if fmt:
        out["text"] = {"format": {"type": "json_schema", "name": fmt.get("name") or "reply",
                                  "strict": bool(fmt.get("strict", True)), "schema": fmt.get("schema") or {}}}
    return out


def responses_result(data: dict) -> tuple[str, str | None, dict]:
    """Responses JSON -> (text, finish_reason, usage with prompt_tokens and completion_tokens)."""
    texts: list[str] = []
    for item in data.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if isinstance(part, dict) and part.get("type") in ("output_text", "text") and part.get("text"):
                texts.append(str(part["text"]))
    if not texts and isinstance(data.get("output_text"), str):
        texts.append(data["output_text"])
    usage = data.get("usage") or {}
    norm = {
        "prompt_tokens": int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0),
        "completion_tokens": int(usage.get("output_tokens") or usage.get("completion_tokens") or 0),
    }
    finish = "length" if data.get("status") == "incomplete" else "stop"
    return "".join(texts), finish, norm


def provider_blocks(models) -> list[tuple[str, dict]]:
    """Enabled remote endpoints. `paid_api` is the original single endpoint and is included only when enabled.
    `providers.<name>` is any other OpenAI-compatible endpoint. `enabled: false` drops that block."""
    m = _mapping(models)
    out: list[tuple[str, dict]] = []
    paid = _mapping(m.get("paid_api"))
    if paid.get("enabled"):
        out.append(("paid_api", paid))
    for name in sorted(_mapping(m.get("providers"))):
        block = _mapping(_mapping(m.get("providers"))[name])
        if block.get("enabled") is False:
            continue
        out.append((str(name), block))
    return out


def remote_routes(models) -> dict[str, RemoteRoute]:
    """role -> route. A role on two providers raises ProviderConflict instead of picking one in silence."""
    routes: dict[str, RemoteRoute] = {}
    seen: dict[str, list[str]] = {}
    for name, block in provider_blocks(models):
        for role, model in _mapping(block.get("roles")).items():
            if not model:
                continue
            seen.setdefault(str(role), []).append(name)
            if str(role) in routes:
                continue
            routes[str(role)] = RemoteRoute(
                name, str(model), str(block.get("base_url") or ""),
                float(block.get("monthly_cap_usd") or 0), float(block.get("usd_per_mtok_in") or 0),
                float(block.get("usd_per_mtok_out") or 0),
                str(block["api_key_secret"]) if block.get("api_key_secret") else None,
                float(block.get("pln_per_usd") or 4.0),
                str(block.get("auth") or "api_key"), str(block.get("wire") or "chat"))
    dupes = {role: names for role, names in seen.items() if len(names) > 1}
    if dupes:
        detail = "; ".join(f"{role} on {', '.join(names)}" for role, names in sorted(dupes.items()))
        raise ProviderConflict(f"a role is on more than one provider: {detail}")
    return routes


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
        pa = _mapping(models.get("paid_api", {}) or {})
        # Remote roles: paid_api (the original one endpoint) plus models.providers (one block per provider).
        # Each provider has its own monthly cap. Cap 0 means no call to that provider leaves the machine.
        # The cap is checked before EVERY paid call against that provider's month-to-date books spend
        # (`paid_spend(provider)`, or a zero-arg callable for the original paid_api) plus this client's
        # not-yet-booked spend. `paid_booked` clears what the books took.
        self.paid = dict(pa) if pa.get("enabled") else {}
        self._routes = remote_routes(models)
        self.paid_roles = {role: route.model for role, route in self._routes.items()}
        self._paid_spend, self._on_paid, self._paid_session = paid_spend, on_paid, 0.0
        self._paid_session_by: dict[str, float] = {}
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

    def provider_of(self, role: str) -> str | None:
        route = self._routes.get(role)
        return route.provider if route else None

    def _month_spent(self, provider: str) -> float:
        fn = self._paid_spend
        if fn is None:
            return 0.0
        try:
            return float(fn(provider))
        except TypeError:
            # Callers written for the original single endpoint pass a zero-arg callable. It is the paid_api total.
            return float(fn()) if provider == "paid_api" else 0.0

    def paid_gate(self, role: str) -> None:
        """Raise PaidCapReached unless a paid call for `role` is within that provider's monthly cap (USD)."""
        route = self._routes[role]
        cap = float(route.cap_usd or 0)
        if cap <= 0:
            raise PaidCapReached(f"role {role!r} uses provider {route.provider!r} but its monthly_cap_usd is 0")
        spent = self._month_spent(route.provider) + self._paid_session_by.get(route.provider, 0.0)
        if spent >= cap:
            raise PaidCapReached(f"provider {route.provider} cap reached: ${spent:.2f} of ${cap:.2f} this month")

    def paid_booked(self, usd: float) -> None:
        """The books recorded `usd` of this client's paid spend (a round's cost rows): it is part of the
        month-to-date total now, so it leaves the unbooked session counters (never counted twice)."""
        self._paid_session = max(0.0, self._paid_session - float(usd))
        left = float(usd)
        for name in sorted(self._paid_session_by):
            take = min(self._paid_session_by[name], left)
            self._paid_session_by[name] -= take
            left -= take
            if left <= 1e-12:
                break

    def _paid_headers(self, route: RemoteRoute) -> dict:
        if route.auth in ("grok_session", "xai_oauth"):
            from .grok_session import proxy_headers
            return proxy_headers(self._session_token(route.auth), route.model)
        ref = route.api_key_secret
        if not ref:
            return {}
        from .notify import bw_secret
        key = bw_secret(str(ref))
        if not key:
            raise LLMError(f"provider {route.provider}: no API key at bw-secret {ref}")
        return {"Authorization": f"Bearer {key}"}

    def _session_token(self, auth: str = "grok_session") -> str:
        fn = getattr(self, "_session_token_fn", None)
        if fn is not None:
            return fn()
        if auth == "xai_oauth":
            from .xai_oauth import access_token
            return access_token()
        from .grok_session import access_token
        return access_token()

    async def _post(self, role: str, body: dict) -> tuple[str, str | None]:
        t0 = time.monotonic()
        route = self._routes.get(role)
        url, headers = self.base_url, {}
        wire = "chat"
        if route is not None:
            self.paid_gate(role)
            if not route.base_url:
                raise LLMError(f"provider {route.provider}: base_url is empty")
            url, headers, wire = route.base_url, self._paid_headers(route), route.wire
        path = "/responses" if wire == "responses" else "/chat/completions"
        payload = responses_body(body) if wire == "responses" else body
        try:
            r = await self._http.post(f"{url}{path}", json=payload, headers=headers)
        except httpx.TransportError as e:  # connect/read timeouts, connection refused, ...
            raise self._transport_failed(e) from None
        self._fails = 0
        if r.status_code >= 400:
            raise _HTTPFail(r.status_code, r.text[:300])
        try:
            data = r.json()
            if wire == "responses":
                content, finish, usage = responses_result(data)
            else:
                usage = data.get("usage") or {}
                choice = data["choices"][0]
                content, finish = choice["message"]["content"] or "", choice.get("finish_reason")
        except (ValueError, KeyError, IndexError, TypeError, AttributeError) as e:
            raise ValueError(f"malformed completion: {e}") from None
        if route is not None:
            usd = (int(usage.get("prompt_tokens", 0)) * route.usd_in
                   + int(usage.get("completion_tokens", 0)) * route.usd_out) / 1e6
            self._paid_session += usd
            self._paid_session_by[route.provider] = self._paid_session_by.get(route.provider, 0.0) + usd
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
                "messages": [{"role": "system", "content": system + ("" if strict else _JSON_ONLY)},
                             {"role": "user", "content": prompt}],
            }
            if role not in self._routes:
                # llama.cpp only. A remote provider rejects this field and the whole call would 400.
                body["chat_template_kwargs"] = {"enable_thinking": False}
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
