"""Round pipeline (scheduler.run_round) with a fake adapter, a fake LLM and fixed NBP rates: no network, no model."""
import asyncio
import json
import random
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from dh import config, ledger, scheduler
from dh.bandit import Bandit
from dh.models import Listing
from dh.money import Fx
from dh.sources.base import Adapter, AdapterError, Query
from dh.store import Store

REPO = Path(__file__).resolve().parents[1]
NOW = datetime.now(timezone.utc).replace(microsecond=0)
RATES = {"USD": "3.70", "EUR": "4.25", "CZK": "0.17", "HUF": "0.011", "GBP": "4.90"}
KEY = "gpu:rtx-3060:12"


async def nbp(url: str):
    ccy = url.split("/rates/a/")[1].split("/")[0].upper()
    return 200, json.dumps({"rates": [{"mid": RATES[ccy]}]})


class FakeFetcher:
    def __init__(self, store):
        self.store = store
        self.gets: list[str] = []
        self.blocked_domains: set[str] = set()
        self.purges: list[int] = []

    def purge_snapshots(self, days):
        self.purges.append(days)
        return 0

    async def get(self, url, *, kind, headers=None):
        self.gets.append(url)

        class R:
            status, text, url_ = 200, "<html/>", url
            blocked = disallowed = False
        r = R()
        r.url = url
        return r

    def blocked(self, d):
        return d in self.blocked_domains

    def disabled(self, d):
        return d in self.blocked_domains

    async def aclose(self):
        pass


class FakeAdapter(Adapter):
    """Polish marketplace (market PL). Returns its listings for every query; fetches one page per search."""
    name, domain, country, currency = "allegro_lokalnie", "fake.test", "PL", "PLN"

    def __init__(self, listings, alive=(True, None), detail_delay=0.0, search_delay=0.0):
        self.listings, self.alive_result = listings, alive
        self.detail_delay, self.search_delay = detail_delay, search_delay
        self.searches, self.details, self.alive_calls = [], 0, 0

    async def search(self, query: Query, fetcher, to_pln=None):
        self.searches.append(query.text)
        if self.search_delay:
            await asyncio.sleep(self.search_delay)
        await fetcher.get(f"https://{self.domain}/s?q={query.text}", kind="html")
        return [Listing(**{**l.__dict__}) for l in self.listings]

    async def detail(self, listing, fetcher):
        self.details += 1
        if self.detail_delay:
            await asyncio.sleep(self.detail_delay)  # suspends: other arms' tasks run meanwhile
        listing.description = "Karta sprawna, testowana, sprzedaję bo zmieniam na nowszą."
        listing.photos = ["https://fake.test/p1.jpg"]
        return listing

    async def alive(self, listing, fetcher):
        self.alive_calls += 1
        ok, price = self.alive_result
        if ok is None:
            raise AdapterError("unverifiable")
        return ok, (listing.price if price is None and ok else price)


class FakeLLM:
    def __init__(self, reply, senior=None):
        self.reply, self.senior, self.calls = reply, senior, []

    async def chat_json(self, role, system, user, schema, max_tokens=600):
        self.calls.append((role, user))
        assert "<listing>" in user
        return dict(self.senior if role == "senior" and self.senior is not None else self.reply)


def listing(sid="1", title="Gigabyte RTX 3060 Gaming OC używana", price="700"):
    return Listing(source="allegro_lokalnie", source_id=sid, url=f"https://fake.test/oferta/{sid}", title=title,
                   description="", price=Decimal(price), currency="PLN", country="PL", city="Kraków", lat=None,
                   lon=None, seller_hash="h" + sid, seller_age_days=900, seller_ratings=40, seller_score=4.9,
                   ships=True, pickup_only=False, is_business=False, photos=[], posted_at=None, seen_at=NOW)


LLM_3060 = {"key": KEY, "certainty": 0.8, "condition": "used", "defects": [], "box_only": False, "for_parts": False,
            "wanted_ad": False, "bundle_items": []}


def seed_comps(st, n=10, price="1400"):
    for i in range(n):
        st.add_comp(KEY, "used", "olx", Decimal(price) + i, NOW - timedelta(days=1 + i), "owner_sale", f"t:{i}")


def make_ctx(tmp_path, adapter, llm, *, items=None, dry=True, **over):
    tmp_path.mkdir(parents=True, exist_ok=True)
    cfg = config.load(REPO)
    data = cfg.to_dict()
    data["scheduler"] = {**data.get("scheduler", {}), "budget": 10, "max_searches_per_source": 10,
                         "feed_sources": [], **over}
    cfg = config.Config(data, REPO)
    st = Store(tmp_path / "t.db")
    fx = Fx(st, http=nbp)
    items = items if items is not None else [
        {"id": "q.rtx3060", "kind": "query", "target": "all", "value": "rtx 3060", "sentinel": False}]
    pushes = []

    async def push(deal, card_url, **kw):
        pushes.append((deal, card_url, kw))
        return True

    async def shot(url, path, fetcher):
        return False

    async def gpu():
        return 100.0

    async def no_reuse(listing, fetcher, store):
        return []

    ctx = scheduler.RoundContext(
        cfg=cfg, root=REPO, store=st, fetcher=FakeFetcher(st), adapters={adapter.name: adapter}, llm=llm, fx=fx,
        customs=None, bandit=Bandit(st), rng=random.Random(1), cards_root=tmp_path / "cards",
        shots_root=tmp_path / "shots", dry=dry, push=push, screenshot=shot, gpu_sample=gpu,
        items=items, photo_check=no_reuse)
    ctx.pushes = pushes
    return ctx


