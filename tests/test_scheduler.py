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
from dh.playbook_io import load_items
from dh.sources.base import Adapter, AdapterError, Query
from dh.store import Store

REPO = Path(__file__).resolve().parents[1]
NOW = datetime.now(timezone.utc).replace(microsecond=0)
RATES = {"USD": "3.70", "EUR": "4.25", "CZK": "0.17", "HUF": "0.011", "GBP": "4.90", "CHF": "4.55"}
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


TRIAGE_OK = {"verdict": "ok", "p_scam": 0.02, "reasons": []}


class FakeLLM:
    """`reply` for extraction, `senior` for the senior confirm pass, `triage` for the finalist triage (a schema with a
    `verdict`). Triage calls are kept apart (`triage_calls`) so extraction-call counts stay as they were."""

    def __init__(self, reply, senior=None, triage=TRIAGE_OK):
        self.reply, self.senior, self.triage, self.calls, self.triage_calls = reply, senior, triage, [], []

    async def chat_json(self, role, system, user, schema, max_tokens=600):
        assert "<listing>" in user
        if "verdict" in (schema.get("properties") or {}):
            self.triage_calls.append((role, user, system))
            if isinstance(self.triage, Exception):
                raise self.triage
            return dict(self.triage)
        self.calls.append((role, user))
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
    assert len(llm.triage_calls) == 1 and llm.triage_calls[0][0] == "senior"  # tier 2: the finalist triage
    assert stats.llm_calls == 2 and stats.triage == {"keep": 1}
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
    assert d.key == "pc:bundle" and d.tier == 3  # 3 parts -> heavy; certainty 0.5 -> low
    # allegro_lokalnie ships inside PL: the direct-to-PL bonus eases heavy -> moderate (owner 2026-11-07)
    assert d.effort == "moderate" and d.ship_pl == 1
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

def tier1_ctx(tmp_path, ad, llm="fake", **kw):
    """Comps for a verified tier-1 RTX 3060 at 6400 PLN (see test_tier1_push_and_screenshot_when_not_dry). The LLM
    defaults to a fake whose finalist triage says ok: without a triage there is no tier 1 (final review I4)."""
    ctx = make_ctx(tmp_path, ad, FakeLLM(LLM_3060) if llm == "fake" else llm, dry=False, **kw)
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
    ctx = tier1_ctx(tmp_path, ad, llm=None)
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
    assert payload["headers"]["Click"].startswith("https://fake.test/oferta/5") and "RTX 3060 12GB" in payload["headers"]["Title"]
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


# ------------------------------------------------------------------------------------------------ fix wave A


def _with(ctx, **sections):
    data = ctx.cfg.to_dict()
    for k, v in sections.items():
        data[k] = {**data.get(k, {}), **v}
    ctx.cfg = config.Config(data, REPO)
    return ctx


def test_pickup_origin_comes_from_config(tmp_path):
    ctx = make_ctx(tmp_path, FakeAdapter([]), None)
    l = Listing(**{**listing().__dict__, "pickup_only": True, "lat": 50.0647, "lon": 19.9450})
    assert scheduler._Round(ctx)._shipping(l, "gpu")[2] == 0.0  # default origin: Krakow
    _with(ctx, pickup={"origin": {"name": "Warszawa", "lat": 52.2297, "lon": 21.0122}})
    km = scheduler._Round(ctx)._shipping(l, "gpu")[2]
    assert 245 < km < 260


async def test_retention_keys_drive_the_daily_purge(tmp_path):
    ad = FakeAdapter([])
    ctx = make_ctx(tmp_path, ad, None)
    _with(ctx, sources={"retention": {"photos_days": 30, "snapshots_days": 45}})
    from dh import photos
    photos.ensure_schema(ctx.store)
    for i, days in enumerate((40, 20)):  # 40 d old: purged at 30; 20 d old: kept
        ctx.store.conn.execute("insert into photo_hashes (listing_id, seller_hash, phash, seen_at) values (?,?,?,?)",
                               (i, "s", f"{i:04x}", (NOW - timedelta(days=days)).isoformat()))
    ctx.store.conn.commit()
    s = await scheduler.run_round(ctx)
    assert s.maintenance["photo_hashes"] == 1 and ctx.fetcher.purges == [45]
    assert ctx.store.conn.execute("select count(*) from photo_hashes").fetchone()[0] == 1


def test_retention_alias_and_defaults():
    base = config.load(REPO)
    assert scheduler.retention_days(base) == (90, 90)
    d = base.to_dict()
    d["sources"] = {k: v for k, v in d["sources"].items() if k != "retention"}
    d["scheduler"] = {**d["scheduler"], "retention_days": 60}  # the old key: an alias when sources.retention is absent
    assert scheduler.retention_days(config.Config(d, REPO)) == (60, 60)


