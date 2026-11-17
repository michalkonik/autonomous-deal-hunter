"""Deal Hunter's own xAI device-code login. The token stays in the session file and out of the transcript."""
import io
import json
from datetime import datetime, timezone

import httpx

from dh.config import Config
from dh.llm import Client, LLMError
from dh.xai_oauth import access_token, login

import pytest

SCHEMA = {"type": "object", "required": ["a"], "properties": {"a": {"type": "integer"}}}
NOW = datetime(2026, 11, 12, tzinfo=timezone.utc)


def _discovery(method, url, form):
    assert method == "GET"
    return 200, {
        "device_authorization_endpoint": "https://auth.x.ai/oauth/device",
        "token_endpoint": "https://auth.x.ai/oauth/token",
    }


def test_login_prints_the_code_and_stores_the_session(tmp_path):
    polls = {"n": 0}

    def http(method, url, form):
        if method == "GET":
            return _discovery(method, url, form)
        if url.endswith("/device"):
            assert form["scope"].startswith("openid")
            return 200, {
                "device_code": "secret-device", "user_code": "ABCD-EFGH",
                "verification_uri": "https://accounts.x.ai/oauth2/device",
                "verification_uri_complete": "https://accounts.x.ai/oauth2/device?user_code=ABCD-EFGH",
                "interval": 1, "expires_in": 30}
        polls["n"] += 1
        assert form["device_code"] == "secret-device"
        if polls["n"] == 1:
            return 400, {"error": "authorization_pending"}
        return 200, {"access_token": "access-1", "refresh_token": "refresh-1", "expires_in": 3600}

    buf, path = io.StringIO(), tmp_path / "xai-oauth.json"
    login(http=http, sleep=lambda _s: None, out=buf, path=path, now=NOW)
    text = buf.getvalue()
    assert "Open: https://accounts.x.ai/oauth2/device?user_code=ABCD-EFGH" in text
    assert "Code: ABCD-EFGH" in text
    assert "secret-device" not in text and "access-1" not in text and "refresh-1" not in text
    saved = json.loads(path.read_text())
    assert saved["refresh_token"] == "refresh-1" and saved["access_token"] == "access-1"
    assert (path.stat().st_mode & 0o777) == 0o600
    assert access_token(path, now=NOW, http=lambda *a: (_ for _ in ()).throw(AssertionError("no refresh"))) == "access-1"


def test_a_denied_login_stores_nothing(tmp_path):
    def http(method, url, form):
        if method == "GET":
            return _discovery(method, url, form)
        if url.endswith("/device"):
            return 200, {"device_code": "d", "user_code": "WXYZ-1234",
                         "verification_uri": "https://accounts.x.ai/oauth2/device", "interval": 1, "expires_in": 30}
        return 400, {"error": "access_denied"}

    path = tmp_path / "xai-oauth.json"
    with pytest.raises(LLMError, match="denied"):
        login(http=http, sleep=lambda _s: None, out=io.StringIO(), path=path, now=NOW)
    assert not path.exists()


def test_an_expired_session_refreshes_without_leaking_the_token(tmp_path):
    path = tmp_path / "xai-oauth.json"
    path.write_text(json.dumps({
        "access_token": "access-old", "refresh_token": "refresh-old",
        "expires_at": "2020-01-01T00:00:00Z", "token_endpoint": "https://auth.x.ai/oauth/token",
        "client_id": "client"}))

    def http(method, url, form):
        assert url == "https://auth.x.ai/oauth/token"
        assert form["refresh_token"] == "refresh-old" and "access-old" not in json.dumps(form)
        return 200, {"access_token": "access-new", "refresh_token": "refresh-new", "expires_in": 120}

    assert access_token(path, now=NOW, http=http) == "access-new"
    saved = json.loads(path.read_text())
    assert saved["refresh_token"] == "refresh-new" and saved["expires_at"] == "2026-11-12T00:02:00Z"


def test_a_bad_refresh_does_not_include_the_token(tmp_path):
    path = tmp_path / "xai-oauth.json"
    path.write_text(json.dumps({
        "access_token": "access-secret", "refresh_token": "refresh-secret",
        "expires_at": "2020-01-01T00:00:00Z", "token_endpoint": "https://auth.x.ai/oauth/token"}))

    def http(method, url, form):
        return 400, {"error": "invalid_grant"}

    with pytest.raises(LLMError) as exc:
        access_token(path, now=NOW, http=http)
    assert "access-secret" not in str(exc.value) and "refresh-secret" not in str(exc.value)


def test_an_untrusted_token_endpoint_is_refused(tmp_path):
    def http(method, url, form):
        return 200, {"device_authorization_endpoint": "https://evil.example/device",
                     "token_endpoint": "https://evil.example/token"}

    with pytest.raises(LLMError, match="untrusted"):
        login(http=http, sleep=lambda _s: None, out=io.StringIO(), path=tmp_path / "x.json", now=NOW)


def test_xai_oauth_calls_the_responses_proxy():
    hits = []

    def handler(request: httpx.Request):
        hits.append((str(request.url), json.loads(request.content), dict(request.headers)))
        return httpx.Response(200, json={
            "status": "completed", "output_text": '{"a": 1}',
            "usage": {"input_tokens": 3, "output_tokens": 2}})

    cfg = Config({"models": {
        "base_url": "http://local/v1",
        "roles": {"worker": "local", "senior": "local", "advisor": "local"},
        "providers": {"grok": {
            "base_url": "https://cli-chat-proxy.grok.com/v1", "auth": "xai_oauth", "wire": "responses",
            "monthly_cap_usd": 5, "usd_per_mtok_in": 2, "usd_per_mtok_out": 6,
            "roles": {"advisor": "grok-4.7"}}}}})
    import asyncio
    c = Client(cfg, transport=httpx.MockTransport(handler))
    c._session_token_fn = lambda: "session-token"
    assert asyncio.run(c.chat_json("advisor", "sys", "user", SCHEMA)) == {"a": 1}
    url, body, headers = hits[0]
    assert url.endswith("/responses") and body["model"] == "grok-4.7"
    assert headers["authorization"] == "Bearer session-token"
    assert headers["x-xai-token-auth"] == "xai-grok-cli"