async def test_round_pipeline_with_fakes(tmp_path):
    ad = FakeAdapter([listing()])
    llm = FakeLLM(LLM_3060)
    ctx = make_ctx(tmp_path, ad, llm)
    seed_comps(ctx.store)
    stats = await scheduler.run_round(ctx)
    st = ctx.store

    assert ad.searches == ["rtx 3060"]
    assert stats.listings_seen == 1 and stats.candidates == 1
    assert len(llm.calls) == 1 and llm.calls[0][0] == "worker"  # rules certainty 0.7 -> LLM; 0.8 < 0.85: no confirm
    assert stats.llm_calls == 1
    assert stats.deals_by_tier == {2: 1}, stats.deals
    d = st.deals()[0]
    assert d.tier == 2 and d.key == KEY and d.confidence == "medium" and d.effort == "easy"
    assert d.cons_net_usd >= Decimal("50")
    assert d.verified_at is not None  # tier 2 is re-checked live (same price)
    ex = st.conn.execute("select * from extractions").fetchone()
    assert ex["method"] == "llm" and ex["key"] == KEY
    # deal card written (md + json), detail fetched once, no push in a dry tier-2 round
    md = list((tmp_path / "cards" / "deals").glob("*/*.md"))
    assert len(md) == 1 and md[0].name.endswith(f"-{d.id}.md")
    card = json.loads(md[0].with_suffix(".json").read_text())
    assert card["tier"] == 2 and card["key"] == KEY
    assert ad.details == 1 and ctx.pushes == []
    # books and bandit
    kinds = {r["kind"] for r in st.conn.execute("select kind from costs")}
    assert {"energy", "llm", "fetch", "paid_api"} <= kinds
    arm = ledger.arm_id("allegro_lokalnie", "rtx 3060")
    row = st.conn.execute("select * from arm_stats where arm=?", (arm,)).fetchone()
    assert row["pulls"] == 1 and row["reward"] > 0
    r = st.conn.execute("select * from rounds").fetchone()
    assert r["deals_found"] == 1 and json.loads(r["notes"])["deals_by_tier"] == {"2": 1}


async def test_second_round_does_not_reprocess_or_duplicate(tmp_path):
    ad = FakeAdapter([listing()])
    llm = FakeLLM(LLM_3060)
    ctx = make_ctx(tmp_path, ad, llm)
    seed_comps(ctx.store)
    await scheduler.run_round(ctx)
    s2 = await scheduler.run_round(ctx)
    assert len(llm.calls) == 1 and s2.candidates == 0 and len(ctx.store.deals()) == 1
    # a price change is re-evaluated, reusing the stored LLM extraction; the listing's own deal is UPDATED
    ad.listings = [listing(price="650")]
    s3 = await scheduler.run_round(ctx)
    assert len(llm.calls) == 1 and s3.candidates == 1 and s3.updated == 1 and s3.duplicates == 0
    ds = ctx.store.deals()
    assert len(ds) == 1 and json.loads(ds[0].evidence_json)["listing"]["price"] == "650"


async def test_cheap_filter_keeps_bait_and_noise_away_from_llm(tmp_path):
    ls = [listing("1", "Kupię RTX 3060"), listing("2", "Pudełko po RTX 3060"), listing("3", "Rower górski"),
          listing("4", "RX 6800 używana", "1500")]  # certain rules key: no LLM
    llm = FakeLLM(LLM_3060)
    ctx = make_ctx(tmp_path, FakeAdapter(ls), llm)
    stats = await scheduler.run_round(ctx)
    assert llm.calls == [] and stats.listings_seen == 4 and stats.candidates == 1


async def test_price_sanity_range_drops_accessory_prices(tmp_path):
    llm = FakeLLM(LLM_3060)
    ctx = make_ctx(tmp_path, FakeAdapter([listing("9", "RTX 3060 używana", "50")]), llm)
    seed_comps(ctx.store)  # median ~1405: 50 PLN is below 0.15x
    stats = await scheduler.run_round(ctx)
    assert stats.candidates == 0 and llm.calls == [] and stats.dropped.get("price_range") == 1


async def test_no_llm_mode_uses_rules_only(tmp_path):
    ctx = make_ctx(tmp_path, FakeAdapter([listing()]), None)
    seed_comps(ctx.store)
    stats = await scheduler.run_round(ctx)
    ex = ctx.store.conn.execute("select * from extractions").fetchone()
    assert ex["method"] == "rules" and stats.llm_calls == 0
    # rules certainty 0.7 keeps the match below "medium" (0.75): low confidence -> tier 3 at most
    assert all(d.tier >= 3 for d in ctx.store.deals())


async def test_tier1_push_and_screenshot_when_not_dry(tmp_path):
    hi = {**LLM_3060}
    # certain rules key (no LLM); price 0.71x the median (not "too cheap"), net >= $300, 14 sold comps, verified
    ad = FakeAdapter([listing("5", "RTX 3060 12GB używana", "6400")])
    ctx = make_ctx(tmp_path, ad, FakeLLM(hi), dry=False)
    for i in range(14):
        ctx.store.add_comp(KEY, "used", "olx", Decimal("9000") + i, NOW - timedelta(days=1 + i), "owner_sale", f"t:{i}")
    stats = await scheduler.run_round(ctx)
    tiers = [d.tier for d in ctx.store.deals()]
    assert tiers == [1], (tiers, stats.deals)
    assert len(ctx.pushes) == 1 and ctx.pushes[0][2]["listing_url"].startswith("https://fake.test/")