def test_bandit_settings_come_from_scheduler_config(tmp_path):
    from dh import runtime
    base = config.load(REPO)
    assert scheduler.bandit_kwargs(base) == {"gamma": 0.98, "floor": 0.10, "prior": (1.0, 10.0), "new_pulls": 3}
    d = base.to_dict()
    d["scheduler"] = {**d["scheduler"], "bandit": {"gamma": 0.9, "floor": 0.2, "prior": [2, 5]}}
    c = config.Config(d, REPO)
    ctx = runtime.build_context(c, tmp_path, Store(tmp_path / "b.db"), fetcher=None, no_llm=True)
    b = ctx.bandit
    assert (b.gamma, b.floor, b.prior, b.new_pulls) == (0.9, 0.2, (2.0, 5.0), 3)


def test_trend_signal_rules():
    rules = config.load(REPO).rules
    sc = scheduler.sched_cfg(config.load(REPO))
    t = scheduler.trend_signal(0.20, "10", "1000", "high", rules, sc, 50)
    assert t.tier == 4 and not t.star and "trend" in t.reasons[0]
    assert scheduler.trend_signal(0.149, "10", "1000", "high", rules, sc) is None  # below +15%
    assert scheduler.trend_signal(None, "10", "1000", "high", rules, sc) is None
    assert scheduler.trend_signal(0.3, "-1", "1000", "high", rules, sc) is None  # losing money
    assert scheduler.trend_signal(0.3, "10", "1000", "excluded", rules, sc) is None
    assert scheduler.trend_signal(0.3, "10", "99999", "high", rules, sc) is None  # over the capital cap


async def test_rising_market_makes_a_below_floor_listing_a_tier_4_trend_deal(tmp_path, monkeypatch):
    ad = FakeAdapter([listing("1", "Gigabyte RTX 3060 Gaming OC używana", "1380")])
    ctx = make_ctx(tmp_path, ad, FakeLLM(LLM_3060))
    seed_comps(ctx.store)
    s0 = await scheduler.run_round(ctx)
    assert ctx.store.deals() == [], s0.dropped  # below the floor: not logged
    ctx2 = make_ctx(tmp_path / "b", FakeAdapter([listing("1", "Gigabyte RTX 3060 Gaming OC używana", "1380")]),
                    FakeLLM(LLM_3060))
    seed_comps(ctx2.store)
    _with(ctx2, scheduler={"trend": {"min_30d": 0.15, "min_cons_net_usd": -100000}})
    import dataclasses
    real = scheduler._Round._estimate

    def rising(self, *a, **kw):
        e = real(self, *a, **kw)
        return None if e is None else dataclasses.replace(e, trend_30d=0.3)
    monkeypatch.setattr(scheduler._Round, "_estimate", rising)
    await scheduler.run_round(ctx2)
    ds = ctx2.store.deals()
    assert [d.tier for d in ds] == [4] and any(r.startswith("trend:") for r in ds[0].reasons)


class KleinanzeigenFake(FakeAdapter):
    name, domain = "kleinanzeigen", "ka.test"


async def test_mailbox_kleinanzeigen_listing_is_detailed_and_verified_by_the_real_adapter(tmp_path):
    ka = Listing(**{**listing("m1", "RX 6800 używana", "900").__dict__, "source": "kleinanzeigen",
                    "url": "https://www.kleinanzeigen.de/s-anzeige/m1"})
    olx = Listing(**{**listing("m2", "RX 6800 używana", "900").__dict__, "source": "olx",
                     "url": "https://www.olx.pl/d/oferta/m2"})
    real = KleinanzeigenFake([], alive=(True, Decimal("900")))
    ctx = make_ctx(tmp_path, real, None, items=[])
    mail = FakeMail([ka], ctx.store)
    ctx.adapters["mailbox"] = mail
    for i in range(10):
        ctx.store.add_comp(RX, "used", "olx", Decimal("1400") + i, NOW - timedelta(days=1 + i), "owner_sale", f"r:{i}")
    real_detail_calls = []
    orig = real.detail

    async def spy(listing, fetcher):
        real_detail_calls.append(listing.source)
        return await orig(listing, fetcher)
    real.detail = spy
    await scheduler.run_round(ctx)
    d = ctx.store.deals()[0]
    assert real_detail_calls == ["kleinanzeigen"] and real.alive_calls == 1
    assert d.verified_at is not None  # verified through the real adapter
    # an OLX alert stays unverifiable: no detail, no alive, never verified
    ctx2 = make_ctx(tmp_path / "o", real, None, items=[])
    mail2 = FakeMail([olx], ctx2.store)
    ctx2.adapters["mailbox"] = mail2
    for i in range(10):
        ctx2.store.add_comp(RX, "used", "olx", Decimal("1400") + i, NOW - timedelta(days=1 + i), "owner_sale", f"r:{i}")
    real_detail_calls.clear()
    await scheduler.run_round(ctx2)
    assert real_detail_calls == [] and ctx2.store.deals()[0].verified_at is None


