import asyncio
import datetime as dt
import gzip

import httpx
import pytest

from dh.fetch import Fetcher, RobotsRules


class FakeTime:
    def __init__(self):
        self.t = 1000.0
        self.sleeps = []

    def clock(self):
        return self.t

    async def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


class FakeStore:
    def __init__(self):
        self.health = []

    def add_health(self, source, kind, detail):
        self.health.append((source, kind, detail))


CFG = {"fetch": {"min_interval_s": 4, "jitter_s": 2, "backoff_start_s": 60, "backoff_cap_s": 3600}}


def make(tmp_path, handler):
    ft, st = FakeTime(), FakeStore()
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    f = Fetcher(CFG, st, tmp_path, clock=ft.clock, sleep=ft.sleep, http=http)
    return f, ft, st


def ok_handler(log):
    def h(req):
        log.append((req.url.host, req.url.path))
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, text="<html>hello</html>")
    return h


async def test_min_interval_per_domain(tmp_path):
    log = []
    f, ft, _ = make(tmp_path, ok_handler(log))
    await f.get("https://a.test/1", kind="html")
    t1 = ft.t
    await f.get("https://a.test/2", kind="html")
    assert ft.t - t1 >= 4
    n = len(ft.sleeps)
    await f.allowed("https://b.test/1")  # first request to another domain: not delayed by a.test
    assert len(ft.sleeps) == n