async def test_gone_listing_is_not_saved(tmp_path):
    ad = FakeAdapter([listing()], alive=(False, None))
    ctx = make_ctx(tmp_path, ad, FakeLLM(LLM_3060))
    seed_comps(ctx.store)
    stats = await scheduler.run_round(ctx)
    assert ctx.store.deals() == [] and stats.dropped.get("gone") == 1


async def test_blocked_source_arms_skipped_and_failures_recorded(tmp_path):
    ad = FakeAdapter([listing()])
    ctx = make_ctx(tmp_path, ad, FakeLLM(LLM_3060))
    ctx.fetcher.blocked_domains.add("fake.test")
    stats = await scheduler.run_round(ctx)
    assert ad.searches == [] and stats.skipped_arms == 1

    class Failing(FakeAdapter):
        async def search(self, query, fetcher, to_pln=None):
            raise AdapterError("blocked")
    ctx2 = make_ctx(tmp_path / "b", Failing([]), None)
    s2 = await scheduler.run_round(ctx2)
    assert s2.sources["allegro_lokalnie"]["failed"] == 1
    assert ctx2.store.conn.execute("select ok from sweeps").fetchone()["ok"] == 0


async def test_sentinel_arms_always_run_and_targets(tmp_path):
    items = [{"id": "s.4090", "kind": "query", "target": "allegro_lokalnie,kleinanzeigen", "value": "rtx 4090",
              "sentinel": True},
             {"id": "q.x", "kind": "query", "target": "kleinanzeigen", "value": "rtx 3060", "sentinel": False}]
    ad = FakeAdapter([])
    ctx = make_ctx(tmp_path, ad, None, items=items)
    stats = await scheduler.run_round(ctx)
    assert ad.searches == ["rtx 4090"] and stats.sentinel_arms == 1


async def test_disappeared_listing_confirmed_alive_is_cleared(tmp_path):
    a, b = listing("1", "RX 6800 używana", "1500"), listing("2", "RX 6800 XT używana", "1900")
    ad = FakeAdapter([a, b])
    ctx = make_ctx(tmp_path, ad, None)
    await scheduler.run_round(ctx)
    ad.listings = [a]  # listing 2 drops off the results for two sweeps
    await scheduler.run_round(ctx)
    s3 = await scheduler.run_round(ctx)
    assert s3.disappeared == 1 and s3.alive_cleared == 1 and ad.alive_calls >= 1
    row = ctx.store.conn.execute("select disappeared_at from listings where source_id='2'").fetchone()
    assert row["disappeared_at"] is None


def test_round_fetcher_caches_search_pages():
    import asyncio

    class Inner(FakeFetcher):
        pass
    inner = Inner(None)
    rf = scheduler.RoundFetcher(inner)

    async def go():
        await rf.get("https://x.test/a", kind="html")
        await rf.get("https://x.test/a", kind="html")
        await rf.get("https://x.test/b", kind="html")
    asyncio.run(go())
    assert inner.gets == ["https://x.test/a", "https://x.test/b"] and rf.requests == {"x.test": 2}


def test_build_arms_from_playbook_items():
    ads = {"kleinanzeigen": FakeAdapter([]), "retail": FakeAdapter([])}
    ads["kleinanzeigen"].name = "kleinanzeigen"
    ads["retail"].name = "retail"
    items = [{"id": "q1", "kind": "query", "target": "all", "value": "RTX  4090", "min_pln": 2000, "sentinel": False},
             {"id": "p", "kind": "prompt", "target": "llm", "value": "x"}]
    arms = scheduler.build_arms(items, ads, feed_sources=["retail"])
    ids = {a.id: a for a in arms}
    assert set(ids) == {"kleinanzeigen:rtx 4090", "retail:"}
    assert ids["kleinanzeigen:rtx 4090"].query.min_price_pln == Decimal("2000")
    assert ids["retail:"].feed is True


# ------------------------------------------------------------------------------------------------ M2 hooks

RX = "gpu:rx-6800:16"


class FakeMail(FakeAdapter):
    """Alert mailbox: poll() returns platform listings (source olx); search/detail must never be used."""
    name, domain, via_mailbox = "mailbox", "imap", True

    def __init__(self, listings, store=None, commit_error=False):
        super().__init__(listings)
        self.polls, self.store, self.commit_error = 0, store, commit_error
        self.committed_with: list[int] = []  # listings rows present in the DB at each commit()
        self.purged: list[int] = []

    async def poll(self, fetcher=None):
        self.polls += 1
        return [Listing(**{**l.__dict__}) for l in self.listings]

    async def commit(self):
        n = self.store.conn.execute("select count(*) from listings where source_id like 'm%'").fetchone()[0]
        self.committed_with.append(n)
        if self.commit_error:
            raise AdapterError("mailbox: IMAP error while committing: OSError")
        return len(self.listings)

    def purge(self, older_than_days=90):
        self.purged.append(older_than_days)
        return 0

    async def search(self, query, fetcher, to_pln=None):
        raise AssertionError("the mailbox must never be searched per query")

    async def detail(self, listing, fetcher):
        raise AssertionError("mailbox listings are never detail-fetched")

    async def alive(self, listing, fetcher):
        raise AdapterError("mailbox listings cannot be re-verified")


