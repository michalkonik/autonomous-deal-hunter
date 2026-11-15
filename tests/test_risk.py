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
    assert assess(e=ext(condition="for_parts")).hard_exclude
    # final review I3 (controller ruling): a bare phone number or e-mail is a scam-score feature, not a hard exclude
    for desc in ("Zadzwon 600 123 456", "tel +48 600-123-456", "tel. 600123456", "Tel 600123456", "Tel: 600123456",
                 "Kontakt: 600123456", "zadzwoń 600123456", "ZADZWOŃ 600123456", "Telefon: 600123456",
                 "Zadzwoń +48600123456", "pisz +49 170 1234567", "mail: jan.kowalski@example.pl"):
        r = assess(l=listing(description=desc))
        assert not r.hard_exclude and "contact_in_text" in r.flags, (desc, r.flags)
    for desc, flag in [("pisz na WhatsApp", "offplatform_whatsapp"), ("kontakt Telegram @x", "offplatform_telegram"),
                       ("napisz wa.me/48600123456", "offplatform_whatsapp"), ("t.me/sprzedawca", "offplatform_telegram"),
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


@pytest.mark.parametrize("desc", [
    "Handy 0176 1234 5678", "pisz +49 176 12345678", "ruf an 0049 176 12345678", "Rufnummer: 0176 12345678",
    "volejte +420 777 123 456", "Mobil: 777 123 456", "hivj +36 30 123 4567", "telefon 06 30 123 4567",
    "Tel 06-30-123-4567", "+36 30 123 4567"])
def test_de_cz_hu_mobile_formats_are_phone_numbers(desc):
    r = assess(l=listing(description=desc))
    assert not r.hard_exclude and "contact_in_text" in r.flags, (desc, r.flags)


@pytest.mark.parametrize("desc", [
    "Seriennummer 0176123456789012", "Part no 0176 1234 5678 9012", "Artikel 06 30 123 4567 8", "Model 0123456",
    "GTX 1080 8GB 256bit 2020", "ISBN 978-3-16-148410-0", "Nr 06301234567"])
def test_serials_and_part_numbers_are_not_phone_numbers(desc):
    r = assess(l=listing(description=desc))
    assert "contact_in_text" not in r.flags, (desc, r.flags)


def test_redacted_contact_flag_is_the_contact_feature():
    clean = listing(description="Karta sprawna, [redacted]")
    assert not assess(l=clean).hard_exclude and "contact_in_text" not in assess(l=clean).flags
    r = assess(l=replace(clean, contact_in_text=True))
    assert not r.hard_exclude and "contact_in_text" in r.flags and r.p_scam > assess(l=clean).p_scam


def test_contact_weight_pinned_by_the_ruling():
    """I3 ruling: a normal listing with a phone stays p_scam < 0.05 (it can still be tier 1); phone + new account +
    too cheap is > 0.5. Phone + too cheap from an established seller leaves "high" confidence (>= 0.05)."""
    phone = "Odbior osobisty Krakow, tel 600 123 456"
    assert CFG.economics.risk.weights.contact_in_text > 0
    assert assess(l=listing(description=phone)).p_scam < 0.05
    assert assess(l=listing(contact_in_text=True)).p_scam < 0.05
    bad = assess(l=listing(description=phone, price=Decimal("3000"), seller_age_days=5))
    assert {"contact_in_text", "too_cheap", "new_account"} <= set(bad.flags) and bad.p_scam > 0.5
    assert not bad.hard_exclude
    cheap = assess(l=listing(description=phone, price=Decimal("3000")))
    assert 0.05 <= cheap.p_scam < 0.5
    # the excludes that stay hard
    for desc in ("pisz na whatsapp", "telegram @x", "https://x.example", "paypal friends and family"):
        assert assess(l=listing(description=desc)).hard_exclude, desc
    assert assess(l=listing(description="przelew na konto", seller_age_days=3)).hard_exclude


@pytest.mark.parametrize("source,pickup,protected", [
    ("allegro_lokalnie", False, True), ("kleinanzeigen", False, True), ("ebay_de", False, True),
    ("ebay_pl", True, True), ("olx", False, True),
    ("bazos_cz", False, False), ("hardverapro", False, False), ("sprzedajemy", False, False),
    ("marktplaats", False, False),
    ("allegro_lokalnie", True, False), ("kleinanzeigen", True, False)])
def test_checklist_payment_line_depends_on_the_platform(source, pickup, protected):
    """I3: platforms with a protected checkout -> pay only through it; phone-first classifieds (and a pickup where
    the platform's protection does not apply) -> meet, test, pay only after testing, never in advance."""
    c = assess(l=listing(source=source, pickup_only=pickup, description="tel 600 123 456")).checklist
    text = " ".join(c).lower()
    if protected:
        assert "protected checkout" in text and "never pay in advance" not in text
    else:
        assert "protected checkout" not in text
        assert "meet in person" in text and "test before paying" in text and "gpu-z" in text
        assert "cash or instant transfer only after testing" in text and "never pay in advance" in text


def test_for_parts_allowed_when_repair_enabled():
    cfg = config.Config({**CFG.to_dict(), "rules": {**CFG.rules.to_dict(), "repair": {"enabled": True}}})
    assert not risk.assess(listing(), ext(for_parts=True), Est(), cfg).hard_exclude


def test_no_false_phone_on_specs_and_prices():
    for desc in ("24 GB GDDR6X, 384 bit, 450 W, cena 5 000 zl, SN 1234567890123",
                 "SN 123456789", "S/N: 600123456 gwarancja", "P/N 900-1G136-2514-000", "nr seryjny 123456789012", "P/N 900123456 rev2", "S/N: 123456789", "Hotel 123456789",
                 "kod 1234 5678 9012", "cena 3.500.000"):
        r = assess(l=listing(description=desc))
        assert not r.hard_exclude, (desc, r.flags)


def test_too_cheap_new_account_is_risky():
    r = assess(l=listing(price=Decimal("2800"), seller_age_days=5, seller_ratings=0))
    assert r.p_scam > 0.3 and not r.hard_exclude
    assert {"too_cheap", "new_account", "no_ratings"} <= set(r.flags)
    assert assess(l=listing(price=Decimal("3000"), seller_age_days=5)).p_scam > 0.30   # too cheap + new account
    assert assess(l=listing(price=Decimal("3000"), seller_ratings=0)).p_scam > 0.20    # too cheap + 0 ratings


def test_weights_review_round_1():
    # controller ruling: a deep discount alone from an established seller must not block tier 1
    assert assess().p_scam < 0.02
    est = assess(l=listing(price=Decimal("3000"), seller_age_days=180, seller_ratings=5))
    assert "too_cheap" in est.flags and "extreme_price" not in est.flags and est.p_scam < 0.05
    # extreme (< 35% of the median) + any one weak-seller signal
    for weak in (dict(seller_age_days=5), dict(seller_ratings=0), dict(photos=[])):
        r = assess(l=listing(price=Decimal("1700"), **weak), stock_photo=False)
        assert "extreme_price" in r.flags and r.p_scam > 0.5, (weak, r.p_scam)
    assert assess(l=listing(price=Decimal("1700")), stock_photo=True).p_scam > 0.5
    assert assess(l=listing(price=Decimal("1700"), country="CN")).p_scam > 0.5
    assert "extreme_price" not in assess(l=listing(price=Decimal("1750"))).flags      # exactly 35% is not below


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
    assert "origin_cn_hk" not in assess(l=listing(country="CN"), e=ext(key="gpu-mobile:rtx-4090:16")).flags
    assert "origin_cn_hk" not in assess(l=listing(country="CN"), e=ext(key="gpu:rx-7900-xtx:24")).flags
    for k in ("rtx-4080", "rtx-4080-super", "rtx-4090", "rtx-4090d", "rtx-5080", "rtx-5090",
              "rtx-6080", "rtx-6090"):
        assert "origin_cn_hk" in assess(l=listing(country="HK"), e=ext(key=f"gpu:{k}:16")).flags, k
    assert "origin_cn_hk" not in assess(l=listing(country="CN"), e=ext(key="gpu:rtx-6070:12")).flags
    nophoto = assess(l=listing(photos=[]))
    assert "no_photos_flagship" in nophoto.flags
    assert "no_photos_flagship" not in assess(l=listing(photos=[]), e=ext(key="gpu:rtx-4080:16")).flags
    assert "no_photos_flagship" in assess(l=listing(photos=[]), e=ext(key="gpu:rtx-6090:32")).flags
    assert "no_photos_flagship" not in assess(l=listing(photos=[]), e=ext(key="gpu:rtx-6080:20")).flags
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


def test_keywords_cached_per_mtime(tmp_path):
    import os
    (tmp_path / "playbook").mkdir()
    p = tmp_path / "playbook" / "items.yaml"
    p.write_text(yaml.safe_dump([{"id": "scam_kw.a", "kind": "scam_kw", "target": "risk", "value": "aaa"}]))
    assert risk._scam_keywords(tmp_path) == [("scam_kw.a", "aaa")]
    st = p.stat()
    p.write_text(yaml.safe_dump([{"id": "scam_kw.b", "kind": "scam_kw", "target": "risk", "value": "bbb"}]))
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))            # same mtime: served from cache
    assert risk._scam_keywords(tmp_path) == [("scam_kw.a", "aaa")]
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    assert risk._scam_keywords(tmp_path) == [("scam_kw.b", "bbb")]


def test_checklist():
    gpu = assess().checklist
    text = " ".join(gpu).lower()
    for needle in ("platform", "gpu-z", "device id", "vram", "12v-2x6", "die side", "serial"):
        assert needle in text
    assert not any("cash" in c.lower() for c in assess(l=listing(seller_age_days=3)).checklist)
    assert any("ram" in c.lower() or "memtest" in c.lower() for c in assess(e=ext(key="ram:ddr5:32:2x16", category="ram")).checklist)