# ------------------------------------------------------------------------------------------------ final review I2


class _Nbp:
    """NBP fake whose table for `today` is published only when `published` is set (a different rate)."""

    def __init__(self, today):
        self.today, self.published = today, False
        self.old = {"USD": "3.70", "EUR": "4.25"}
        self.new = {"USD": "3.80", "EUR": "4.40"}

    async def __call__(self, url: str):
        ccy = url.split("/rates/a/")[1].split("/")[0].upper()
        day = url.split("/rates/a/")[1].split("/")[1]
        if day == self.today.isoformat():
            if not self.published:
                return 404, "Not Found"
            return 200, json.dumps({"rates": [{"mid": self.new.get(ccy, RATES[ccy])}]})
        return 200, json.dumps({"rates": [{"mid": self.old.get(ccy, RATES[ccy])}]})


async def test_verify_replays_the_rates_the_deal_was_valued_with(tmp_path):
    """I2: valued before NBP published today's table (yesterday's rate served, not pinned); after the table appears,
    `dh verify` must still recompute the same net from the evidence's recorded rates."""
    from datetime import date

    from dh import cards, verify
    eur = listing("7", "Gigabyte RTX 3060 Gaming OC używana", "165")
    eur.currency = "EUR"
    ad = FakeAdapter([eur])
    ctx = make_ctx(tmp_path, ad, FakeLLM(LLM_3060))
    nbp2 = _Nbp(date.today())
    ctx.fx = Fx(ctx.store, http=nbp2)
    seed_comps(ctx.store)
    await scheduler.run_round(ctx)
    [d] = ctx.store.deals()
    ev = json.loads(d.evidence_json)
    assert json.loads(d.evidence_json)["net_inputs"]["buy_ccy"] == "EUR"
    got = {(r["currency"], r["day"]): (r["rate"], r["effective_date"]) for r in ev["fx_rates"]}
    t = date.today()
    assert got[("EUR", t.isoformat())] == ("4.25", (t - timedelta(days=1)).isoformat())
    assert got[("USD", t.isoformat())][0] == "3.70"
    assert ev["fx_usd"] == {"rate": "3.70", "date": (t - timedelta(days=1)).isoformat()}
    md, data = cards.from_deal(d)
    assert "3.70 PLN per USD" in md and data["fx_usd"]["rate"] == "3.70"

    nbp2.published = True  # NBP publishes today's table: live rates move
    fresh = Fx(ctx.store, http=nbp2)
    assert await fresh.rate("EUR", t) == Decimal("4.40")
    res = await verify.verify_deal(ctx.store, d.id, ctx.cfg, fresh, None, {ad.name: ad}, ctx.fetcher)
    assert res.ok, res.diffs
    assert res.net_now_cons_pln == d.cons_net_pln


def test_round_today_is_the_local_date_like_fx(tmp_path, monkeypatch):
    """Minor: Fx pins by the LOCAL date (date.today()); the round's `today` must be the same local date, not UTC."""
    import time as _time
    monkeypatch.setenv("TZ", "Europe/Warsaw")
    _time.tzset()
    try:
        ctx = make_ctx(tmp_path, FakeAdapter([]), None)
        ctx.now = lambda: datetime(2026, 10, 29, 23, 30, tzinfo=timezone.utc)  # 00:30 on the 30th in Warsaw
        from datetime import date
        assert scheduler._Round(ctx).today == date(2026, 10, 30)
    finally:
        monkeypatch.delenv("TZ")
        _time.tzset()


# ------------------------------------------------------------------------------------------------ final review I4