async def test_mailbox_polled_once_per_round_never_detailed(tmp_path):
    olx = Listing(**{**listing("m1", "RX 6800 używana", "900").__dict__, "source": "olx",
                     "url": "https://www.olx.pl/d/oferta/m1"})
    mail, search = FakeMail([olx]), FakeAdapter([])
    items = [{"id": f"q{i}", "kind": "query", "target": "all", "value": f"rtx 30{i}0", "sentinel": False}
             for i in range(5)]
    ctx = make_ctx(tmp_path, search, None, items=items)
    ctx.adapters["mailbox"] = mail
    mail.store = ctx.store
    for i in range(10):
        ctx.store.add_comp(RX, "used", "olx", Decimal("1400") + i, NOW - timedelta(days=1 + i), "owner_sale", f"r:{i}")
    stats = await scheduler.run_round(ctx)
    assert mail.polls == 1 and len(search.searches) == 5
    assert stats.mail_listings == 1 and stats.sources["mailbox"]["ok"] == 1
    row = ctx.store.conn.execute("select id, source from listings where source_id='m1'").fetchone()
    assert row["source"] == "olx"
    assert ctx.store.conn.execute("select count(*) from sweeps where source='olx'").fetchone()[0] == 0  # no arm
    d = ctx.store.deals()[0]
    assert d.tier == 2 and d.verified_at is None  # unverifiable: medium at most, never tier 1
    assert all(a.source != "mailbox" for a in scheduler.build_arms(items, ctx.adapters, []))
    assert mail.purged == [90]  # daily maintenance purges saved .eml files


async def test_mailbox_commit_only_after_persistence(tmp_path):
    ls = [Listing(**{**listing(f"m{i}", "RX 6800 używana", "900").__dict__, "source": "olx"}) for i in (1, 2)]
    ctx = make_ctx(tmp_path, FakeAdapter([]), None, items=[])
    mail = FakeMail(ls, ctx.store)
    ctx.adapters["mailbox"] = mail
    stats = await scheduler.run_round(ctx)
    assert mail.committed_with == [2] and stats.mail_committed == 2  # both rows were in the DB when acked
    # IMAP trouble on commit: health event, round goes on, the mail is offered again next round (idempotent upsert)
    bad = FakeMail(ls, ctx.store, commit_error=True)
    ctx.adapters["mailbox"] = bad
    s2 = await scheduler.run_round(ctx)
    assert s2.mail_committed == 0 and bad.committed_with == [2]
    h = ctx.store.conn.execute("select kind from health where source='mailbox'").fetchall()
    assert [r["kind"] for r in h] == ["commit_failed"]
    assert ctx.store.conn.execute("select count(*) from listings where source='olx'").fetchone()[0] == 2


async def test_ebay_insights_ingested_before_valuation_and_capped(tmp_path):
    from dh.models import CompRow

    class FakeInsights:
        enabled = True

        def __init__(self):
            self.calls = []

        async def sold_comps(self, key, fetcher=None):
            self.calls.append(key)
            return [CompRow(key=key, condition="used", channel="ebay_de", kind="ebay_sold", price=Decimal("330") + i,
                            currency="EUR", at=NOW - timedelta(days=1 + i), weight=1.0, ref=f"ebay:s{i}", country="DE")
                    for i in range(8)]
    ls = [listing("1", "RX 6800 używana", "900"), listing("2", "MSI RTX 3080 10GB używana", "1200")]
    ctx = make_ctx(tmp_path, FakeAdapter(ls), None, ebay_insights_keys=1)
    ctx.insights = FakeInsights()
    stats = await scheduler.run_round(ctx)
    assert len(ctx.insights.calls) == 1 and stats.insights_calls == 1
    key = ctx.insights.calls[0]
    n = ctx.store.conn.execute("select count(*) from comps where kind='ebay_sold' and key=?", (key,)).fetchone()[0]
    assert n == 8 and stats.comps_ingested == 8
    # the key that got sold comps was valued in this same round; the other one had nothing
    assert stats.dropped.get("no_valuation") == 1
    assert stats.candidates == 2


async def test_partout_values_pc_bundle(tmp_path, monkeypatch):
    from dh.valuation import ExitEstimate
    seen = {}

    async def fake_value(l, ex, store, day, client=None, **kw):
        seen["key"], seen["client"] = ex.key, client
        return ExitEstimate(channel="allegro", key="pc:bundle", condition=None, p25_pln=Decimal("7000"),
                            median_pln=Decimal("7500"), p75_pln=Decimal("8000"), n_effective=8.0, n_sold=8,
                            newest_days=1.0, days_to_sell=None, trend_30d=None, ceiling_pln=None, comps=[],
                            n_sold_effective=8.0,
                            notes=["partout", "parts:3", "part:gpu:rtx-3060:12", "part:cpu:ryzen-5-5600",
                                   "part:ram:ddr4:16:2x8"])
    monkeypatch.setattr(scheduler.partout, "value", fake_value)
    llm = FakeLLM(LLM_3060)
    ctx = make_ctx(tmp_path, FakeAdapter([listing("7", "Komputer gamingowy Ryzen 5 5600 RTX 3060 16GB", "3500")]), llm)
    stats = await scheduler.run_round(ctx)
    assert seen["key"] == "pc:bundle" and seen["client"] is not None
    assert llm.calls == []  # the product extractor is not asked about whole PCs
    assert stats.partout == 1
    d = ctx.store.deals()[0]
    assert d.key == "pc:bundle" and d.effort == "heavy" and d.tier == 3  # 3 parts -> heavy; certainty 0.5 -> low
    assert sorted(json.loads(d.evidence_json)["components"]) == ["cpu:ryzen-5-5600", "gpu:rtx-3060:12",
                                                                 "ram:ddr4:16:2x8"]


