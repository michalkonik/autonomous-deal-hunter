import asyncio
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import yaml

from dh import config, notify
from dh.models import Deal
from dh.store import Store


def deal(star=True):
    return Deal(id=1, listing_ref=1, key="RTX 4090", tier=1, star=star, effort="easy", confidence="high",
                cons_net_pln=Decimal("2500"), exp_net_pln=Decimal("3000"), cons_net_usd=Decimal("639.6"),
                exp_net_usd=Decimal("700"), capital_usd=Decimal("1500"), breakdown_json="{}", evidence_json="{}",
                reasons=[], created_at=datetime.now(timezone.utc))


def setup(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "notify.yaml").write_text(
        "ntfy: {server: 'https://ntfy.test', topic: null, tier1_priority: urgent}\nemail_fallback: {enabled: true}\n")
    return config.load(tmp_path), Store(tmp_path / "t.db")


def test_title_format():
    assert notify.title(deal(), "olx", "Kraków") == "T1 ★ +$640 RTX 4090 · OLX Kraków"
    assert notify.title(deal(star=False), "olx", "Kraków") == "T1 +$640 RTX 4090 · OLX Kraków"


def test_topic_generated_once(tmp_path):
    t = notify.topic(tmp_path)
    assert len(t) == 24 and notify.topic(tmp_path) == t
    assert yaml.safe_load((tmp_path / "config/local/notify.yaml").read_text())["ntfy"]["topic"] == t


def test_push_headers_ok(tmp_path):
    cfg, st = setup(tmp_path)
    seen = {}

    def h(req):
        seen["url"], seen["h"] = str(req.url), req.headers
        return httpx.Response(200)
    c = httpx.AsyncClient(transport=httpx.MockTransport(h))
    ok = asyncio.run(notify.push_tier1(deal(), "https://card", store=st, cfg=cfg, root=tmp_path,
                                       listing_url="https://olx/1", client=c))
    assert ok and seen["url"].endswith("/" + notify.topic(tmp_path))
    assert seen["h"]["priority"] == "urgent" and seen["h"]["tags"] == "moneybag"
    assert seen["h"]["click"] == "https://olx/1" and seen["h"]["actions"] == "view, Card, https://card"
    assert not _has(st) or st.conn.execute("select count(*) from pending_pushes").fetchone()[0] == 0
    # RFC 2047-encoded non-ASCII title
    import base64
    raw = seen["h"]["title"]
    assert raw.startswith("=?UTF-8?B?") and raw.endswith("?=")
    assert base64.b64decode(raw[10:-2]).decode().startswith("T1 ★ +$640 RTX 4090")


def _has(st):
    return bool(st.conn.execute("select 1 from sqlite_master where name='pending_pushes'").fetchone())


def test_push_falls_back_to_email_then_queue(tmp_path):
    cfg, st = setup(tmp_path)
    down = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    sent = []
    creds = json.dumps({"host": "h", "port": 587, "user": "u", "password": "p", "to": "me"})
    # ntfy down, email works
    ok = asyncio.run(notify.push_tier1(deal(), "https://card", store=st, cfg=cfg, root=tmp_path, client=down,
                                       smtp_send=lambda c, s, b: sent.append((c, s)), secret_fn=lambda r: creds))
    assert ok and sent and sent[0][1].startswith("T1")
    assert not _has(st) or st.conn.execute("select count(*) from pending_pushes").fetchone()[0] == 0
    # both down -> queued, no exception
    def boom(*a):
        raise OSError("smtp down")
    ok = asyncio.run(notify.push_tier1(deal(), "https://card", store=st, cfg=cfg, root=tmp_path, client=down,
                                       smtp_send=boom, secret_fn=lambda r: creds))
    assert ok is False
    assert st.conn.execute("select count(*) from pending_pushes").fetchone()[0] == 1
    # email disabled (no bw item) -> queued too
    asyncio.run(notify.push_tier1(deal(), "https://card", store=st, cfg=cfg, root=tmp_path, client=down,
                                  secret_fn=lambda r: None))
    assert st.conn.execute("select count(*) from pending_pushes").fetchone()[0] == 2
    assert st.conn.execute("select count(*) from health where kind='email_disabled'").fetchone()[0] == 1
    # retry: still down keeps them; ntfy back delivers and clears
    assert asyncio.run(notify.retry_pending(st, cfg, tmp_path, client=down, smtp_send=boom,
                                            secret_fn=lambda r: None)) == 0
    up = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    assert asyncio.run(notify.retry_pending(st, cfg, tmp_path, client=up)) == 2
    assert st.conn.execute("select count(*) from pending_pushes").fetchone()[0] == 0


def test_pending_expires_after_6h(tmp_path):
    cfg, st = setup(tmp_path)
    down = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    t0 = datetime.now(timezone.utc)
    asyncio.run(notify.push_tier1(deal(), "https://card", store=st, cfg=cfg, root=tmp_path, client=down,
                                  secret_fn=lambda r: None, now=t0))
    asyncio.run(notify.retry_pending(st, cfg, tmp_path, client=down, secret_fn=lambda r: None,
                                     now=t0 + timedelta(hours=6, minutes=1)))
    assert st.conn.execute("select count(*) from pending_pushes").fetchone()[0] == 0


def test_title_strips_control_chars():
    d = deal()
    d.key = "RTX\r\n4090\x00"
    assert "\n" not in notify.title(d, "olx", "Kra\x07ków") and "\x07" not in notify.title(d, "olx", "Kra\x07ków")


def test_send_test(tmp_path):
    cfg, st = setup(tmp_path)
    seen = {}

    def h(req):
        seen["h"], seen["body"] = req.headers, req.content
        return httpx.Response(200)
    c = httpx.AsyncClient(transport=httpx.MockTransport(h))
    asyncio.run(notify.send_test(cfg, tmp_path, client=c))
    assert seen["h"]["title"] == "Deal Hunter test" and "priority" not in seen["h"]


def test_push_with_cfg_none_does_not_raise(tmp_path):
    (tmp_path / "config").mkdir()  # no notify.yaml at all
    st = Store(tmp_path / "t.db")
    assert asyncio.run(notify.push_tier1(deal(), "https://card", store=st, cfg=None, root=tmp_path)) is False


def test_last_error_recorded_and_health_rate_limited(tmp_path):
    cfg, st = setup(tmp_path)
    down = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    for _ in range(3):
        asyncio.run(notify.push_tier1(deal(), "https://card", store=st, cfg=cfg, root=tmp_path, client=down,
                                      secret_fn=lambda r: None))
    assert st.conn.execute("select count(*) from health where kind='ntfy_failed'").fetchone()[0] == 1
    assert "503" in st.conn.execute("select last_error from pending_pushes").fetchone()[0]


def test_slow_email_does_not_block_loop(tmp_path):
    import time
    cfg, st = setup(tmp_path)
    down = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    creds = json.dumps({"host": "h", "port": 587, "user": "u", "password": "p", "to": "me"})

    async def main():
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.02)
                ticks += 1
        tk = asyncio.create_task(ticker())
        ok = await notify.push_tier1(deal(), "https://card", store=st, cfg=cfg, root=tmp_path, client=down,
                                     smtp_send=lambda c, s, b: time.sleep(0.5), secret_fn=lambda r: creds)
        tk.cancel()
        return ok, ticks
    ok, ticks = asyncio.run(main())
    assert ok and ticks >= 10
