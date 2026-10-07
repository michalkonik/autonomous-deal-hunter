"""Task 22: alert mailbox. Fixtures are SYNTHETIC .eml files (tests/fixtures/mailbox/README.md)."""
from __future__ import annotations

import asyncio
import email
import json
from decimal import Decimal
from email import policy

import pytest

from dh.sources import AdapterError, Query, registry
from dh.sources import mailbox as mb
from dh.store import Store

from helpers import FIX, FakeFetcher, Res, fixture_text

run = asyncio.run
CREDS = json.dumps({"host": "imap.example.invalid", "port": 993, "user": "dh", "password": "pw", "folder": "INBOX"})


def eml(name: str) -> bytes:
    return (FIX / "mailbox" / name).read_bytes()


def parse(name: str):
    msg = email.message_from_bytes(eml(name), policy=policy.default)
    return mb.parse_message(msg, mb.platform_of(msg["From"]))


class FakeImap:
    def __init__(self, mails: dict[bytes, bytes]):
        self.mails, self.seen, self.ops = mails, [], []

    def login(self, u, p):
        self.ops.append(("login", u))
        return "OK", []

    def select(self, folder):
        self.ops.append(("select", folder))
        return "OK", [b"1"]

    def search(self, charset, crit):
        assert crit == "UNSEEN"
        return "OK", [b" ".join(sorted(self.mails))]

    def fetch(self, i, what):
        assert "PEEK" in what  # fetching must not set \Seen by itself
        return "OK", [(b"1 (BODY[] {1}", self.mails[i]), b")"]

    def store(self, i, op, flag):
        assert (op, flag) == ("+FLAGS", "\\Seen")
        self.seen.append(i)
        return "OK", []

    def logout(self):
        return "BYE", []


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


def make_box(tmp_path, mails, store=None, creds=CREDS):
    imap = FakeImap(mails)
    box = mb.Mailbox(secret_fn=lambda ref: creds if ref == "dh-mail#imap" else None, snapshot_dir=tmp_path / "snap",
                     imap_factory=lambda h, p: imap, store=store)
    return box, imap


# ------------------------------------------------------------------ per-platform parse
def test_allegro_parse():
    ls = parse("allegro.eml")
    by = {l.source_id: l for l in ls}
    assert set(by) == {"12345678901", "98765432100"}, "the 'do negocjacji' offer has no price and is skipped"
    a = by["12345678901"]
    assert (a.source, a.country, a.currency, a.price) == ("allegro", "PL", "PLN", Decimal("7499.00"))
    assert a.title == "Karta graficzna MSI GeForce RTX 4090 Suprim X 24GB"
    assert a.url == "https://allegro.pl/oferta/karta-graficzna-msi-geforce-rtx-4090-suprim-x-24gb-12345678901"
    assert a.photos == ["https://a.allegroimg.com/s180/aa.jpg"]
    assert a.posted_at is not None and a.posted_at.tzinfo is not None
    assert by["98765432100"].price == Decimal("6899")


def test_olx_parse():
    ls = parse("olx.eml")
    assert {l.source_id: l.price for l in ls} == {"Xyz12": Decimal("6800"), "Abc34": Decimal("7100")}
    assert all(l.source == "olx" and l.currency == "PLN" and "?" not in l.url for l in ls)


def test_kleinanzeigen_parse():
    ls = parse("kleinanzeigen.eml")
    assert {l.source_id: l.price for l in ls} == {"2876543210": Decimal("1450"), "2876543211": Decimal("1399")}
    assert all(l.source == "kleinanzeigen" and l.currency == "EUR" and l.country == "DE" for l in ls)


def test_ebay_parse_multipart():
    ls = parse("ebay.eml")
    assert {l.source_id: l.price for l in ls} == {"305123456789": Decimal("1389.00"), "305123456790": Decimal("1450.50")}
    assert all(l.source == "ebay_de" and l.currency == "EUR" for l in ls)


def test_unknown_sender_and_phone_redaction():
    assert mb.platform_of("Evil <x@allegro.pl.evil.example>") is None
    assert mb.platform_of("Allegro <a@mail.allegro.pl>") == "allegro"
    msg = email.message_from_bytes(eml("olx.eml").replace(b"RTX 4090 24GB Founders Edition", b"RTX 4090 tel 600 123 456"),
                                   policy=policy.default)
    l = [x for x in mb.parse_message(msg, "olx") if x.source_id == "Xyz12"][0]
    assert "600" not in l.title and l.contact_in_text


# ------------------------------------------------------------------ poll
def test_poll_marks_seen_saves_eml_and_skips_unknown(tmp_path):
    mails = {b"1": eml("allegro.eml"), b"2": eml("olx.eml"),
             b"3": b"From: Bob <bob@example.invalid>\nSubject: hi\n\nhello\n"}
    box, imap = make_box(tmp_path, mails)
    ls = run(box.poll())
    assert len(ls) == 4 and {l.source for l in ls} == {"allegro", "olx"}
    assert sorted(imap.seen) == [b"1", b"2"], "unknown sender stays unseen"
    for l in ls:
        assert l.raw_path and l.raw_path.endswith(".eml") and (tmp_path / "snap" / "mail").exists()
        assert open(l.raw_path, "rb").read() in (mails[b"1"], mails[b"2"])


def test_allegro_and_olx_are_never_fetched(tmp_path):
    box, _ = make_box(tmp_path, {b"1": eml("allegro.eml"), b"2": eml("olx.eml"), b"3": eml("ebay.eml")})
    f = FakeFetcher(default=Res(200, "<html></html>"))
    ls = run(box.poll(f))
    assert len(ls) == 6 and f.urls == []


def test_kleinanzeigen_enriched_when_fetchable_and_survives_failure(tmp_path):
    box, _ = make_box(tmp_path, {b"1": eml("kleinanzeigen.eml")})
    f = FakeFetcher({"2876543210": Res(200, fixture_text("kleinanzeigen", "detail.html"),
                                       "https://www.kleinanzeigen.de/s-anzeige/x/2876543210-225-1234")},
                    default=Res(403, "", blocked=True))
    ls = run(box.poll(f))
    assert len(f.urls) == 2 and len(ls) == 2, "a failed detail keeps the e-mail listing"
    assert all(l.raw_path for l in ls)


def test_disabled_without_credentials_one_health_event(tmp_path, store):
    box, imap = make_box(tmp_path, {b"1": eml("allegro.eml")}, store=store, creds=None)
    assert not box.enabled
    assert run(box.poll()) == [] and run(box.poll()) == []
    assert imap.ops == []
    rows = [tuple(r) for r in store.conn.execute("select source, kind from health")]
    assert rows == [("mailbox", "no_credentials")]


def test_malformed_credentials_disabled(tmp_path):
    box, _ = make_box(tmp_path, {}, creds="not json")
    assert not box.enabled


def test_imap_error_is_adapter_error(tmp_path):
    import imaplib
    box = mb.Mailbox(secret_fn=lambda r: CREDS, imap_factory=lambda h, p: (_ for _ in ()).throw(imaplib.IMAP4.error("x")))
    with pytest.raises(AdapterError):
        run(box.poll())


def test_search_applies_price_bounds_and_alive_unsupported(tmp_path):
    box, _ = make_box(tmp_path, {b"1": eml("allegro.eml")})
    ls = run(box.search(Query(text="ignored", max_price_pln=Decimal("7000"))))
    assert [l.source_id for l in ls] == ["98765432100"]
    with pytest.raises(AdapterError):
        run(box.alive(ls[0], FakeFetcher()))


def test_registry_has_mailbox_disabled_by_default():
    assert "mailbox" not in registry()