async def test_photo_reuse_feeds_risk_for_candidates(tmp_path):
    checked = []

    async def reuse(listing, fetcher, store):
        checked.append(listing.source_id)
        return ["photo_reuse"]
    ctx = make_ctx(tmp_path, FakeAdapter([listing()]), FakeLLM(LLM_3060))
    ctx.photo_check = reuse
    seed_comps(ctx.store)
    stats = await scheduler.run_round(ctx)
    assert checked == ["1"] and stats.photo_reuse == 1
    d = ctx.store.deals()[0]
    assert d.tier == 3  # stock_photo + too_cheap lift p_scam above the medium bar (0.15)
    assert "stock_photo" in json.loads(d.evidence_json)["risk"]["flags"]


async def test_daily_maintenance_purges_once(tmp_path):
    ctx = make_ctx(tmp_path, FakeAdapter([]), None)
    from dh import photos
    photos.ensure_schema(ctx.store)
    old = (NOW - timedelta(days=120)).isoformat()
    ctx.store.conn.execute("insert into photo_hashes (listing_id, seller_hash, phash, seen_at) values (1,'s','00ff',?)",
                           (old,))
    ctx.store.conn.commit()
    s1 = await scheduler.run_round(ctx)
    await scheduler.run_round(ctx)
    assert ctx.fetcher.purges == [90] and s1.maintenance == {"photo_hashes": 1, "snapshot_files": 0}
    assert ctx.store.conn.execute("select count(*) from photo_hashes").fetchone()[0] == 0


async def test_estimate_from_too_few_comps_is_not_a_price(tmp_path):
    """Live round 2026-10-29: one stray ask made a whole exit estimate."""
    ctx = make_ctx(tmp_path, FakeAdapter([listing("1", "RX 6800 używana", "900")]), None)
    for i in range(2):
        ctx.store.add_comp(RX, "used", "olx", Decimal("9000"), NOW - timedelta(days=1 + i), "owner_sale", f"x:{i}")
    stats = await scheduler.run_round(ctx)
    assert ctx.store.deals() == [] and stats.dropped.get("no_valuation") == 1


async def test_rules_only_evaluation_is_redone_when_the_llm_is_back(tmp_path):
    ad = FakeAdapter([listing()])
    ctx = make_ctx(tmp_path, ad, None)  # LLM off: uncertain RTX 3060 valued on rules alone
    seed_comps(ctx.store)
    await scheduler.run_round(ctx)
    assert ctx.store.conn.execute("select note from evaluations").fetchone()["note"] == "llm_pending"
    llm = FakeLLM(LLM_3060)
    ctx.llm = llm
    s2 = await scheduler.run_round(ctx)
    assert len(llm.calls) == 1 and s2.llm_items == 1
    assert ctx.store.conn.execute("select note from evaluations").fetchone()["note"] == ""
    await scheduler.run_round(ctx)
    assert len(llm.calls) == 1  # done once


# ------------------------------------------------------------------------------------------------ review fix round 1

def tier1_ctx(tmp_path, ad, llm=None, **kw):
    """Comps for a verified tier-1 RTX 3060 at 6400 PLN (see test_tier1_push_and_screenshot_when_not_dry)."""
    ctx = make_ctx(tmp_path, ad, llm, dry=False, **kw)
    for i in range(14):
        ctx.store.add_comp(KEY, "used", "olx", Decimal("9000") + i, NOW - timedelta(days=1 + i), "owner_sale", f"t:{i}")
    return ctx


TWO_QUERIES = [{"id": "q1", "kind": "query", "target": "all", "value": "rtx 3060", "sentinel": False},
               {"id": "q2", "kind": "query", "target": "all", "value": "rtx 3060 12gb", "sentinel": False}]


async def test_two_arms_same_listing_one_deal_one_push(tmp_path):
    """Critical 1: two arms return the same listing; detail() suspends so both evaluations would overlap."""
    ad = FakeAdapter([listing("5", "RTX 3060 12GB używana", "6400")], detail_delay=0.05)
    ctx = tier1_ctx(tmp_path, ad, items=TWO_QUERIES)
    stats = await scheduler.run_round(ctx)
    assert len(ad.searches) == 2 and stats.listings_seen == 2
    assert len(ctx.store.deals()) == 1 and len(ctx.pushes) == 1 and ad.details == 1


async def test_concurrent_saves_of_one_listing_make_one_deal(tmp_path):
    """The save lock + unique index hold even when two evaluations of one listing race to _save."""
    ad = FakeAdapter([listing("5", "RTX 3060 12GB używana", "6400")], detail_delay=0.05)
    ctx = tier1_ctx(tmp_path, ad)
    r = scheduler._Round(ctx)
    await r._warm_rates()
    lid = ctx.store.upsert_listing(listing("5", "RTX 3060 12GB używana", "6400"))
    from dh.match import rules_extract
    ex = rules_extract("RTX 3060 12GB używana", "", "pl")
    arm = scheduler.Arm("a", "allegro_lokalnie", Query(text="x"), "q")
    l = listing("5", "RTX 3060 12GB używana", "6400")
    await asyncio.gather(r._deal_path(l, lid, ex, arm, Decimal("6400"), ad),
                         r._deal_path(listing("5", "RTX 3060 12GB używana", "6400"), lid, ex, arm, Decimal("6400"), ad))
    assert len(ctx.store.deals()) == 1 and len(ctx.pushes) == 1
    idx = ctx.store.conn.execute("select name from sqlite_master where name='idx_deals_listing'").fetchone()
    assert idx is not None


