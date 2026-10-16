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
    """UID-based fake: `seen` collects the UIDs flagged \\Seen (by STORE)."""

    def __init__(self, mails: dict[bytes, bytes]):
        self.mails, self.seen, self.ops = mails, [], []

    def login(self, u, p):
        self.ops.append(("login", u))
        return "OK", []

    def select(self, folder):
        self.ops.append(("select", folder))
        return "OK", [b"1"]

    def uid(self, cmd, *a):
        if cmd == "SEARCH":
            assert a == (None, "UNSEEN")
            return "OK", [b" ".join(sorted(u for u in self.mails if u not in self.seen))]
        if cmd == "FETCH":
            assert "PEEK" in a[1]  # fetching must not set \\Seen by itself
            return "OK", [(b"1 (BODY[] {1}", self.mails[a[0]]), b")"]
        if cmd == "STORE":
            assert a[1:] == ("+FLAGS", "\\Seen")
            self.seen.append(a[0])
            return "OK", []
        raise AssertionError(cmd)

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
    assert imap.seen == [], "poll alone flags nothing: delivery is acknowledged by commit()"
    assert run(box.commit()) == 2
    assert sorted(imap.seen) == [b"1", b"2"], "unknown sender stays unseen"
    assert run(box.commit()) == 0
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


# ------------------------------------------------------------------ review round 1
def test_spoofed_from_and_missing_auth_are_skipped_with_health(tmp_path, store):
    good = eml("olx.eml")
    no_auth = b"\n".join(l for l in good.split(b"\n") if not l.startswith(b"Authentication-Results"))
    spoof = good.replace(b"dkim=pass header.d=olx.pl", b"dkim=pass header.d=olx.pl.evil.example") \
                .replace(b"smtp.mailfrom=bounce@olx.pl", b"smtp.mailfrom=bounce@evil.example")
    dkim_fail = good.replace(b"dkim=pass", b"dkim=fail").replace(b"spf=pass", b"spf=none")
    other_dom = good.replace(b"header.d=olx.pl", b"header.d=allegro.pl").replace(b"smtp.mailfrom=bounce@olx.pl", b"smtp.mailfrom=bounce@allegro.pl")
    mails = {b"1": no_auth, b"2": spoof, b"3": dkim_fail, b"4": other_dom}
    mails = {u: m.replace(b"olx-1@olx.pl", b"olx-%s@olx.pl" % u) for u, m in mails.items()}
    box, imap = make_box(tmp_path, mails, store=store)
    assert run(box.poll()) == [] and run(box.commit()) == 0
    kinds = [r[0] for r in store.conn.execute("select kind from health")]
    assert kinds.count("unauthenticated_alert") == 4 and imap.seen == []
    run(box.poll())  # the same mails again: no health spam
    assert [r[0] for r in store.conn.execute("select kind from health")].count("unauthenticated_alert") == 4


def test_spf_pass_alone_with_matching_envelope_is_accepted(tmp_path):
    raw = eml("olx.eml").replace(b"dkim=pass header.d=olx.pl header.s=sel; ", b"")
    box, _ = make_box(tmp_path, {b"1": raw})
    assert len(run(box.poll())) == 2


def test_only_topmost_auth_header_counts(tmp_path):
    good = eml("olx.eml")
    forged = b"Authentication-Results: x; dkim=pass header.d=olx.pl\n"
    bad_top = good.replace(b"dkim=pass", b"dkim=fail").replace(b"spf=pass", b"spf=fail")
    box, _ = make_box(tmp_path, {b"1": bad_top + b""})
    assert run(box.poll()) == []
    # a sender-forged header BELOW the receiving server's failing one does not rescue the mail
    raw = bad_top.replace(b"From:", forged + b"From:", 1)
    box2, _ = make_box(tmp_path, {b"1": raw})
    assert run(box2.poll()) == []


def test_photos_only_from_platform_image_hosts(tmp_path):
    raw = eml("allegro.eml")
    ok = run(make_box(tmp_path, {b"1": raw})[0].poll())
    assert [l.photos for l in ok if l.source_id == "12345678901"] == [["https://a.allegroimg.com/s180/aa.jpg"]]
    evil = raw.replace(b"https://a.allegroimg.com/s180/aa.jpg", b"https://tracker.evil.example/p.gif?u=1")
    assert [l.photos for l in run(make_box(tmp_path, {b"1": evil})[0].poll())] == [[], []]
    http = raw.replace(b"https://a.allegroimg.com", b"http://a.allegroimg.com")
    assert all(l.photos == [] for l in run(make_box(tmp_path, {b"1": http})[0].poll()))
    lookalike = raw.replace(b"https://a.allegroimg.com", b"https://allegroimg.com.evil.example")
    assert all(l.photos == [] for l in run(make_box(tmp_path, {b"1": lookalike})[0].poll()))


