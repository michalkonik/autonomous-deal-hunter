"""The Grok CLI login in ~/.grok/auth.json.

That file is what `grok login` writes. Deal Hunter's own device login is `dh xai-login`
(`~/.config/deal-hunter/xai-oauth.json`). OpenClaw keeps a separate copy in its own store.
Deal Hunter reads it at call time. It does not copy the token into the repo, the database, or a log.
A refresh writes the new token back into the same file, which is what the CLI does too.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from .llm import LLMError

AUTH_PATH = Path.home() / ".grok" / "auth.json"
# Public client id of the Grok CLI / OpenClaw shared xAI login. Not a secret.
CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
DISCOVERY_URL = "https://auth.x.ai/.well-known/openid-configuration"
PROXY_BASE = "https://cli-chat-proxy.grok.com/v1"


def cli_version() -> str:
    """The installed Grok CLI version. The proxy rejects a call that does not send one."""
    try:
        data = json.loads((Path.home() / ".grok" / "version.json").read_text())
        version = str(data.get("version") or "")
        if version:
            return version
    except (OSError, ValueError):
        pass
    return "1.0.77"


def proxy_headers(token: str, model: str) -> dict[str, str]:
    """Headers the CLI chat proxy requires so it bills the subscription, not an API key."""
    return {
        "Authorization": f"Bearer {token}",
        "X-XAI-Token-Auth": "xai-grok-cli",
        "x-grok-client-version": cli_version(),
        "x-grok-model-override": model,
    }


def _parse_expiry(value: str) -> datetime:
    return datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


def _entry(data: dict) -> tuple[str, dict]:
    for key, row in data.items():
        if isinstance(row, dict) and row.get("refresh_token") and "auth.x.ai" in str(key):
            return str(key), row
    raise LLMError("no grok login in ~/.grok/auth.json (run `grok login`)")


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise LLMError("no grok login in ~/.grok/auth.json (run `grok login`)") from e
    if not isinstance(data, dict):
        raise LLMError("no grok login in ~/.grok/auth.json (run `grok login`)")
    return data


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _http_post(url: str, form: dict) -> tuple[int, dict]:
    try:
        r = httpx.post(url, data=form, headers={"Accept": "application/json"}, timeout=30)
    except httpx.HTTPError as e:
        raise LLMError(f"grok login refresh failed: {type(e).__name__}") from None
    try:
        body = r.json()
    except ValueError:
        body = {}
    return r.status_code, body if isinstance(body, dict) else {}


def _token_endpoint(post) -> str:
    status, body = post(DISCOVERY_URL, {})
    endpoint = body.get("token_endpoint") if status == 200 else None
    if not isinstance(endpoint, str) or not endpoint.startswith("https://auth.x.ai/"):
        raise LLMError("grok login refresh failed: token endpoint was not on auth.x.ai")
    return endpoint


def access_token(path: Path | None = None, *, now: datetime | None = None, post=None,
                 skew_s: float = 60) -> str:
    """A bearer token for the CLI chat proxy. Refreshes only when the stored one is about to expire."""
    path = path or AUTH_PATH
    now = now or datetime.now(timezone.utc)
    post = post or _http_post
    data = _load(path)
    key, row = _entry(data)
    token = str(row.get("key") or "")
    try:
        exp = _parse_expiry(str(row.get("expires_at") or ""))
    except ValueError:
        exp = now
    if token and exp - now > timedelta(seconds=skew_s):
        return token
    refresh = str(row.get("refresh_token") or "")
    if not refresh:
        raise LLMError("grok login has no refresh token (run `grok login`)")
    endpoint = _token_endpoint(post)
    status, body = post(endpoint, {
        "grant_type": "refresh_token",
        "client_id": str(row.get("oidc_client_id") or CLIENT_ID),
        "refresh_token": refresh,
    })
    new = body.get("access_token") if status == 200 else None
    if not isinstance(new, str) or not new:
        err = body.get("error") if isinstance(body.get("error"), str) else ""
        raise LLMError(f"grok login refresh failed ({status}{': ' + err if err else ''}). Run `grok login`.")
    row["key"] = new
    if isinstance(body.get("refresh_token"), str) and body["refresh_token"]:
        row["refresh_token"] = body["refresh_token"]
    try:
        seconds = int(body.get("expires_in") or 3600)
    except (TypeError, ValueError):
        seconds = 3600
    row["expires_at"] = (now + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.000000000Z")
    data[key] = row
    _save(path, data)
    return new
