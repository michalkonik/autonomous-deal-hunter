"""Review round 1, item 8: the offline judge (replay.Judge) must decide like the production round.

Every must-pass listing goes through the production evaluation path, `scheduler._Round._evaluate`, with fakes for
the network (the detail fetch returns the listing unchanged, the photo check finds nothing, the live re-check is
unavailable, nothing is pushed). It also goes through `replay.Judge.judge`. Both value against the SAME store (comps
seeded from each case's market) with the same fixed FX. The key, the exclusion and the tier band must agree.
"""
import asyncio
import random
from datetime import timedelta
from decimal import Decimal

import pytest

from dh import config, scheduler
from dh.improve import gates, replay
from dh.sources.base import AdapterError, Query
from dh.store import Store
from improve_fix import REPO, seed_playbook

NOW = gates.EVAL_NOW


class FakeAd:
    domain, enabled, via_mailbox = "example.invalid", True, False

    async def detail(self, l, fetcher):
        return l

    async def alive(self, l, fetcher):
        raise AdapterError("no live re-check offline")


async def _no_photos(*a, **kw):
    return []


async def _false(*a, **kw):
    return False


def seed_market(store, cases):
    """Comps around each case market: P25 and median land near the case's numbers (owner sales, used)."""
    seen = set()
    for c in cases:
        m = c.get("market")
        if not m or m["key"] in seen:
            continue
        seen.add(m["key"])
        p25, med = Decimal(str(m["p25_pln"])), Decimal(str(m["median_pln"]))
        for i in range(20):
            price = p25 - (med - p25) + (med - p25) * Decimal(i) / Decimal(5)
            for ch in ("allegro", "olx"):
                store.add_comp(m["key"], "used", ch, price.quantize(Decimal("0.01")), NOW - timedelta(days=1 + i % 25),
                               "owner_sale", f"eq:{m['key']}:{ch}:{i}")


def band(tier):
    return "deal" if tier in (1, 2, 3) else ("near" if tier == 4 else None)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "dh.db")
    yield s
    s.close()


def test_offline_judge_matches_the_production_round(store, tmp_path):
    cases = gates.load_cases(gates.MUST_PASS)
    seed_market(store, cases)
    cfg = config.load(REPO)
    ctx = scheduler.RoundContext(
        cfg=cfg, root=REPO, store=store, fetcher=None, adapters={}, llm=None, fx=replay.TableFx(gates.EVAL_FX),
        customs=None, bandit=None, rng=random.Random(0), cards_root=tmp_path / "cards", shots_root=tmp_path / "shots",
        dry=True, now=lambda: NOW, push=_false, screenshot=_false, photo_check=_no_photos)
    rnd = scheduler._Round(ctx)
    judge = replay.Judge(seed_playbook(), store, cfg=cfg, fx=replay.TableFx(gates.EVAL_FX), now=NOW)
    arm = scheduler.Arm("olx:eq", "olx", Query(text="eq", arm_id="olx:eq"), "query.eq")
    ad = FakeAd()
    diffs = []

    async def go():
        for c in cases:
            l = gates.case_listing(c)
            lid = store.upsert_listing(l)
            before = dict(rnd.stats.dropped)
            await rnd._evaluate(l, lid, arm, ad)
            dropped = {k for k, v in rnd.stats.dropped.items() if v > before.get(k, 0)}
            ev = store.conn.execute("select tier, note from evaluations where listing_id=?", (lid,)).fetchone()
            ex = store.conn.execute("select key from extractions where listing_id=?", (lid,)).fetchone()
            prod = (ex["key"] if ex else None, band(ev["tier"]),
                    bool(dropped & {"bait_or_parts", "no_key", "price_range", "hard_exclude"}))
            o = await judge.judge(l, lid=lid)
            off = (o.key, band(o.tier), o.excluded)
            if prod != off:
                diffs.append((c["id"], prod, off, ev["note"], sorted(dropped), o.reason))
    asyncio.run(go())
    assert not diffs, diffs