async def test_retier_updates_the_deal_and_pushes_once(tmp_path):
    """Important 1: valued with the LLM off -> tier 3; the LLM confirms the key next round -> the SAME deal row
    becomes tier 1, is pushed once; later rounds never push it again."""
    ad = FakeAdapter([listing("5", "RTX 3060 używana", "6400")])  # rules certainty 0.7
    ctx = tier1_ctx(tmp_path, ad)
    s1 = await scheduler.run_round(ctx)
    d1 = ctx.store.deals()
    assert [d.tier for d in d1] == [3] and ctx.pushes == [], s1.deals
    ctx.llm = FakeLLM({**LLM_3060, "certainty": 0.95}, senior={"same": True, "key": KEY, "certainty": 0.95})
    s2 = await scheduler.run_round(ctx)  # llm_pending -> re-evaluated
    d2 = ctx.store.deals()
    assert len(d2) == 1 and d2[0].id == d1[0].id and d2[0].tier == 1 and s2.updated == 1
    assert len(ctx.pushes) == 1
    ad.listings = [listing("5", "RTX 3060 używana", "6390")]  # small cut: re-evaluated, still tier 1
    await scheduler.run_round(ctx)
    assert len(ctx.store.deals()) == 1 and len(ctx.pushes) == 1


async def test_unevaluated_listings_are_requeued_at_round_start(tmp_path):
    """Important 2: a persisted (e.g. acked mailbox) listing whose evaluation never ran is evaluated next round even
    though no search returns it again."""
    ad = FakeAdapter([])
    ctx = make_ctx(tmp_path, ad, None)
    seed_comps(ctx.store)
    olx = Listing(**{**listing("m9", "RTX 3060 12GB używana", "700").__dict__, "source": "olx"})
    ledger.observe(ctx.store, "olx", [olx], NOW)  # persisted, never evaluated
    mail = FakeMail([], ctx.store)
    ctx.adapters["mailbox"] = mail
    stats = await scheduler.run_round(ctx)
    assert stats.requeued == 1 and stats.candidates == 1
    assert ctx.store.conn.execute("select count(*) from evaluations").fetchone()[0] == 1
    assert (await scheduler.run_round(ctx)).requeued == 0


async def test_llm_budget_deferral_is_requeued(tmp_path):
    ls = [listing("1", "RTX 3060 używana", "700"), listing("2", "Gigabyte RTX 3060 używana", "710")]
    llm = FakeLLM(LLM_3060)
    ctx = make_ctx(tmp_path, FakeAdapter(ls), llm, max_llm_items=1)
    s1 = await scheduler.run_round(ctx)
    assert s1.llm_deferred == 1 and len(llm.calls) == 1
    ctx.adapters["allegro_lokalnie"].listings = []  # nothing returned any more
    s2 = await scheduler.run_round(ctx)
    assert s2.requeued == 1 and len(llm.calls) == 2


async def test_sweep_deadline_cuts_only_the_stuck_source(tmp_path):
    """Important 3: one source hangs; its sweep is cut at sources.sweep_timeout_s, the other source's deal is saved."""
    good = FakeAdapter([listing("1", "RX 6800 używana", "900")])

    class Stuck(FakeAdapter):
        name, domain = "sprzedajemy", "stuck.test"
    stuck = Stuck([], search_delay=30)
    ctx = make_ctx(tmp_path, good, None)
    ctx.adapters["sprzedajemy"] = stuck
    data = ctx.cfg.to_dict()
    data["sources"] = {**data["sources"], "sweep_timeout_s": 0.3}
    ctx.cfg = config.Config(data, REPO)
    for i in range(10):
        ctx.store.add_comp(RX, "used", "olx", Decimal("1400") + i, NOW - timedelta(days=1 + i), "owner_sale", f"r:{i}")
    stats = await scheduler.run_round(ctx)
    assert stats.sweep_timeouts == ["sprzedajemy"] and stats.seconds < 10
    assert len(ctx.store.deals()) == 1
    assert ctx.store.conn.execute("select kind from health where source='sprzedajemy'").fetchone()["kind"] == "sweep_timeout"


async def test_round_timeout_gives_started_evaluations_a_grace_period(tmp_path):
    ad = FakeAdapter([listing()], detail_delay=0.6)
    ctx = make_ctx(tmp_path, ad, FakeLLM(LLM_3060), round_timeout_s=0.3, timeout_grace_s=2)
    seed_comps(ctx.store)
    stats = await scheduler.run_round(ctx)
    assert len(ctx.store.deals()) == 1  # finished inside the grace period
    assert any("grace" in e for e in stats.errors)
    ad2 = FakeAdapter([listing()], detail_delay=5)
    ctx2 = make_ctx(tmp_path / "b", ad2, FakeLLM(LLM_3060), round_timeout_s=0.2, timeout_grace_s=0.3)
    seed_comps(ctx2.store)
    s2 = await scheduler.run_round(ctx2)
    assert ctx2.store.deals() == [] and any("cancelled" in e for e in s2.errors) and s2.seconds < 3