async def test_finalist_triage_keep_demote_exclude(tmp_path):
    """I4b: a tier <= 2 candidate is reviewed by the senior model (prompt.triage, listing in <listing>) BEFORE the live
    re-check and the push: ok keeps the tier, suspicious caps it at 3, scam is a hard exclude with reason triage."""
    for verdict, want in (("ok", 1), ("suspicious", 3), ("scam", None)):
        ad = FakeAdapter([listing("5", "RTX 3060 12GB używana", "6400")])
        llm = FakeLLM(LLM_3060, triage={"verdict": verdict, "p_scam": 0.4, "reasons": ["price far below market"]})
        ctx = tier1_ctx(tmp_path / verdict, ad, llm)
        st = await scheduler.run_round(ctx)
        assert len(llm.triage_calls) == 1
        role, user, system = llm.triage_calls[0]
        assert role == "senior" and user.startswith("<listing>") and "RTX 3060 12GB" in user
        assert system == next(i for i in load_items(REPO) if i["id"] == "prompt.triage")["value"]
        deals = ctx.store.deals()
        if want is None:
            assert deals == [] and st.dropped.get("triage_exclude") == 1 and ctx.pushes == []
            assert ad.alive_calls == 0  # excluded BEFORE the live re-check
            continue
        [d] = deals
        assert d.tier == want, (verdict, d.tier, d.reasons)
        assert len(ctx.pushes) == (1 if want == 1 else 0)
        ev = json.loads(d.evidence_json)
        assert ev["triage"]["verdict"] == {"ok": "keep", "suspicious": "demote"}[verdict]
        from dh import cards
        md, data = cards.from_deal(d)
        assert "Triage" in md and data["triage"]["verdict"] == ev["triage"]["verdict"]
        if want == 3:
            assert any("triage" in r for r in d.reasons) and "price far below market" in md


async def test_triage_unavailable_caps_confidence_at_medium(tmp_path):
    """I4b: no triage (LLM error, no LLM) -> the tier stands but confidence is at most medium: never a tier-1 push."""
    from dh.llm import LLMError
    for name, llm in (("error", FakeLLM(LLM_3060, triage=LLMError("down"))), ("malformed", FakeLLM(LLM_3060, triage={"x": 1})),
                      ("nollm", None)):
        ad = FakeAdapter([listing("5", "RTX 3060 12GB używana", "6400")])
        ctx = make_ctx(tmp_path / name, ad, llm, dry=False)
        for i in range(14):
            ctx.store.add_comp(KEY, "used", "olx", Decimal("9000") + i, NOW - timedelta(days=1 + i), "owner_sale", f"t:{i}")
        await scheduler.run_round(ctx)
        [d] = ctx.store.deals()
        assert d.tier == 2 and d.confidence == "medium" and ctx.pushes == [], (name, d.tier, d.confidence)
        assert json.loads(d.evidence_json)["triage"]["verdict"] == "unavailable"


async def test_triage_result_is_reused_for_the_same_title_and_price(tmp_path):
    ad = FakeAdapter([listing("5", "RTX 3060 12GB używana", "6400")])
    llm = FakeLLM(LLM_3060)
    ctx = tier1_ctx(tmp_path, ad, llm, reeval_hours=0)
    await scheduler.run_round(ctx)
    await scheduler.run_round(ctx)
    assert len(llm.triage_calls) == 1


def _cadence_items(minutes):
    from dh.improve.playbook import Item, Playbook
    q = [{"id": "q.rtx3060", "kind": "query", "target": "all", "value": "rtx 3060", "sentinel": False}]
    if minutes is None:
        return q
    pb = Playbook.from_file(REPO)
    item = Item("cadence.allegro_lokalnie", "cadence", "allegro_lokalnie", minutes)
    # an owner may already carry a cadence item for this source in playbook/items.yaml: overwrite it instead of
    # adding a second one ("add: item exists" is a PlaybookError, and the test is about the value, not the op)
    pb = pb.apply(pb.make_delta("update" if pb.get(item.id) else "add", item))
    return q + [i for i in pb.as_dicts() if i["kind"] == "cadence"]


async def test_promoted_cadence_item_spaces_the_sources_searches(tmp_path):
    """I4a: a cadence item (minutes between searches of a source) makes the plan skip the source's bandit arms until
    that many minutes passed since its last searched round."""
    t0 = datetime.now(timezone.utc)
    clock = {"t": t0}
    for minutes, expected in ((None, [1, 1, 1]), (30, [1, 0, 1])):
        ad = FakeAdapter([])
        ctx = make_ctx(tmp_path / str(minutes), ad, None, items=_cadence_items(minutes))
        ctx.now = lambda: clock["t"]
        ran = []
        for dt in (0, 10, 31):
            clock["t"] = t0 + timedelta(minutes=dt)
            before = len(ad.searches)
            st = await scheduler.run_round(ctx)
            ran.append(len(ad.searches) - before)
        assert ran == expected, (minutes, ran)
        if minutes:
            assert st.cadence == {"allegro_lokalnie": {"minutes": 30, "due": True, "cap": 10}}