async def test_backoff_after_429(tmp_path):
    def h(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(429, text="slow down")
    f, ft, st = make(tmp_path, h)
    r = await f.get("https://a.test/x", kind="html")
    assert r.blocked and r.status == 429
    assert f.blocked("a.test")
    ft.t += 59
    assert f.blocked("a.test")
    ft.t += 2
    assert not f.blocked("a.test")
    await f.get("https://a.test/x", kind="html")
    ft.t += 119
    assert f.blocked("a.test")
    ft.t += 2
    assert not f.blocked("a.test")
    assert [k for _, k, _ in st.health].count("blocked") == 2


async def test_disable_after_3_blocks(tmp_path):
    calls = []

    def h(req):
        calls.append(req.url.path)
        return httpx.Response(404) if req.url.path == "/robots.txt" else httpx.Response(403, text="no")
    f, ft, st = make(tmp_path, h)
    for _ in range(3):
        await f.get("https://a.test/x", kind="html")
        ft.t += 4000
    assert f.disabled("a.test") and f.blocked("a.test")
    n = len(calls)
    r = await f.get("https://a.test/x", kind="html")
    assert r.blocked and len(calls) == n
    assert any(k == "disabled" for _, k, _ in st.health)


async def test_robots_disallow_respected(tmp_path):
    calls = []

    def h(req):
        calls.append(req.url.path)
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private\n")
        return httpx.Response(200, text="ok")
    f, _, st = make(tmp_path, h)
    r = await f.get("https://a.test/private/x", kind="html")
    assert r.disallowed and r.status == 0
    assert "/private/x" not in calls
    assert (await f.get("https://a.test/public", kind="html")).status == 200
    assert calls.count("/robots.txt") == 1  # cached


async def test_robots_failure_only_404_allows(tmp_path):
    f, _, _ = make(tmp_path, lambda req: httpx.Response(500))
    assert not await f.allowed("https://a.test/x")


async def test_challenge_page_is_blocked(tmp_path):
    def h(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, text="<title>Just a moment...</title><div id='cf-chl-x'></div>")
    f, _, st = make(tmp_path, h)
    r = await f.get("https://a.test/x", kind="html")
    assert r.blocked and f.blocked("a.test")
    assert st.health and st.health[0][1] == "blocked"


async def test_snapshot_gzip_and_ua(tmp_path):
    seen = {}

    def h(req):
        seen["ua"] = req.headers["user-agent"]
        return httpx.Response(404) if req.url.path == "/robots.txt" else httpx.Response(200, text="hello")
    f, _, _ = make(tmp_path, h)
    r = await f.get("https://a.test/x", kind="json")
    assert seen["ua"].startswith("DealHunter/0.1")
    assert r.raw_path.suffix == ".gz" and "a.test" in r.raw_path.parts
    assert gzip.open(r.raw_path, "rt").read() == "hello"


async def test_concurrent_gets_hit_blocked_domain_once(tmp_path):
    calls = []

    def h(req):
        calls.append(req.url.path)
        return httpx.Response(404) if req.url.path == "/robots.txt" else httpx.Response(429, text="x")
    f, _, _ = make(tmp_path, h)
    rs = await asyncio.gather(*[f.get(f"https://a.test/{i}", kind="html") for i in range(3)])
    assert len([p for p in calls if p != "/robots.txt"]) == 1
    assert all(r.blocked for r in rs)
    assert calls.count("/robots.txt") == 1


def rules(txt):
    return RobotsRules(txt)


def test_robots_wildcard_query():
    r = rules("User-agent: *\nDisallow: /*?q=\n")
    assert not r.allows("/search?q=x")
    assert r.allows("/search?x=1")


def test_robots_mid_wildcard():
    r = rules("User-agent: *\nDisallow: /a/*/b\n")
    assert not r.allows("/a/zz/b")
    assert not r.allows("/a/x/y/b/c")
    assert r.allows("/a/b")


def test_robots_end_anchor():
    r = rules("User-agent: *\nDisallow: /*.pdf$\n")
    assert not r.allows("/x/y.pdf")
    assert r.allows("/x/y.pdf?download=1")
    assert r.allows("/x/y.pdfx")


def test_robots_longest_match_and_allow_tie():
    r = rules("User-agent: *\nDisallow: /p\nAllow: /p/ok\n")
    assert not r.allows("/p/no")
    assert r.allows("/p/ok/x")
    t = rules("User-agent: *\nDisallow: /same\nAllow: /same\n")
    assert t.allows("/same")


def test_robots_specific_ua_beats_star():
    r = rules("User-agent: *\nDisallow: /\n\nUser-agent: DealHunter\nDisallow: /secret\n")
    assert r.allows("/open")
    assert not r.allows("/secret/x")
    star = rules("User-agent: *\nDisallow: /\n\nUser-agent: otherbot\nAllow: /\n")
    assert not star.allows("/open")


def test_robots_empty_disallow_and_multi_agent_group():
    assert rules("User-agent: *\nDisallow:\n").allows("/x")
    r = rules("User-agent: foo\nUser-agent: dealhunter\nDisallow: /z\n")
    assert not r.allows("/z")


async def test_crawl_delay_raises_interval(tmp_path):
    def h(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nCrawl-delay: 30\n")
        return httpx.Response(200, text="ok")
    f, ft, _ = make(tmp_path, h)
    await f.get("https://a.test/1", kind="html")
    t = ft.t
    await f.get("https://a.test/2", kind="html")
    assert ft.t - t >= 30


async def test_redirect_same_host(tmp_path):
    seen = []

    def h(req):
        seen.append(req.url.path)
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        if req.url.path == "/a":
            return httpx.Response(302, headers={"location": "/b"})
        return httpx.Response(200, text="final")
    f, _, _ = make(tmp_path, h)
    r = await f.get("https://a.test/a", kind="html")
    assert r.status == 200 and r.text == "final" and r.url.endswith("/b")
    assert seen == ["/robots.txt", "/a", "/b"]


async def test_redirect_cross_host_rechecks_robots_and_block(tmp_path):
    seen = []

    def h(req):
        seen.append((req.url.host, req.url.path))
        if req.url.path == "/robots.txt":
            body = "User-agent: *\nDisallow: /nope\n" if req.url.host == "b.test" else None
            return httpx.Response(200, text=body) if body else httpx.Response(404)
        if req.url.host == "a.test":
            tgt = {"/x": "https://b.test/y", "/z": "https://b.test/nope", "/w": "https://c.test/y"}[req.url.path]
            return httpx.Response(301, headers={"location": tgt})
        if req.url.host == "c.test":
            return httpx.Response(429, text="no")
        return httpx.Response(200, text="b-ok")
    f, _, _ = make(tmp_path, h)
    r = await f.get("https://a.test/x", kind="html")
    assert r.status == 200 and ("b.test", "/robots.txt") in seen
    r = await f.get("https://a.test/z", kind="html")
    assert r.disallowed and ("b.test", "/nope") not in seen
    await f.get("https://c.test/q", kind="html")  # block c.test
    assert f.blocked("c.test")
    seen.clear()
    r = await f.get("https://a.test/w", kind="html")
    assert r.blocked and ("c.test", "/y") not in seen


async def test_5xx_soft_backoff_no_snapshot_no_disable(tmp_path):
    def h(req):
        return httpx.Response(404) if req.url.path == "/robots.txt" else httpx.Response(503, text="down")
    f, ft, st = make(tmp_path, h)
    for _ in range(4):
        r = await f.get("https://a.test/x", kind="html")
        assert r.status == 503 and r.raw_path is None and not r.blocked
        assert f.blocked("a.test")
        ft.t += 4000
    assert not f.disabled("a.test")


async def test_404_not_snapshotted(tmp_path):
    f, _, _ = make(tmp_path, lambda req: httpx.Response(404, text="nf"))
    r = await f.get("https://a.test/x", kind="html")
    assert r.status == 404 and r.raw_path is None


async def test_challenge_marker_in_large_page_only_in_head(tmp_path):
    big_ok = "<html><body>" + "x" * 60000 + "datadome</body></html>"
    big_chal = "<html><title>Just a moment...</title><body>" + "x" * 60000 + "</body></html>"
    page = {"v": big_ok}

    def h(req):
        return httpx.Response(404) if req.url.path == "/robots.txt" else httpx.Response(200, text=page["v"])
    f, ft, _ = make(tmp_path, h)
    assert not (await f.get("https://a.test/1", kind="html")).blocked
    page["v"] = big_chal
    assert (await f.get("https://a.test/2", kind="html")).blocked


async def test_robots_403_counts_as_block_and_invalid_url(tmp_path):
    f, _, st = make(tmp_path, lambda req: httpx.Response(403, text="no"))
    assert not await f.allowed("https://a.test/x")
    assert f.blocked("a.test")
    r = await f.get("https://b.test:badport/x", kind="html")
    assert r.status == 0


def test_purge_snapshots(tmp_path):
    f, _, _ = make(tmp_path, lambda req: httpx.Response(404))
    old = tmp_path / "snapshots" / "2020-01-01" / "a.test"
    new = tmp_path / "snapshots" / "2999-01-01" / "a.test"
    for d in (old, new):
        d.mkdir(parents=True)
        (d / "x.gz").write_bytes(b"x")
    assert f.purge_snapshots(90) == 1
    assert not old.exists() and new.exists()


async def test_cross_host_redirect_drops_credentials(tmp_path):
    seen = []

    def h(req):
        seen.append((req.url.host, req.url.path, req.headers.get("authorization"), req.headers.get("x-api-key"),
                     req.headers.get("accept-language")))
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        if req.url.path == "/s":
            return httpx.Response(302, headers={"location": "/s2"})
        if req.url.path == "/s2":
            return httpx.Response(302, headers={"location": "https://b.test/t"})
        return httpx.Response(200, text="ok")
    f, _, _ = make(tmp_path, h)
    await f.get("https://a.test/s", kind="json",
                headers={"Authorization": "Bearer x", "X-Api-Key": "k", "Accept-Language": "nl"})
    pages = {(host, path): rest for host, path, *rest in seen if path != "/robots.txt"}
    assert pages[("a.test", "/s2")] == ["Bearer x", "k", "nl"]  # same-host hop keeps them
    assert pages[("b.test", "/t")] == [None, None, "nl"]


def test_robots_ua_token_exact_match():
    assert rules("User-agent: research\nDisallow: /\n").allows("/x")
    assert not rules("User-agent: DealHunter\nDisallow: /\n").allows("/x")
    assert rules("User-agent: deal\nDisallow: /\n").allows("/x")


async def test_blocked_after_robots_load_no_disallow_event(tmp_path):
    f, _, st = make(tmp_path, lambda req: httpx.Response(429, text="no"))
    r = await f.get("https://a.test/x", kind="html")
    assert r.blocked and not r.disallowed
    assert not any(k == "robots_disallow" for _, k, _ in st.health)


async def test_small_page_marker_after_4kb_is_challenge(tmp_path):
    page = "<html>" + "x" * 5000 + "captcha-delivery</html>"

    def h(req):
        return httpx.Response(404) if req.url.path == "/robots.txt" else httpx.Response(200, text=page)
    f, _, _ = make(tmp_path, h)
    assert (await f.get("https://a.test/1", kind="html")).blocked


async def test_crawl_delay_capped(tmp_path):
    def h(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nCrawl-delay: 3600\n")
        return httpx.Response(200, text="ok")
    f, ft, _ = make(tmp_path, h)
    await f.get("https://a.test/1", kind="html")
    t = ft.t
    await f.get("https://a.test/2", kind="html")
    assert 60 <= ft.t - t < 63


async def test_pace_and_report_block_for_external_page_loads(tmp_path):
    f, ft, st = make(tmp_path, ok_handler([]))
    await f.get("https://a.test/1", kind="html")
    t1, n = ft.t, len(ft.sleeps)
    assert await f.pace("a.test") is True          # waits out the interval like a normal request
    assert ft.t - t1 >= 4 and len(ft.sleeps) == n + 1
    assert await f.pace("fresh.test") is True and len(ft.sleeps) == n + 1  # first request to a domain: no wait
    f.report_block("a.test", "screenshot got status 429")
    assert f.blocked("a.test") and any(h[1] == "blocked" for h in st.health)
    assert await f.pace("a.test") is False
    for _ in range(2):
        f.report_block("a.test", "again")
    assert f.disabled("a.test")


async def test_get_bytes_binary_body_obeys_robots_and_skips_snapshot(tmp_path):
    seen = []

    def h(req):
        seen.append(req.url.path)
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private/\n")
        return httpx.Response(200, content=b"\xff\xd8\xff\xe0JPEG\x00\x80", headers={"content-type": "image/jpeg"})
    f, ft, st = make(tmp_path, h)
    r = await f.get_bytes("https://img.example/p/1.jpg")
    assert r.status == 200 and r.content == b"\xff\xd8\xff\xe0JPEG\x00\x80" and r.raw_path is None and not list(tmp_path.iterdir())
    d = await f.get_bytes("https://img.example/private/1.jpg")
    assert d.disallowed and d.content == b""
    big = await f.get_bytes("https://img.example/p/2.jpg", max_bytes=3)
    assert big.content == b""
