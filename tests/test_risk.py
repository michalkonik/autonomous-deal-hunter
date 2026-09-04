from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from dh import config, risk
from dh.models import Extraction, Listing

REPO = Path(__file__).resolve().parent.parent
CFG = config.load(REPO)


@dataclass
class Est:  # stand-in for valuation.ExitEstimate (only the fields risk reads)
    median_pln: Decimal = Decimal("5000")
    p25_pln: Decimal = Decimal("4600")
    p75_pln: Decimal = Decimal("5400")
    n_sold: int = 20
    comps: list = field(default_factory=list)


def listing(**kw) -> Listing:
    base = dict(source="olx", source_id="1", url="https://olx.pl/1", title="RTX 4090 24GB Gigabyte Gaming OC",
                description="Karta w pelni sprawna, uzywana, odbior osobisty Krakow.", price=Decimal("5000"),
                currency="PLN", country="PL", city="Krakow", lat=None, lon=None, seller_hash="h",
                seller_age_days=400, seller_ratings=50, seller_score=4.9, ships=True, pickup_only=False,
                is_business=False, photos=["a.jpg", "b.jpg"], posted_at=None,
                seen_at=datetime(2026, 10, 29, tzinfo=timezone.utc))
    base.update(kw)
    return Listing(**base)


def ext(**kw) -> Extraction:
    base = dict(key="gpu:rtx-4090:24", category="gpu", certainty=0.95, condition="used")
    base.update(kw)
    return Extraction(**base)


def assess(l=None, e=None, est=None, **kw):
    return risk.assess(l or listing(), e or ext(), Est() if est is None else est, CFG, **kw)


def test_hard_excludes():
    assert assess(e=ext(box_only=True)).hard_exclude
    assert assess(e=ext(wanted_ad=True)).hard_exclude
    r = assess(e=ext(for_parts=True))
    assert r.hard_exclude and "for_parts" in r.flags
    for desc, flag in [("Zadzwon 600 123 456", "offplatform_phone"), ("tel +48 600-123-456", "offplatform_phone"),
                       ("pisz na WhatsApp", "offplatform_whatsapp"), ("kontakt Telegram @x", "offplatform_telegram"),
                       ("wiecej zdjec https://foto.example/x", "offplatform_url"),
                       ("zdjecia na www.foto.example", "offplatform_url"),
                       ("PayPal Friends & Family", "offplatform_paypal_ff")]:
        r = assess(l=listing(description=desc))
        assert r.hard_exclude and flag in r.flags, (desc, r.flags)
    # a transfer request alone is fine; with a new account it is a hard exclude
    pay = listing(description="Platnosc: przelew na konto")
    assert not assess(l=pay).hard_exclude
    r = assess(l=replace(pay, seller_age_days=5))
    assert r.hard_exclude and "transfer_new_account" in r.flags


def test_for_parts_allowed_when_repair_enabled():
    cfg = config.Config({**CFG.to_dict(), "rules": {**CFG.rules.to_dict(), "repair": {"enabled": True}}})
    assert not risk.assess(listing(), ext(for_parts=True), Est(), cfg).hard_exclude


def test_no_false_phone_on_specs_and_prices():
    r = assess(l=listing(description="24 GB GDDR6X, 384 bit, 450 W, cena 5 000 zl, SN 1234567890123"))
    assert not r.hard_exclude


def test_too_cheap_new_account_is_risky():
    r = assess(l=listing(price=Decimal("2800"), seller_age_days=5, seller_ratings=0))
    assert r.p_scam > 0.3 and not r.hard_exclude
    assert {"too_cheap", "new_account", "no_ratings"} <= set(r.flags)
    # even without the zero ratings: -4.5 + 2.5 + 1.5 -> ~0.38
    assert assess(l=listing(price=Decimal("2800"), seller_age_days=5)).p_scam > 0.3


def test_normal_listing_low_risk():
    r = assess()
    assert r.p_scam < 0.05 and not r.hard_exclude and r.flags == []
    # a mildly cheap price is not suspicious, and one weak signal alone stays below 0.15
    assert assess(l=listing(price=Decimal("4400"))).p_scam < 0.05
    assert assess(l=listing(seller_age_days=5)).p_scam < 0.15


def test_flagship_signals():
    cn = assess(l=listing(country="CN"))
    assert "origin_cn_hk" in cn.flags and cn.p_scam > assess().p_scam
    assert "origin_cn_hk" not in assess(l=listing(country="CN"), e=ext(key="gpu:rtx-3070:8")).flags
    nophoto = assess(l=listing(photos=[]))
    assert "no_photos_flagship" in nophoto.flags
    assert "no_photos_flagship" not in assess(l=listing(photos=[]), e=ext(key="gpu:rtx-4080:16")).flags
    assert "stock_photo" in assess(stock_photo=True).flags
    assert assess(stock_photo=True).p_scam > assess().p_scam


def test_foreign_currency_needs_price_pln():
    l = listing(price=Decimal("600"), currency="EUR")
    assert "too_cheap" not in assess(l=l).flags            # cannot compare EUR with a PLN median
    assert "too_cheap" in assess(l=l, price_pln=Decimal("2600")).flags


def test_no_estimate_is_fine():
    r = risk.assess(listing(), ext(), None, CFG)
    assert 0 <= r.p_scam < 0.05


def test_scam_keywords_come_from_playbook(tmp_path):
    (tmp_path / "playbook").mkdir()
    (tmp_path / "playbook" / "items.yaml").write_text(yaml.safe_dump([
        {"id": "scam_kw.x", "kind": "scam_kw", "target": "risk", "value": "tylko przedpłata"}]))
    l = listing(description="Wysylka, TYLKO PRZEDPLATA")
    r = risk.assess(l, ext(), Est(), CFG, root=tmp_path)
    assert "scam_kw.x" in r.flags and r.p_scam > assess().p_scam and not r.hard_exclude
    # the seed playbook ships keywords too
    assert any(i["kind"] == "scam_kw" and i["target"] == "risk"
               for i in __import__("dh.playbook_io", fromlist=["x"]).load_items(REPO))
    assert "scam_kw.prepayment_only" in assess(l=l).flags


def test_checklist():
    gpu = assess().checklist
    text = " ".join(gpu).lower()
    for needle in ("platform", "gpu-z", "device id", "vram", "12v-2x6", "die side", "serial"):
        assert needle in text
    assert any("ram" in c.lower() or "memtest" in c.lower() for c in assess(e=ext(key="ram:ddr5:32:2x16", category="ram")).checklist)