async def test_foreign_model_makes_the_round_rules_only(tmp_path):
    """Important 4: a model outside the roles is loaded -> no LLM call this round, LLM listings are llm_pending."""
    llm = FakeLLM(LLM_3060)
    ctx = make_ctx(tmp_path, FakeAdapter([listing()]), llm)
    seed_comps(ctx.store)

    async def foreign():
        return ["someone-elses-model"]
    ctx.llm_check = foreign
    stats = await scheduler.run_round(ctx)
    assert llm.calls == [] and stats.llm_mode == "foreign_model" and stats.foreign_models == ["someone-elses-model"]
    assert ctx.store.conn.execute("select note from evaluations").fetchone()["note"] == "llm_pending"

    async def clear():
        return []
    ctx.llm_check = clear
    s2 = await scheduler.run_round(ctx)
    assert len(llm.calls) == 1 and s2.llm_mode == "on"


def _plan_sim(tmp_path, seed: int, rounds: int = 200):
    """Production arm selection (_Round._plan -> Bandit.select, I5 ruling) with simulated rewards: 40 arms on one
    source (16 at 0.3 value units per fetch, 16 at 0.05, 8 dead), budget 10 searches per round (the 25 % arm coverage
    of production: budget 60 of ~300 arms), one fetch per selected arm."""
    rates = {**{f"good{i}": 0.3 for i in range(16)}, **{f"poor{i}": 0.05 for i in range(16)},
             **{f"dead{i}": 0.0 for i in range(8)}}
    items = [{"id": q, "kind": "query", "target": "all", "value": q, "sentinel": False} for q in rates]
    ctx = make_ctx(tmp_path, FakeAdapter([]), None, items=items, budget=10, max_searches_per_source=12)
    ctx.rng = random.Random(seed)
    sim = random.Random(seed + 1000)
    r = scheduler._Round(ctx)
    got: Counter = Counter()
    for _ in range(rounds):
        ctx.budget = 10
        r.stats = scheduler.RoundStats()
        for arm in r._plan():
            q = arm.query.text
            got[q] += 1
            lam, k, t = rates[q], 0, sim.expovariate(1.0) if rates[q] > 0 else float("inf")
            while t < lam:
                k += 1
                t += sim.expovariate(1.0)
            ctx.bandit.update(arm.id, float(k), 1)
        ctx.bandit.discount()
    return got, rates


@pytest.mark.parametrize("seed", range(1, 16))
def test_plan_shifts_searches_to_productive_arms_and_keeps_exploring(tmp_path, seed):
    """>= 3x selection frequency for 0.3 vs 0.05 arms (local simulation of the same setup: 3.33-4.41x over these 15
    seeds with the plan's prior (1, 10)); no arm starves: each gets its 3 guaranteed pulls plus at least half its
    expected share of the exploration slots (200 rounds x 1 slot / ~30 non-top arms ~ 6.7)."""
    got, rates = _plan_sim(tmp_path, seed)
    per = lambda p: sum(v for k, v in got.items() if k.startswith(p)) / sum(1 for k in rates if k.startswith(p))  # noqa: E731
    assert per("good") >= 3 * per("poor"), got
    assert min(got[a] for a in rates) >= 6, got
    assert sum(got.values()) == 2000


def _select_runs(tmp_path, floor: float, seeds: int):
    """20 arms: 5 good (high reward per fetch), 14 poor (no reward; poor0 the cheapest, poor13 the dearest, so their
    Thompson draws differ), 1 new. k = 10. One store, arms re-seeded per run through select's rng only."""
    from dh.bandit import Bandit
    st = Store(tmp_path / f"s{floor}.db")
    b = Bandit(st, floor=floor)
    for i in range(5):
        for _ in range(3):
            b.update(f"good{i}", 20.0, 1)
    for i in range(14):
        for _ in range(3):
            b.update(f"poor{i}", 0.0, 10 * (i + 1))
    arms = [f"good{i}" for i in range(5)] + [f"poor{i}" for i in range(14)] + ["new"]
    return arms, [b.select(arms, 10, random.Random(seed)) for seed in range(seeds)]


def test_select_reserves_exploration_and_new_arm_pulls(tmp_path):
    """A full order; the new arm's guaranteed pull first; the top draws next; and ceil(floor x k) slots in the first k
    drawn uniformly from the arms the draws did not pick: the worst-looking poor arm still gets ~ its share with
    floor 0.3 and almost never with floor 0."""
    def poor_freq(floor):
        arms, outs = _select_runs(tmp_path, floor, 400)
        cnt = Counter()
        for out in outs:
            assert sorted(out) == sorted(arms)  # every arm once
            assert out[0] == "new"
            top = out[:10]
            assert len(set(top)) == 10 and {f"good{i}" for i in range(5)} <= set(top)
            assert sum(a.startswith("poor") for a in top) == 4
            cnt.update(a for a in top if a.startswith("poor"))
        return [cnt[f"poor{i}"] / len(outs) for i in range(14)]
    with_floor, no_floor = poor_freq(0.3), poor_freq(0.0)
    assert min(with_floor) >= 0.15, with_floor  # ~3 uniform slots + 1 by draw over 14 arms: >= ~0.21 each
    assert no_floor[13] < 0.1 and no_floor[0] > 0.6, no_floor  # draws alone favour the cheap arm


