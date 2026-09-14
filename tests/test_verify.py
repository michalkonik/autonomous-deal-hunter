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
        self.ok, self.bl, self.asked = allowed, blocked, []

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

    async def fake_shoot(url, path):
        called.append(url)
        return True
    monkeypatch.setattr(verify, "_shoot", fake_shoot)
    url = "https://www.kleinanzeigen.de/s-anzeige/x/1"
    assert asyncio.run(verify.screenshot(url, tmp_path / "a.png", FakeFetcher(allowed=False))) is False
    assert asyncio.run(verify.screenshot(url, tmp_path / "a.png", FakeFetcher(blocked=True))) is False
    assert called == []
    assert asyncio.run(verify.screenshot(url, tmp_path / "a.png", FakeFetcher())) is True and called == [url]


def test_screenshot_failure_returns_false(tmp_path, monkeypatch):
    async def boom(url, path):
        raise RuntimeError("chrome died")
    monkeypatch.setattr(verify, "_shoot", boom)
    assert asyncio.run(verify.screenshot("https://x.example/a", tmp_path / "a.png", FakeFetcher())) is False
