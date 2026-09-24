import asyncio
from decimal import Decimal

from dealfix import make_deal
from dh import verify
from dh.sources.base import AdapterError
from dh.store import Store
from test_economics import FakeCustoms, FakeFx, make_cfg

P = Decimal


class FakeAdapter:
    def __init__(self, alive=True, price=None, err=False):
        self.a, self.p, self.err, self.seen = alive, price, err, []

    async def alive(self, listing, fetcher):
        self.seen.append(listing.url)
        if self.err:
            raise AdapterError("blocked")
        return self.a, self.p


class FakeFetcher:
    def __init__(self, allowed=True, blocked=False):
        self.ok, self.bl, self.asked, self.paced, self.reported = allowed, blocked, [], [], []

    async def pace(self, domain):
        self.paced.append(domain)
        return True

    def report_block(self, domain, why):
        self.reported.append((domain, why))

    async def allowed(self, url):
        self.asked.append(url)
        return self.ok

    def blocked(self, d):
        return self.bl

    def disabled(self, d):
        return False


def run(st, d, adapter):
    return asyncio.run(verify.verify_deal(st, d.id, make_cfg(), FakeFx(), FakeCustoms(),
                                          {"kleinanzeigen": adapter}, FakeFetcher()))


def test_verify_recomputes_same_net(tmp_path):
    st = Store(tmp_path / "t.db")
    d, res, _ = make_deal(st)
    ad = FakeAdapter(True, P("2000"))
    r = run(st, d, ad)
    assert r.ok and r.alive and r.diffs == [] and r.net_now_cons_pln == r.net_stored_cons_pln == res.cons_net_pln
    assert ad.seen == ["https://www.kleinanzeigen.de/s-anzeige/x/1"]


def test_verify_reports_price_change_and_dead(tmp_path):
    st = Store(tmp_path / "t.db")
    d, res, _ = make_deal(st)
    r = run(st, d, FakeAdapter(True, P("2300")))
    assert not r.ok and r.alive and r.price_now == P("2300") and r.net_now_cons_pln < res.cons_net_pln
    assert any("price changed" in x for x in r.diffs)
    r = run(st, d, FakeAdapter(False, None))
    assert not r.ok and not r.alive and "listing not confirmed alive" in r.diffs
    r = run(st, d, FakeAdapter(err=True))      # blocked fetch is not "alive"
    assert not r.ok and not r.alive


def test_verify_detects_tampered_stored_net(tmp_path):
    st = Store(tmp_path / "t.db")
    d, _, _ = make_deal(st)
    st.conn.execute("update deals set cons_net_pln='999.00' where id=?", (d.id,))
    st.conn.commit()
    r = run(st, d, FakeAdapter(True, P("2000")))
    assert not r.ok and any("does not recompute" in x for x in r.diffs)


def test_recheck_wraps_adapter_errors():
    assert asyncio.run(verify.recheck(type('L', (), {'url': 'u'})(), FakeAdapter(err=True), None)) == (False, None)


def test_record_verification(tmp_path):
    st = Store(tmp_path / "t.db")
    d, _, _ = make_deal(st)
    verify.record_verification(st, d.id, True, "shots/a.png")
    d2 = st.get_deal(d.id)
    assert d2.verified_at is not None and '"verification": "shots/a.png"' in d2.evidence_json
    verify.record_verification(st, d.id, False)
    assert st.get_deal(d.id).verified_at == d2.verified_at


def test_screenshot_respects_robots_and_blocks(tmp_path, monkeypatch):
    called = []

    async def fake_shoot(url, path, fetcher):
        called.append(url)
        return True
    monkeypatch.setattr(verify, "_shoot", fake_shoot)
    url = "https://www.kleinanzeigen.de/s-anzeige/x/1"
    assert asyncio.run(verify.screenshot(url, tmp_path / "a.png", FakeFetcher(allowed=False))) is False
    assert asyncio.run(verify.screenshot(url, tmp_path / "a.png", FakeFetcher(blocked=True))) is False
    assert called == []
    f = FakeFetcher()
    assert asyncio.run(verify.screenshot(url, tmp_path / "a.png", f)) is True and called == [url]
    assert f.paced == ["www.kleinanzeigen.de"]  # paced like any other request to the domain


def test_screenshot_failure_returns_false(tmp_path, monkeypatch):
    async def boom(url, path, fetcher):
        raise RuntimeError("chrome died")
    monkeypatch.setattr(verify, "_shoot", boom)
    assert asyncio.run(verify.screenshot("https://x.example/a", tmp_path / "a.png", FakeFetcher())) is False


def test_verify_price_not_confirmed(tmp_path):
    st = Store(tmp_path / "t.db")
    d, _, _ = make_deal(st)
    r = run(st, d, FakeAdapter(True, None))
    assert r.alive and not r.ok and "price not confirmed" in r.diffs


def test_screenshot_discards_blocked_response_and_reports(tmp_path, monkeypatch):
    """Drive _shoot against a fake Playwright: 429 -> report_block, no file; redirect to a robots-forbidden URL -> no file."""
    import sys, types

    class Resp:
        def __init__(self, status):
            self.status = status

    class Page:
        def __init__(self, status, final):
            self.status, self.url = status, final

        def set_default_timeout(self, ms):
            pass

        async def goto(self, url, **kw):
            return Resp(self.status)

        async def content(self):
            return "<html>hi</html>"

        async def screenshot(self, path):
            open(path, "wb").write(b"png")

    def fake_pw(status, final):
        class B:
            async def new_page(self, **kw):
                return Page(status, final)

            async def close(self):
                pass

        class CM:
            async def __aenter__(self):
                class PW:
                    class chromium:
                        @staticmethod
                        async def launch(**kw):
                            return B()
                return PW()

            async def __aexit__(self, *a):
                pass
        mod = types.ModuleType("playwright.async_api")
        mod.async_playwright = lambda: CM()
        monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
        monkeypatch.setitem(sys.modules, "playwright.async_api", mod)

    url = "https://www.kleinanzeigen.de/s-anzeige/x/1"
    f = FakeFetcher()
    fake_pw(429, url)
    assert asyncio.run(verify.screenshot(url, tmp_path / "b.png", f)) is False
    assert f.reported and f.reported[0][0] == "www.kleinanzeigen.de" and not (tmp_path / "b.png").exists()

    class F2(FakeFetcher):
        async def allowed(self, u):
            return u == url
    fake_pw(200, "https://www.kleinanzeigen.de/private/area")
    assert asyncio.run(verify.screenshot(url, tmp_path / "c.png", F2())) is False
    assert not (tmp_path / "c.png").exists()
    fake_pw(200, url)
    assert asyncio.run(verify.screenshot(url, tmp_path / "d.png", F2())) is True and (tmp_path / "d.png").exists()
