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


def test_title_failure_falls_back_and_still_pushes(tmp_path):
    cfg, st = setup(tmp_path)
    bad = deal()
    bad.cons_net_usd = "not a number"  # title() raises; the push must still go out with a plain title
    seen = {}

    def h(req):
        seen["h"], seen["body"] = req.headers, req.content.decode()
        return httpx.Response(200)
    c = httpx.AsyncClient(transport=httpx.MockTransport(h))
    ok = asyncio.run(notify.push_tier1(bad, "https://card", store=st, cfg=cfg, root=tmp_path, client=c))
    assert ok and seen["h"]["title"] == "T1 deal 1" and seen["body"] == "T1 deal 1"
    assert st.conn.execute("select count(*) from health where kind='title_error'").fetchone()[0] == 1
    # and with ntfy down it is queued, not lost
    down = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    ok = asyncio.run(notify.push_tier1(bad, "https://card", store=st, cfg=cfg, root=tmp_path, client=down,
                                       secret_fn=lambda ref: None))
    assert ok is False and st.conn.execute("select count(*) from pending_pushes").fetchone()[0] == 1


def test_click_and_actions_urls_are_ascii_and_encoded(tmp_path):
    cfg, st = setup(tmp_path)
    seen = {}

    def h(req):
        seen["h"] = req.headers
        return httpx.Response(200)
    c = httpx.AsyncClient(transport=httpx.MockTransport(h))
    listing = "https://www.olx.pl/d/oferta/karta-grafiki-żółw-ID1.html?a=1&b=x y\r\nX-Evil: 1"
    card = "https://log.example/deals/kót,1.md?a=1;b='2'&c=\"3\""
    ok = asyncio.run(notify.push_tier1(deal(), card, store=st, cfg=cfg, root=tmp_path, listing_url=listing, client=c))
    assert ok
    click, actions = seen["h"]["click"], seen["h"]["actions"]
    assert click.isascii() and actions.isascii() and "\n" not in click + actions and "\r" not in click + actions
    assert click.startswith("https://www.olx.pl/d/oferta/karta-grafiki-%C5%BC%C3%B3%C5%82w-ID1.html?a=1&b=x%20y")
    assert actions == "view, Card, https://log.example/deals/k%C3%B3t%2C1.md?a=1%3Bb=%272%27&c=%223%22"  # , ; ' " would split/quote the action


# ------------------------------------------------------------------ final review C1: Actions header, rebuild on retry


def _capture(seen, status=200):
    def h(req):
        seen.append({"h": dict(req.headers), "body": req.content.decode()})
        return httpx.Response(status)
    return httpx.AsyncClient(transport=httpx.MockTransport(h))


def test_empty_card_url_sends_no_actions_header(tmp_path):
    """C1: with no repo_web_url the card URL is "": an `Actions: view, Card, ` header makes ntfy answer 400, so
    every tier-1 push failed. The header must be left out and the request must be a normal 200-shaped push."""
    cfg, st = setup(tmp_path)
    seen = []
    ok = asyncio.run(notify.push_tier1(deal(), "", store=st, cfg=cfg, root=tmp_path, listing_url="https://olx.pl/1",
                                       client=_capture(seen)))
    assert ok and len(seen) == 1
    h = seen[0]["h"]
    assert "actions" not in h and h["click"] == "https://olx.pl/1" and h["priority"] == "urgent"


def test_only_http_urls_make_headers(tmp_path):
    cfg, st = setup(tmp_path)
    for card, listing_url in (("javascript:alert(1)", "ftp://x/y"), ("not a url", ""), ("https://", "http:///p")):
        p = notify.build_payload(deal(), card, "urgent", listing_url=listing_url)
        assert "Actions" not in p["headers"] and "Click" not in p["headers"], (card, listing_url, p["headers"])
    p = notify.build_payload(deal(), "https://github.com/o/r/blob/log/deals/x.md", "urgent", listing_url="")
    assert p["headers"]["Actions"] == "view, Card, https://github.com/o/r/blob/log/deals/x.md"
    assert p["headers"]["Click"] == "https://github.com/o/r/blob/log/deals/x.md"  # no listing: the card


