"""Grok CLI session login: read ~/.grok/auth.json, refresh when it is about to expire, call /responses."""
import json
from datetime import datetime, timezone

import httpx
import pytest

from dh.config import Config
from dh.grok_session import access_token
from dh.llm import Client as LLMClient
from dh.llm import LLMError

SCHEMA = {"type": "object", "required": ["a"], "properties": {"a": {"type": "integer"}}}


def _auth(path, *, expires="2099-01-01T00:00:00.000000000Z", key="access-old"):
    path.write_text(json.dumps({
        "https://auth.x.ai::client": {
            "key": key, "refresh_token": "refresh-old", "expires_at": expires,
            "oidc_client_id": "client",
        }}))


def test_a_fresh_session_is_used_and_not_rewritten(tmp_path):
    p = tmp_path / "auth.json"
    _auth(p)
    calls = []
    assert access_token(p, now=datetime(2026, 11, 12, tzinfo=timezone.utc),
                        post=lambda url, form: calls.append(url) or (200, {})) == "access-old"
    assert calls == []
    assert json.loads(p.read_text())["https://auth.x.ai::client"]["key"] == "access-old"


def test_an_expired_session_is_refreshed_and_saved(tmp_path):
    p = tmp_path / "auth.json"
    _auth(p, expires="2020-01-01T00:00:00.000000000Z")
    seen = []

    def post(url, form):
        seen.append((url, form))
        if url.endswith("openid-configuration"):
            return 200, {"token_endpoint": "https://auth.x.ai/oauth/token"}
        assert form["grant_type"] == "refresh_token" and form["refresh_token"] == "refresh-old"
        assert "access-old" not in json.dumps(form)
        return 200, {"access_token": "access-new", "refresh_token": "refresh-new", "expires_in": 3600}

    assert access_token(p, now=datetime(2026, 11, 12, tzinfo=timezone.utc), post=post) == "access-new"
    row = json.loads(p.read_text())["https://auth.x.ai::client"]
    assert row["key"] == "access-new" and row["refresh_token"] == "refresh-new"
    assert seen[1][0] == "https://auth.x.ai/oauth/token"
    assert (p.stat().st_mode & 0o777) == 0o600


def test_a_bad_refresh_does_not_include_the_token(tmp_path):
    p = tmp_path / "auth.json"
    _auth(p, expires="2020-01-01T00:00:00.000000000Z", key="access-secret")
    def post(url, form):
        if "openid" in url:
            return 200, {"token_endpoint": "https://auth.x.ai/oauth/token"}
        return 400, {"error": "invalid_grant"}
    with pytest.raises(LLMError) as exc:
        access_token(p, now=datetime(2026, 11, 12, tzinfo=timezone.utc), post=post)
    assert "access-secret" not in str(exc.value) and "refresh-old" not in str(exc.value)


def test_grok_session_calls_the_responses_proxy(tmp_path):
    hits = []

    def handler(request: httpx.Request):
        hits.append((str(request.url), json.loads(request.content), dict(request.headers)))
        return httpx.Response(200, json={
            "status": "completed",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": '{"a": 1}'}]}],
            "usage": {"input_tokens": 10, "output_tokens": 4}})

    cfg = Config({"models": {
        "base_url": "http://local/v1",
        "roles": {"worker": "local", "senior": "local", "advisor": "local"},
        "providers": {"grok": {
            "base_url": "https://cli-chat-proxy.grok.com/v1", "auth": "grok_session", "wire": "responses",
            "monthly_cap_usd": 5, "usd_per_mtok_in": 2, "usd_per_mtok_out": 6,
            "roles": {"worker": "grok-4.7"}}}}})
    c = LLMClient(cfg, transport=httpx.MockTransport(handler))
    c._session_token_fn = lambda: "session-token"
    import asyncio
    assert asyncio.run(c.chat_json("worker", "sys", "user", SCHEMA)) == {"a": 1}
    url, body, headers = hits[0]
    assert url == "https://cli-chat-proxy.grok.com/v1/responses"
    assert body["reasoning"] == {"effort": "low"} and body["store"] is False
    assert body["text"]["format"]["type"] == "json_schema"
    assert "chat_template_kwargs" not in body
    assert headers["x-xai-token-auth"] == "xai-grok-cli"
    assert headers["x-grok-model-override"] == "grok-4.7"
    assert headers["authorization"] == "Bearer session-token"