async def test_cancelled_round_leaves_no_task_running(tmp_path):
    """Important 6: SIGTERM cancels the round: every sweep and evaluation task is cancelled and awaited."""
    ad = FakeAdapter([listing()], detail_delay=10)
    ctx = make_ctx(tmp_path, ad, FakeLLM(LLM_3060))
    seed_comps(ctx.store)
    t = asyncio.create_task(scheduler.run_round(ctx))
    for _ in range(100):
        await asyncio.sleep(0.02)
        if ad.details:
            break
    assert ad.details == 1  # an evaluation is inside detail()
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    others = [x for x in asyncio.all_tasks() if x is not asyncio.current_task()]
    assert others == []
    assert ctx.store.conn.execute("select count(*) from rounds").fetchone()[0] == 0  # no bookkeeping for it


async def test_live_price_change_at_verification_revalues(tmp_path):
    """Minor: the alive check reads a different price -> the deal is valued at the live price (or dropped)."""
    ad = FakeAdapter([listing()], alive=(True, Decimal("720")))
    ctx = make_ctx(tmp_path, ad, FakeLLM(LLM_3060))
    seed_comps(ctx.store)
    stats = await scheduler.run_round(ctx)
    d = ctx.store.deals()[0]
    assert stats.price_changed == 1 and json.loads(d.evidence_json)["listing"]["price"] == "720"
    ad2 = FakeAdapter([listing()], alive=(True, Decimal("1390")))  # now no margin at all
    ctx2 = make_ctx(tmp_path / "b", ad2, FakeLLM(LLM_3060))
    seed_comps(ctx2.store)
    s2 = await scheduler.run_round(ctx2)
    assert ctx2.store.deals() == [] and s2.dropped.get("price_changed_below_floor") == 1


# ------------------------------------------------------------------------------------------------ wiring minors


async def test_tier1_push_is_queued_when_the_round_is_cancelled_after_reservation(tmp_path):
    """Task 19 minor: SIGTERM while the push is in flight must not lose a reserved tier-1 push: it is queued for the
    retry (pending_pushes) and the reservation stays, so it is never sent twice."""
    ad = FakeAdapter([listing("5", "RTX 3060 12GB używana", "6400")])
    ctx = tier1_ctx(tmp_path, ad)
    started = asyncio.Event()

    async def slow_push(deal, card_url, **kw):
        started.set()
        await asyncio.sleep(30)
        return True
    ctx.push = slow_push
    t = asyncio.create_task(scheduler.run_round(ctx))
    await asyncio.wait_for(started.wait(), 20)
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    rows = ctx.store.conn.execute("select payload from pending_pushes").fetchall()
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload"])
    assert payload["headers"]["Click"].startswith("https://fake.test/oferta/5") and "rtx-3060" in payload["headers"]["Title"]
    assert ctx.store.conn.execute("select count(*) from deal_pushes").fetchone()[0] == 1


async def test_live_price_change_is_recorded_as_the_evaluated_price(tmp_path):
    """Task 19 minor: the evaluation row carries the re-valued (live) price, so the next sweep that reads that price
    does not evaluate the listing again."""
    ad = FakeAdapter([listing()], alive=(True, Decimal("720")))
    ctx = make_ctx(tmp_path, ad, FakeLLM(LLM_3060))
    seed_comps(ctx.store)
    await scheduler.run_round(ctx)
    ev = ctx.store.conn.execute("select price from evaluations").fetchone()
    assert Decimal(ev["price"]) == Decimal("720")
    ad.listings = [listing(price="720")]
    s2 = await scheduler.run_round(ctx)
    assert s2.processed == 0


def test_deal_index_health_event_is_recorded_once_per_process(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, "_INDEX_WARNED", False)
    st = Store(tmp_path / "d.db")
    for _ in range(2):  # duplicate deals per listing: the unique index cannot be created
        st.conn.execute("insert into deals (listing_ref, key, tier) values (7, 'k', 2)")
    st.conn.commit()
    for _ in range(3):
        assert scheduler.ensure_deal_index(st) is False
    n = st.conn.execute("select count(*) from health where kind='deal_index'").fetchone()[0]
    assert n == 1


async def test_sweep_exception_is_surfaced_in_the_round_errors(tmp_path, monkeypatch):
    """Task 19 minor: a sweep that dies with a non-timeout exception shows in stats.errors and the health table;
    the round still completes."""
    ad = FakeAdapter([listing()])
    ctx = make_ctx(tmp_path, ad, None)

    def boom(*a, **kw):
        raise RuntimeError("ledger exploded")
    monkeypatch.setattr(scheduler.ledger, "sweep_disappeared", boom)
    stats = await scheduler.run_round(ctx)
    assert any("allegro_lokalnie" in e and "ledger exploded" in e for e in stats.errors), stats.errors
    assert ctx.store.conn.execute("select count(*) from health where kind='sweep_error'").fetchone()[0] == 1
    assert stats.round_id


def test_rules_extract_gets_the_round_root(tmp_path, monkeypatch):
    """Playbook aliases of the active root apply: the scheduler passes root= to both rules_extract calls."""
    seen = []
    real = scheduler.rules_extract

    def spy(*a, **kw):
        seen.append(kw.get("root"))
        return real(*a, **kw)
    monkeypatch.setattr(scheduler, "rules_extract", spy)
    ad = FakeAdapter([listing("1", "RTX 3060 12GB używana", "700")])
    ctx = make_ctx(tmp_path, ad, None)
    seed_comps(ctx.store)
    asyncio.run(scheduler.run_round(ctx))
    assert seen and all(r == ctx.root for r in seen)
