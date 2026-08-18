import gzip

import httpx
import pytest

from dh.fetch import Fetcher


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