def test_send_test_uses_the_real_push_header_code(tmp_path, monkeypatch):
    """C1: `dh`'s notifier test must exercise the same header builder as a real push (it passed while real pushes
    were rejected)."""
    cfg, st = setup(tmp_path)
    calls = []
    real = notify.ntfy_headers

    def spy(*a, **kw):
        calls.append((a, kw))
        return real(*a, **kw)
    monkeypatch.setattr(notify, "ntfy_headers", spy)
    seen = []
    asyncio.run(notify.send_test(cfg, tmp_path, client=_capture(seen)))
    asyncio.run(notify.push_tier1(deal(), "", store=st, cfg=cfg, root=tmp_path, client=_capture(seen)))
    assert calls[0][0] == ("Deal Hunter test",) and len(calls) >= 2 and len(seen) == 2
    assert all("actions" not in s["h"] for s in seen)


def test_queued_push_is_rebuilt_with_current_code_on_retry(tmp_path):
    """C1: a push queued by an older build (with the broken empty Actions header) is rebuilt by the current payload
    code when it is retried, so a queued push is not doomed to fail forever."""
    cfg, st = setup(tmp_path)
    notify._enqueue(st, {"headers": {"Title": "T1 +$640 RTX 4090", "Priority": "urgent", "Tags": "moneybag",
                                     "Click": "", "Actions": "view, Card, "}, "body": "T1 +$640 RTX 4090"},
                    datetime.now(timezone.utc), "old build")
    down = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    asyncio.run(notify.push_tier1(deal(), "", store=st, cfg=cfg, root=tmp_path, client=down,
                                  listing_url="https://olx.pl/1", secret_fn=lambda r: None))
    stored = json.loads(st.conn.execute("select payload from pending_pushes order by id desc").fetchone()[0])
    assert stored["params"]["listing_url"] == "https://olx.pl/1"  # the inputs are queued, not only the headers
    seen = []
    assert asyncio.run(notify.retry_pending(st, cfg, tmp_path, client=_capture(seen))) == 2
    assert [("actions" in s["h"]) for s in seen] == [False, False]
    assert seen[1]["h"]["click"] == "https://olx.pl/1"


def test_push_body_has_buy_exit_and_readable_name(tmp_path):
    """Spec §12 item 5: the body carries the buy price, the exit, the net and the confidence; the product is named
    for a human (taxonomy display name), not by its raw key."""
    d = deal()
    d.key = "gpu:rtx-4090:24"
    d.evidence_json = json.dumps({"listing": {"price": "4200", "currency": "PLN"}, "buy_pln": "4200.00",
                                  "estimate": {"p25_pln": "5600.00", "median_pln": "6000.00"},
                                  "net_inputs": {"exit_channel": "allegro"}})
    cfg, st = setup(tmp_path)
    seen = []
    assert asyncio.run(notify.push_tier1(d, "https://card", store=st, cfg=cfg, root=tmp_path, client=_capture(seen)))
    body = seen[0]["body"]
    assert "RTX 4090 24GB" in body and "gpu:rtx-4090:24" not in body
    assert "Buy 4200 PLN" in body and "exit 5600-6000 PLN" in body and "allegro" in body
    assert "net $640" in body and "confidence high" in body
    assert notify.title(d, "olx", "Kraków") == "T1 ★ +$640 RTX 4090 24GB · OLX Kraków"
    # a foreign-currency buy shows both
    d.evidence_json = json.dumps({"listing": {"price": "900", "currency": "EUR"}, "buy_pln": "3870.00",
                                  "estimate": {"p25_pln": "5600.00", "median_pln": "6000.00"}})
    assert "Buy 900 EUR (3870 PLN)" in notify.build_payload(d, "", "urgent")["body"]


def test_display_names():
    from dh.taxonomy import display_name
    assert display_name("gpu:rtx-4090:24") == "RTX 4090 24GB"
    assert display_name("gpu:rtx-4070-ti-super:16") == "RTX 4070 Ti Super 16GB"
    assert display_name("gpu:rx-7900-xtx:24") == "RX 7900 XTX 24GB"
    assert display_name("gpu-mobile:rtx-4090:16") == "RTX 4090 Laptop 16GB"
    assert display_name("ram:ddr5:32:2x16") == "DDR5 32GB (2x16GB)"
    assert display_name("ram-sodimm:ddr4:16:2x8") == "DDR4 SO-DIMM 16GB (2x8GB)"
    assert display_name("ssd:nvme:2tb") == "NVMe SSD 2TB"
    assert display_name("pc:bundle") == "PC bundle"
    assert display_name("RTX 4090") == "RTX 4090" and display_name(None) == ""
