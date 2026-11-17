"""xAI device-code login for Deal Hunter.

Same approval as OpenClaw: a link and a code, approved at accounts.x.ai, no API key.
The session stays in ~/.config/deal-hunter/xai-oauth.json (mode 600). It is not the Grok CLI
file and not OpenClaw's store. Tokens are never printed.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from .llm import LLMError

SESSION_PATH = Path.home() / ".config" / "deal-hunter" / "xai-oauth.json"
# Public client id shared by the Grok CLI and OpenClaw. Not a secret.
CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
SCOPE = "openid profile email offline_access grok-cli:access api:access"
DISCOVERY_URL = "https://auth.x.ai/.well-known/openid-configuration"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


def _trusted(url: str, label: str) -> str:
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or ""
    if parsed.scheme == "https" and (host == "x.ai" or host.endswith(".x.ai")):
        return url
    raise LLMError(f"xAI OAuth returned an untrusted {label}")


def _http(method: str, url: str, form: dict | None = None) -> tuple[int, dict]:
    try:
        if method == "GET":
            r = httpx.get(url, headers={"Accept": "application/json"}, timeout=30, follow_redirects=False)
        else:
            r = httpx.post(url, data=form or {}, headers={"Accept": "application/json"},
                           timeout=30, follow_redirects=False)
    except httpx.HTTPError as e:
        raise LLMError(f"xAI login failed: {type(e).__name__}") from None
    try:
        body = r.json()
    except ValueError:
        body = {}
    return r.status_code, body if isinstance(body, dict) else {}


def _discover(http) -> tuple[str, str]:
    status, body = http("GET", DISCOVERY_URL, None)
    device = body.get("device_authorization_endpoint") if status == 200 else None
    token = body.get("token_endpoint") if status == 200 else None
    if not isinstance(device, str) or not isinstance(token, str):
        raise LLMError("xAI login failed: discovery did not return device-code endpoints")
    return _trusted(device, "device endpoint"), _trusted(token, "token endpoint")


def _request_code(http, device_endpoint: str) -> dict:
    status, body = http("POST", device_endpoint, {"client_id": CLIENT_ID, "scope": SCOPE})
    code = body.get("device_code")
    user = body.get("user_code")
    uri = body.get("verification_uri")
    if status != 200 or not isinstance(code, str) or not isinstance(user, str) or not isinstance(uri, str):
        raise LLMError(f"xAI login failed: device code request returned {status}")
    complete = body.get("verification_uri_complete")
    try:
        interval = max(1, int(body.get("interval") or 5))
        expires = max(1, int(body.get("expires_in") or 300))
    except (TypeError, ValueError):
        interval, expires = 5, 300
    return {
        "device_code": code, "user_code": user,
        "verification_uri": _trusted(uri, "verification address"),
        "verification_uri_complete": _trusted(complete, "verification address") if isinstance(complete, str) else None,
        "interval": interval, "expires_in": expires,
    }


def _poll(http, token_endpoint: str, device_code: str, interval: int, expires_in: int, sleep) -> dict:
    deadline = time.monotonic() + expires_in
    while time.monotonic() < deadline:
        status, body = http("POST", token_endpoint, {
            "grant_type": DEVICE_GRANT, "client_id": CLIENT_ID, "device_code": device_code})
        if status == 200 and isinstance(body.get("access_token"), str) and isinstance(body.get("refresh_token"), str):
            return body
        err = body.get("error") if isinstance(body.get("error"), str) else ""
        if err == "slow_down":
            interval += 5
        elif err in ("authorization_pending", "slow_down"):
            pass
        elif err in ("access_denied", "authorization_denied"):
            raise LLMError("xAI login was denied")
        elif err == "expired_token":
            raise LLMError("xAI login code expired. Run `dh xai-login` again.")
        else:
            raise LLMError(f"xAI login failed ({status}{': ' + err if err else ''})")
        sleep(min(interval, max(0, deadline - time.monotonic())))
    raise LLMError("xAI login timed out. Run `dh xai-login` again.")


def _save(path: Path, token_endpoint: str, body: dict, now: datetime) -> None:
    try:
        seconds = int(body.get("expires_in") or 3600)
    except (TypeError, ValueError):
        seconds = 3600
    data = {
        "access_token": body["access_token"],
        "refresh_token": body["refresh_token"],
        "expires_at": (now + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "token_endpoint": token_endpoint,
        "client_id": CLIENT_ID,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def login(*, http=None, sleep=time.sleep, out=None, path: Path | None = None, now: datetime | None = None) -> None:
    """Print the link and code, wait for approval, store the session. Never prints a token."""
    http = http or _http
    out = out or sys.stdout
    path = path or SESSION_PATH
    now = now or datetime.now(timezone.utc)
    device_endpoint, token_endpoint = _discover(http)
    code = _request_code(http, device_endpoint)
    link = code["verification_uri_complete"] or (
        code["verification_uri"] + "?" + urllib.parse.urlencode({"user_code": code["user_code"]}))
    print(f"Open: {link}", file=out, flush=True)
    print(f"Code: {code['user_code']}", file=out, flush=True)
    print("Waiting for approval at accounts.x.ai", file=out, flush=True)
    body = _poll(http, token_endpoint, code["device_code"], code["interval"], code["expires_in"], sleep)
    _save(path, token_endpoint, body, now)
    print("xai session stored for Deal Hunter. No API key.", file=out, flush=True)


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise LLMError("no xAI login for Deal Hunter (run `dh xai-login`)") from e
    if not isinstance(data, dict) or not data.get("refresh_token"):
        raise LLMError("no xAI login for Deal Hunter (run `dh xai-login`)")
    return data


def _expiry(value: str) -> datetime:
    text = value.replace("Z", "")
    return datetime.strptime(text[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


def access_token(path: Path | None = None, *, now: datetime | None = None, http=None, skew_s: float = 60) -> str:
    """Bearer token for the CLI chat proxy. Refreshes the stored session when it is about to expire."""
    path = path or SESSION_PATH
    now = now or datetime.now(timezone.utc)
    http = http or _http
    data = _load(path)
    token = str(data.get("access_token") or "")
    try:
        exp = _expiry(str(data.get("expires_at") or ""))
    except ValueError:
        exp = now
    if token and exp - now > timedelta(seconds=skew_s):
        return token
    endpoint = _trusted(str(data.get("token_endpoint") or ""), "token endpoint")
    status, body = http("POST", endpoint, {
        "grant_type": "refresh_token",
        "client_id": str(data.get("client_id") or CLIENT_ID),
        "refresh_token": str(data["refresh_token"]),
    })
    new = body.get("access_token") if status == 200 else None
    if not isinstance(new, str) or not new:
        err = body.get("error") if isinstance(body.get("error"), str) else ""
        raise LLMError(f"xAI login refresh failed ({status}{': ' + err if err else ''}). Run `dh xai-login`.")
    data["access_token"] = new
    if isinstance(body.get("refresh_token"), str) and body["refresh_token"]:
        data["refresh_token"] = body["refresh_token"]
    data["expires_in"] = body.get("expires_in") or 3600
    _save(path, endpoint, data, now)
    return new