async def test_fast_cadence_raises_the_sources_per_round_cap(tmp_path):
    """I4a: a cadence shorter than the round interval multiplies the source's per-round search cap."""
    qs = [{"id": f"q{i}", "kind": "query", "target": "all", "value": f"rtx 30{i:02d}", "sentinel": False} for i in range(30)]
    for minutes, want in ((None, 10), (5, 20)):  # round_interval_s 600 = 10 min: cadence 5 min -> x2
        items = qs + ([] if minutes is None else [{"id": "cadence.al", "kind": "cadence", "target": "allegro_lokalnie",
                                                   "value": minutes}])
        ad = FakeAdapter([])
        ctx = make_ctx(tmp_path / str(minutes), ad, None, items=items, budget=40, budget_max=40)
        await scheduler.run_round(ctx)
        assert len(ad.searches) == want, (minutes, len(ad.searches))


# ------------------------------------------------------------------------------------------------ final review I5


def _bulk_market(st, n=2000, gone_share=0.3):
    """n saleable listings of KEY on olx (PLN) seen over the last 25 days, a share of them disappeared in the last
    3 days after a 2-day life (so clears_at has probably-sold rows), each with two observations."""
    import random as _r
    ledger.migrate(st)
    rnd = _r.Random(7)
    now = NOW
    rows, ex, obs = [], [], []
    for i in range(n):
        first = now - timedelta(days=rnd.uniform(3, 25))
        gone = i < n * gone_share
        last = now - timedelta(days=rnd.uniform(0, 2.5)) if gone else now - timedelta(minutes=rnd.uniform(1, 600))
        price = Decimal(1300 + rnd.randint(0, 300))
        dis = last if gone else None
        appeared = (dis - timedelta(days=2)) if gone else None
        rows.append((i + 1, "olx", f"b{i}", f"https://olx.pl/d/{i}", "RTX 3060 12GB", "", str(price), "PLN", "PL",
                     "Krakow", f"s{i}", first.isoformat(), last.isoformat(), None if dis is None else dis.isoformat(),
                     None if appeared is None else appeared.isoformat()))
        ex.append((i + 1, KEY, "gpu", 0.95, "used", "[]", 0, 0, 0, "[]", "rules"))
        obs.append((i + 1, str(price + 50), first.isoformat()))
        obs.append((i + 1, str(price), (first + (last - first) / 2).isoformat()))
    st.conn.executemany(
        "insert into listings (id, source, source_id, url, title, description, price, currency, country, city, "
        "seller_hash, first_seen_at, last_seen_at, disappeared_at, appeared_at) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows)
    st.conn.executemany("insert into extractions (listing_id, key, category, certainty, condition, defects, box_only, "
                        "for_parts, wanted_ad, bundle_items, method) values (?,?,?,?,?,?,?,?,?,?,?)", ex)
    st.conn.executemany("insert into observations (listing_id, price, seen_at) values (?,?,?)", obs)
    st.conn.commit()


async def test_estimate_phase_is_fast_with_a_large_ledger(tmp_path):
    """I5 benchmark: 2,000 listings of one key, 100 candidates. The ledger comps (clears_at, active asks) are
    computed once per (key, condition) per round and the median asks come from one index per (key, currency); the
    estimate phase stays within a few seconds (it was O(candidates x disappeared x listings) queries)."""
    import time as _time
    ctx = make_ctx(tmp_path, FakeAdapter([]), None)
    _bulk_market(ctx.store)
    r = scheduler._Round(ctx)
    await r._warm_rates()
    lids = [row[0] for row in ctx.store.conn.execute(
        "select id from listings where disappeared_at is null order by id limit 100")]
    t0 = _time.monotonic()
    limit = 5.0
    ests = []
    for lid in lids:
        for ch in r.enabled_channels:
            ests.append(r._estimate(KEY, "used", ch, lid))
        assert _time.monotonic() - t0 < limit, f"estimate phase too slow: {len(ests)} estimates in {limit:.0f} s"
    took = _time.monotonic() - t0
    print(f"estimate phase: {len(ests)} estimates for {len(lids)} candidates in {took:.2f} s")
    assert took < limit, took
    e = next(x for x in ests if x is not None)
    kinds = {c.kind for c in e.comps}
    assert {"probably_sold", "active_ask"} <= kinds, kinds


async def test_cached_ledger_rows_give_the_same_estimate_and_drop_the_candidate(tmp_path):
    """I5: the per-round ledger cache must not change an estimate, and a candidate is never its own comparable."""
    from dh import valuation
    ctx = make_ctx(tmp_path, FakeAdapter([]), None)
    _bulk_market(ctx.store, n=120)
    r = scheduler._Round(ctx)
    lid = ctx.store.conn.execute("select id from listings where disappeared_at is null limit 1").fetchone()[0]
    for ch in ("olx", "allegro"):
        fresh = valuation.estimate(ctx.store, KEY, "used", ch, ctx.now(), params=r.params, channels=r.channels,
                                   exclude_listing_ids=[lid])
        cached = r._estimate(KEY, "used", ch, lid)
        assert (fresh is None) == (cached is None)
        if fresh is not None:
            assert (fresh.p25_pln, fresh.median_pln, fresh.p75_pln, fresh.n_effective, len(fresh.comps)) == \
                   (cached.p25_pln, cached.median_pln, cached.p75_pln, cached.n_effective, len(cached.comps))
            assert f"https://olx.pl/d/{lid - 1}" not in {c.ref for c in cached.comps}


def test_median_ask_index_matches_the_reference(tmp_path):
    """I5: the indexed median ask (one load per key and currency) equals the per-call SQL computation."""
    st = Store(tmp_path / "t.db")
    _bulk_market(st, n=300)
    cache: dict = {}
    for h in (1, 10, 30, 70, 200, 500):
        t = NOW - timedelta(hours=h)
        assert ledger.median_ask(st, KEY, "PLN", t, ledger.ASK_WINDOW, cache=cache) == \
               ledger._median_ask(st, KEY, "PLN", t, ledger.ASK_WINDOW)


# ------------------------------------------------------------------------------------------------ minor: gone


async def test_deal_whose_listing_is_gone_gets_status_gone(tmp_path):
    """A listing that is gone at the live re-check, or confirmed gone after disappearing, turns its deal into
    status "gone" (card rewritten so the publisher updates it); a deal found alive again is "new" again."""
    ad = FakeAdapter([listing()])
    ctx = make_ctx(tmp_path, ad, FakeLLM(LLM_3060))
    seed_comps(ctx.store)
    await scheduler.run_round(ctx)
    [d] = ctx.store.deals()
    assert d.status == "new"
    ad.alive_result = (False, None)
    ad.listings = [listing(price="690")]  # a price change: re-evaluated, re-checked live -> gone
    s = await scheduler.run_round(ctx)
    assert s.dropped.get("gone") == 1
    d = ctx.store.get_deal(d.id)
    assert d.status == "gone"
    md = next((tmp_path / "cards" / "deals").glob(f"*/*-{d.id}.md"))
    assert "Status: gone" in md.read_text()
    ad.alive_result = (True, None)
    ad.listings = [listing(price="680")]
    await scheduler.run_round(ctx)
    assert ctx.store.get_deal(d.id).status == "new"


async def test_disappeared_listing_confirmed_dead_marks_its_deal_gone(tmp_path):
    ad = FakeAdapter([listing()])
    ctx = make_ctx(tmp_path, ad, FakeLLM(LLM_3060))
    seed_comps(ctx.store)
    await scheduler.run_round(ctx)
    [d] = ctx.store.deals()
    ad.alive_result = (False, None)
    r = scheduler._Round(ctx)
    await r._confirm_disappeared(ad, ad.name, [d.listing_ref])
    assert ctx.store.get_deal(d.id).status == "gone"


async def test_triage_cache_is_keyed_by_the_prompt_sha(tmp_path):
    """A cached triage is reused only under the same prompt.triage; a promoted new prompt re-triages the listing."""
    q = {"id": "q.rtx3060", "kind": "query", "target": "all", "value": "rtx 3060", "sentinel": False}

    def items(text):
        return [q, {"id": "prompt.triage", "kind": "prompt", "target": "senior", "value": text, "sentinel": False}]

    ad = FakeAdapter([listing("5", "RTX 3060 12GB używana", "6400")])
    llm = FakeLLM(LLM_3060)
    ctx = tier1_ctx(tmp_path, ad, llm, items=items("triage prompt A"), reeval_hours=0)
    await scheduler.run_round(ctx)
    assert len(llm.triage_calls) == 1
    await scheduler.run_round(ctx)  # same prompt: cached
    assert len(llm.triage_calls) == 1
    ctx.items = items("triage prompt B")  # a promoted prompt
    st = await scheduler.run_round(ctx)
    assert len(llm.triage_calls) == 2 and llm.triage_calls[1][2] == "triage prompt B"
    assert st.triage.get("cached", 0) == 0


def test_triage_table_without_prompt_sha_is_migrated(tmp_path):
    ctx = make_ctx(tmp_path, FakeAdapter([]), None)
    ctx.store.conn.execute("drop table if exists triage")
    ctx.store.conn.execute("create table triage (listing_id integer primary key, title_hash text, price text, "
                           "result text not null, at text not null)")
    ctx.store.conn.execute("insert into triage values (1, 'h', '10', '{}', 'x')")
    ctx.store.conn.commit()
    scheduler._Round(ctx)
    cols = {r[1] for r in ctx.store.conn.execute("pragma table_info(triage)")}
    assert "prompt_sha" in cols
    assert ctx.store.conn.execute("select prompt_sha from triage where listing_id=1").fetchone()[0] is None


# ------------------------------------------------------------------ deferred minors: paid booking, bandit clamp


class PaidLLM(FakeLLM):
    """A paid endpoint: every call costs $0.01, reported through ctx.on_paid like llm.Client(on_paid=...) does;
    `paid_booked` records what the round handed back after booking it."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.ctx, self.booked = None, []

    async def chat_json(self, role, system, user, schema, max_tokens=600):
        self.ctx.on_paid(role, 0.01)
        return await super().chat_json(role, system, user, schema, max_tokens)

    def paid_booked(self, usd):
        self.booked.append(usd)


async def test_round_books_paid_spend_once_and_clears_the_clients_unbooked_counter(tmp_path):
    """Wiring minor: the round writes the paid_api cost row, then hands exactly that amount to llm.paid_booked and
    zeroes ctx.usage["paid_usd"], so the client's cap check (books + unbooked) never counts it twice."""
    llm = PaidLLM(LLM_3060)
    ctx = make_ctx(tmp_path, FakeAdapter([listing()]), llm)
    llm.ctx = ctx
    seed_comps(ctx.store)
    s1 = await scheduler.run_round(ctx)
    n_calls = len(llm.calls) + len(llm.triage_calls)
    assert n_calls >= 2 and s1.llm_calls == n_calls
    assert llm.booked == [pytest.approx(0.01 * n_calls)] and ctx.usage["paid_usd"] == 0.0
    pln_per_usd = float(ctx.cfg.models.paid_api.pln_per_usd)
    row = ctx.store.conn.execute("select amount_pln from costs where kind='paid_api' and round_id=?",
                                 (s1.round_id,)).fetchone()
    assert float(row["amount_pln"]) == pytest.approx(0.01 * n_calls * pln_per_usd, abs=0.01)
    s2 = await scheduler.run_round(ctx)  # nothing new to evaluate: no paid call, nothing more booked
    assert len(llm.booked) == 1 and ctx.usage["paid_usd"] == 0.0
    row2 = ctx.store.conn.execute("select amount_pln from costs where kind='paid_api' and round_id=?",
                                  (s2.round_id,)).fetchone()
    assert float(row2["amount_pln"]) == 0.0


async def test_a_negative_net_trend_deal_updates_its_arm_with_zero_value(tmp_path, monkeypatch):
    """Behavioural test of the bandit clamp (was source inspection): a tier-4 trend deal with a negative expected
    net makes value_units negative; the round must still finish and the arm gets one pull with reward 0."""
    import dataclasses

    from dh.bandit import value_units
    ad = FakeAdapter([listing("1", "Gigabyte RTX 3060 Gaming OC używana", "1395")])
    ctx = make_ctx(tmp_path, ad, FakeLLM(LLM_3060))
    seed_comps(ctx.store)
    _with(ctx, scheduler={"trend": {"min_30d": 0.15, "min_cons_net_usd": -100000}})
    real = scheduler._Round._estimate

    def rising(self, *a, **kw):
        e = real(self, *a, **kw)
        return None if e is None else dataclasses.replace(e, trend_30d=0.3)
    monkeypatch.setattr(scheduler._Round, "_estimate", rising)
    st = await scheduler.run_round(ctx)
    ds = ctx.store.deals()
    assert [d.tier for d in ds] == [4] and ds[0].exp_net_usd < 0, [(d.tier, d.exp_net_usd) for d in ds]
    assert value_units([(4, float(ds[0].exp_net_usd))]) < 0  # the raw value would be refused by Bandit.update
    arm = ledger.arm_id("allegro_lokalnie", "rtx 3060")
    row = ctx.store.conn.execute("select pulls, reward from arm_stats where arm=?", (arm,)).fetchone()
    assert row["pulls"] == 1 and row["reward"] == 0.0
    assert st.round_id is not None and ctx.store.conn.execute(
        "select notes from rounds where id=?", (st.round_id,)).fetchone()["notes"]  # bookkeeping completed


async def test_without_the_llm_a_laptop_naming_a_gpu_is_never_valued_as_the_card(tmp_path):
    """Live round 29 Oct: "MSI ... Core Ultra 9 275HX ... RTX 5090" (a laptop) became a T3 deal on the rules key.
    A weak rules key (a system/lot, or certainty < 0.7) waits for the LLM instead of being valued on its own."""
    lap = listing("9", "Ryzen 7 7800X3D 2TB RTX 3060 12GB", "900")  # rules: gpu:rtx-3060:12, a system, 0.5
    ctx = make_ctx(tmp_path, FakeAdapter([lap]), None)  # the LLM is off
    seed_comps(ctx.store)  # 900 PLN against ~1400 PLN cards: a "deal" if valued as a bare card
    st = await scheduler.run_round(ctx)
    assert ctx.store.deals() == [] and st.dropped.get("weak_match_waits_for_llm") == 1, st.dropped
    note = ctx.store.conn.execute("select note from evaluations").fetchone()["note"]
    assert note == "llm_pending"  # evaluated again as soon as the LLM is back


async def test_without_the_llm_a_plain_card_is_still_valued(tmp_path):
    ctx = make_ctx(tmp_path, FakeAdapter([listing()]), None)
    seed_comps(ctx.store)
    st = await scheduler.run_round(ctx)
    assert st.candidates == 1 and "weak_match_waits_for_llm" not in st.dropped


# ------------------------------------------------------------------ validation: a foreign model loaded mid-round


async def test_a_model_loaded_mid_round_stops_the_rounds_llm_calls(tmp_path):
    """Validation 30 Oct: another workload loaded its model after the round-start guard had passed. Every later LLM
    call would make llama-swap load the role model back and evict theirs; the round's calls stop instead, the
    listings wait (llm_pending), and one health event says why."""
    checks = []

    async def running():
        checks.append(1)
        return [] if len(checks) == 1 else ["gemma-4-26b-a4b"]  # clean at the round-start guard, then not
    llm = FakeLLM(LLM_3060)
    ctx = make_ctx(tmp_path, FakeAdapter([listing("1"), listing("2", price="720")]), llm)
    ctx.llm_check = running
    seed_comps(ctx.store)
    st = await scheduler.run_round(ctx)
    assert llm.calls == [] and llm.triage_calls == [] and st.llm_calls == 0
    assert st.llm_mode == "foreign_model" and st.foreign_models == ["gemma-4-26b-a4b"]
    notes = {r["note"] for r in ctx.store.conn.execute("select note from evaluations")}
    assert notes == {"llm_pending"}  # valued on the rules for now (certainty 0.7), re-evaluated with the LLM later
    assert ctx.store.conn.execute("select count(*) from health where kind='foreign_model'").fetchone()[0] == 1


async def test_counting_llm_checks_before_every_call_and_shares_the_check_in_flight():
    """Review: a 15 s cache let a call through up to 15 s after another workload loaded its model, and concurrent
    callers did not share a check. Every call now awaits a check that is in flight or starts a new one."""
    from dh.llm import LLMUnavailable
    checks, foreign, gate = [], [[]], asyncio.Event()

    async def running():
        checks.append(1)
        await gate.wait()  # slow /running: the concurrent calls must all wait for this one check
        return foreign[0]

    class Model:
        def __init__(self):
            self.n = 0

        async def chat_json(self, *a, **kw):
            self.n += 1
            return {"ok": True}
    seen = []
    c = scheduler._CountingLLM(Model(), scheduler.RoundStats(), running, seen.append)
    calls = [asyncio.create_task(c.chat_json("worker", "s", "u", {})) for _ in range(4)]
    await asyncio.sleep(0)
    gate.set()
    assert [await t for t in calls] == [{"ok": True}] * 4 and len(checks) == 1  # one shared check
    foreign[0] = ["qwen3.8-flash-next-uncensored"]  # loaded right after: the very next call sees it (no cache)
    with pytest.raises(LLMUnavailable, match="outside the roles"):
        await c.chat_json("worker", "s", "u", {})
    assert len(checks) == 2 and c._inner.n == 4 and seen == [["qwen3.8-flash-next-uncensored"]]
    foreign[0] = []
    assert await c.chat_json("worker", "s", "u", {}) == {"ok": True} and len(checks) == 3


async def test_warm_rates_cover_every_adapter_currency(tmp_path):
    """Price bounds and PLN valuation silently skip a currency whose rate was never warmed
    (the live log showed "no PLN rate for CHF" once eBay CH went live): keep the warm list complete."""
    from dh.sources import registry
    from dh.sources import registry
    ctx = make_ctx(tmp_path, FakeAdapter([]), None)
    r = scheduler._Round(ctx)
    await r._warm_rates()
    need = {a.currency for a in registry(None, REPO).values() if getattr(a, "currency", None)}
    missing = {c for c in need if c != "PLN" and c not in r.to_pln_rates}
    assert not missing, f"round-start rate warm-up misses {sorted(missing)}"