def test_parse_empty_mail_is_reported_and_left_unseen(tmp_path, store):
    empty = eml("olx.eml").split(b"<html>")[0] + b"<html><body><p>Willkommen! Bestaetigen Sie Ihre Suche.</p></body></html>\n"
    box, imap = make_box(tmp_path, {b"1": empty, b"2": eml("allegro.eml")}, store=store)
    ls = run(box.poll())
    assert len(ls) == 2 and run(box.commit()) == 1 and imap.seen == [b"2"]
    assert [r[0] for r in store.conn.execute("select kind from health")] == ["alert_parse_empty"]
    run(box.poll())
    assert [r[0] for r in store.conn.execute("select kind from health")] == ["alert_parse_empty"]  # reported once


def test_crash_before_commit_redelivers(tmp_path):
    box, imap = make_box(tmp_path, {b"1": eml("allegro.eml")})
    assert len(run(box.poll())) == 2
    box2 = mb.Mailbox(secret_fn=lambda r: CREDS, imap_factory=lambda h, p: imap)  # "restart": nothing was committed
    assert len(run(box2.poll())) == 2
    run(box2.commit())
    assert run(box2.poll()) == []


def test_eml_purge_after_90_days(tmp_path):
    import os
    box, _ = make_box(tmp_path, {b"1": eml("allegro.eml"), b"2": eml("olx.eml")})
    run(box.poll())
    files = sorted((tmp_path / "snap" / "mail").glob("*.eml"))
    assert len(files) == 2
    old = files[0].stat().st_mtime - 91 * 86400
    os.utime(files[0], (old, old))
    assert box.purge(90) == 1 and len(list((tmp_path / "snap" / "mail").glob("*.eml"))) == 1
    assert box.purge(90) == 0


def test_registry_image_hosts_override():
    from dh.config import Config
    reg = registry(Config({"sources": {"adapters": {"mailbox": {"enabled": True, "image_hosts": {"olx": ["cdn.example"]}}}}}))
    assert reg["mailbox"].image_hosts == {"olx": ("cdn.example",)}


# ------------------------------------------------------------------ review round 2: per-clause Authentication-Results
def _auth(clauses: str):
    msg = email.message_from_bytes(b"From: Allegro <a@allegro.pl>\nAuthentication-Results: mx.example.invalid; " +
                                   clauses.encode() + b"\n\nx", policy=policy.default)
    return mb.authenticated(msg, "allegro")


@pytest.mark.parametrize("clauses", [
    "dkim=pass header.d=allegro.pl header.s=s1",
    "dkim=pass header.d=mail.allegro.pl",
    "spf=pass smtp.mailfrom=bounce@allegro.pl",
    "spf=pass smtp.mailfrom=bounce@mail.allegro.pl smtp.helo=mx.other.example",
    "dkim=fail header.d=allegro.pl; dkim=pass header.d=allegro.pl",
    "dmarc=pass header.from=allegro.pl",
])
def test_auth_valid_cases(clauses):
    assert _auth(clauses) is True


@pytest.mark.parametrize("clauses", [
    "dkim=pass header.d=evil.com header.i=@allegro.pl",       # header.i names the platform, the signing domain does not
    "spf=pass smtp.helo=allegro.pl",                          # HELO-only SPF proves nothing
    "spf=pass smtp.mailfrom=bounce@evil.com smtp.helo=allegro.pl",
    "dkim=pass header.d=allegro.pl.evil.example",
    "dkim=pass header.d=notallegro.pl",
    "dkim=fail header.d=allegro.pl; spf=softfail smtp.mailfrom=a@allegro.pl",
    "dkim=pass header.d=evil.com; dmarc=fail header.from=allegro.pl",
    "dmarc=pass header.from=evil.com",
    "dkim=pass header.b=allegro.pl",
    "",
])
def test_auth_rejected_cases(clauses):
    assert _auth(clauses) is False
