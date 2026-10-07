import json
from datetime import timedelta
from decimal import Decimal

import pytest
from starlette.testclient import TestClient

import asyncio
from dataclasses import replace

from dealfix import NOW, make_deal, net_inputs
from test_economics import FakeCustoms, FakeFx, load_cfg_data
from dh import cards, config, economics, publish
from dh.store import Store
from dh.web.app import create_app


def build(tmp_path, hosts=("testserver",)):
    data = load_cfg_data()
    data["publish"] = {"cards_dir": "var/cards/deals"}
    data["web"] = {"allowed_hosts": list(hosts)}
    cfg = config.Config(data)
    db = tmp_path / "t.db"
    st = Store(db)
    app = create_app(db, cfg, tmp_path, fx_factory=lambda s_: FakeFx(), customs_factory=lambda s_: FakeCustoms())
    return st, TestClient(app, raise_server_exceptions=False), cfg


def post(c, deal_id, **data):
    return c.post(f"/deal/{deal_id}/mark", data=data, headers={"Origin": "http://testserver"},
                  follow_redirects=False)


def test_mark_post_creates_mark(tmp_path):
    st, c, _ = build(tmp_path)
    d, _, _ = make_deal(st)
    r = post(c, d.id, status="sold", price_pln="2900.50", note="ok")
    assert r.status_code == 303
    m = st.conn.execute("select * from marks where deal_id=?", (d.id,)).fetchall()
    assert len(m) == 1 and m[0]["status"] == "sold" and Decimal(m[0]["price_pln"]) == Decimal("2900.50")
    assert m[0]["source"] == "web"
    # card file outcome block updated, so the publisher pushes it
    md = next((tmp_path / "var/cards/deals/2026-10-29").glob("*.md"))
    o = publish.parse_outcome(md.read_text())
    assert o["status"] == "sold" and o["price_pln"] == "2900.50"
    assert json.loads(md.with_suffix(".json").read_text())["outcome"]["status"] == "sold"
    # not re-imported as a duplicate "github" mark
    assert publish.import_texts(st, [(md.as_posix(), md.read_text())])[0] == 0
    assert "sold" in c.get(f"/deal/{d.id}").text


def test_mark_validation_and_csrf(tmp_path):
    st, c, _ = build(tmp_path)
    d, _, _ = make_deal(st)
    assert post(c, d.id, status="sold").status_code == 400
    assert post(c, d.id, status="sold", price_pln="-5").status_code == 400
    assert post(c, d.id, status="sold", price_pln="NaN").status_code == 400
    assert post(c, d.id, status="bogus").status_code == 400
    assert post(c, 999, status="bought").status_code == 404
    r = c.post(f"/deal/{d.id}/mark", data={"status": "bought"}, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    assert c.post(f"/deal/{d.id}/mark", data={"status": "bought"}).status_code == 403
    assert st.conn.execute("select count(*) from marks").fetchone()[0] == 0
    assert post(c, d.id, status="skipped", reason="seller vanished").status_code == 303
    assert post(c, d.id, status="bought").status_code == 303


def test_deals_sorted_star_tier_net(tmp_path):
    st, c, _ = build(tmp_path)
    ids = {}
    for name, tier, star, net in (("a", 2, False, "90"), ("b", 1, False, "400"), ("c", 2, False, "300"),
                                  ("d", 3, True, "10"), ("e", 1, True, "800")):
        d, _, _ = make_deal(st, sid=name, seller=name, key=f"KEY-{name}")
        st.conn.execute("update deals set tier=?, star=?, cons_net_usd=? where id=?", (tier, int(star), net, d.id))
        ids[name] = d.id
    st.conn.commit()
    html = c.get("/").text
    pos = [html.index(f"/deal/{ids[n]}") for n in "edbca"]
    assert pos == sorted(pos)


def test_filters_and_bad_values(tmp_path):
    st, c, _ = build(tmp_path)
    d1, _, _ = make_deal(st, sid="1", seller="1", key="KEY-ONE")
    d2, _, _ = make_deal(st, sid="2", seller="2", key="KEY-TWO", source="olx", created=NOW - timedelta(days=10))
    st.conn.execute("update deals set tier=1 where id=?", (d1.id,))
    st.conn.execute("update listings set country='DE' where id=?", (d2.listing_ref,))
    st.conn.execute("insert into extractions (listing_id, key, category) values (?,?,?)", (d1.listing_ref, "k", "gpu"))
    st.conn.commit()
    both = c.get("/").text
    assert "KEY-ONE" in both and "KEY-TWO" in both
    assert "KEY-TWO" not in c.get("/?tier=1").text
    assert "KEY-ONE" not in c.get("/?country=DE").text
    assert "KEY-ONE" not in c.get("/?source=olx").text
    assert "KEY-TWO" not in c.get("/?category=gpu").text
    assert "KEY-TWO" not in c.get("/?since=2026-10-25").text
    assert "KEY-ONE" in c.get("/?bogus=1&tier=").text
    for q in ("tier=9", "tier=x", "since=yesterday"):
        assert c.get("/?" + q).status_code == 400


def test_untrusted_title_escaped(tmp_path):
    st, c, _ = build(tmp_path)
    d, _, _ = make_deal(st)
    st.conn.execute("update listings set title=? where id=?", ("<script>alert(1)</script>", d.listing_ref))
    ev = json.loads(st.conn.execute("select evidence_json from deals").fetchone()[0])
    ev["listing"]["title"] = "<script>alert(1)</script>"
    st.conn.execute("update deals set evidence_json=?", (json.dumps(ev),))
    st.conn.commit()
    for url in ("/", f"/deal/{d.id}"):
        t = c.get(url).text
        assert "<script>alert" not in t and "&lt;script&gt;" in t


def test_deal_page_shows_card_data(tmp_path):
    st, c, _ = build(tmp_path)
    d, res, _ = make_deal(st)
    t = c.get(f"/deal/{d.id}").text
    for needle in ("RTX 4090", "553.00", "clears the easy floor", "https://allegro.pl/c/1", "Bought", "Skipped", "Sold"):
        assert needle in t, needle
    assert c.get("/deal/999").status_code == 404


@pytest.mark.parametrize("path", ["/rounds", "/playbook", "/books", "/health"])
def test_empty_pages_do_not_error(tmp_path, path):
    st, c, _ = build(tmp_path)
    assert c.get("/").status_code == 200
    r = c.get(path)
    assert r.status_code == 200 and "no data yet" in r.text.lower()


def test_pages_with_data(tmp_path):
    st, c, _ = build(tmp_path)
    d, _, _ = make_deal(st)
    rid = st.log_round(NOW, NOW + timedelta(minutes=5), "olx", 10, 1, "n")
    st.add_cost(rid, "energy", Decimal("1.50"), 3.0)
    st.add_health("olx", "http_403", "blocked <b>x</b>")
    st.conn.execute("insert into playbook_versions (version, content) values ('v1', 'a\nb\n'), ('v2', 'a\nc\n')")
    st.conn.commit()
    assert "olx" in c.get("/rounds").text
    b = c.get("/books").text
    assert "1.50" in b
    h = c.get("/health").text
    assert "http_403" in h and "<b>x</b>" not in h
    p = c.get("/playbook").text
    assert "v2" in p and "-b" in p and "+c" in p


def test_books_realised_net_from_sold_deals(tmp_path):
    st, c, cfg = build(tmp_path)
    d, res, _ = make_deal(st)
    d2, _, _ = make_deal(st, sid="2", seller="s2", key="OPEN-ONE")
    assert post(c, d.id, status="sold", price_pln="3000").status_code == 303
    inp = replace(net_inputs(), exit_price_cons_pln=Decimal("3000"), exit_price_exp_pln=Decimal("3000"))
    want = asyncio.run(economics.net(inp, cfg, FakeFx(), FakeCustoms())).cons_net_pln
    assert want != res.cons_net_pln
    t = c.get("/books").text
    assert f"{want:.2f} PLN" in t and "Realised net" in t and "Forecast: expected net" in t
    assert f"<td class=\"n\">{want:.2f}</td>" in t and "OPEN-ONE" not in t
    usd = FakeFx()
    assert f"{asyncio.run(usd.pln_to_usd(want, NOW.date())):.2f} USD" in t


def test_books_realised_row_survives_bad_evidence(tmp_path):
    st, c, _ = build(tmp_path)
    d, _, _ = make_deal(st)
    post(c, d.id, status="sold", price_pln="3000")
    st.conn.execute("update deals set evidence_json='{}'")
    st.conn.commit()
    r = c.get("/books")
    assert r.status_code == 200 and "not computable" in r.text


def test_card_write_failure_keeps_mark(tmp_path, monkeypatch):
    st, c, _ = build(tmp_path)
    d, _, _ = make_deal(st)

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(cards, "set_outcome", boom)
    assert post(c, d.id, status="sold", price_pln="3000").status_code == 303
    assert st.conn.execute("select count(*) from marks").fetchone()[0] == 1
    row = st.conn.execute("select outcome from mark_imports where deal_id=?", (d.id,)).fetchone()
    assert json.loads(row["outcome"])["status"] == "sold"  # the publisher writes this into the log copy
    assert st.conn.execute("select count(*) from health where kind='card_write_failed'").fetchone()[0] == 1


def test_add_mark_failure_changes_nothing(tmp_path, monkeypatch):
    st, c, _ = build(tmp_path)
    d, _, _ = make_deal(st)
    cards.write(d, tmp_path / "var/cards")
    md = next((tmp_path / "var/cards/deals").glob("*/*.md"))
    before = md.read_text()

    def boom(self, *a, **k):
        raise RuntimeError("db locked")
    monkeypatch.setattr(Store, "add_mark", boom)
    assert post(c, d.id, status="bought").status_code == 500
    assert md.read_text() == before
    assert st.conn.execute("select count(*) from mark_imports").fetchone()[0] == 0
    assert st.conn.execute("select count(*) from marks").fetchone()[0] == 0


def test_set_outcome_is_atomic_and_public(tmp_path):
    st = Store(tmp_path / "t.db")
    d, res, lst = make_deal(st)
    p = cards.write(d, tmp_path)
    o = {"status": "bought", "price_pln": None, "date": "2026-10-29", "note": "n"}
    cards.set_outcome(p, o)
    assert publish.parse_outcome(p.read_text())["status"] == "bought"
    assert json.loads(p.with_suffix(".json").read_text())["outcome"] == o
    assert not list(tmp_path.glob("deals/*/*.tmp"))
    cards.write(d, tmp_path)  # rewrite keeps it
    assert publish.parse_outcome(p.read_text())["status"] == "bought"


@pytest.mark.parametrize("price", ["1e10", "1E3", "1234567890", "12.345", "0", "0.00", "-5", "NaN", "Infinity",
                                   "", "abc", "1 000", "0x10", ".5"])
def test_bad_sold_prices_rejected(tmp_path, price):
    st, c, _ = build(tmp_path)
    d, _, _ = make_deal(st)
    r = post(c, d.id, status="sold", price_pln=price)
    assert r.status_code == 400 and "Not saved" in r.text
    assert st.conn.execute("select count(*) from marks").fetchone()[0] == 0


def test_good_prices_and_bought_with_price(tmp_path):
    st, c, _ = build(tmp_path)
    d, _, _ = make_deal(st)
    assert post(c, d.id, status="sold", price_pln="2 900".replace(" ", "")).status_code == 303
    assert post(c, d.id, status="sold", price_pln="2900,5").status_code == 303
    assert post(c, d.id, status="bought", price_pln="1999.99").status_code == 303
    assert post(c, d.id, status="bought", price_pln="1e9").status_code == 400
    prices = [r[0] for r in st.conn.execute("select price_pln from marks order by id")]
    assert prices == ["2900", "2900.5", "1999.99"]


def test_validation_error_rerenders_card(tmp_path):
    st, c, _ = build(tmp_path)
    d, _, _ = make_deal(st)
    r = post(c, d.id, status="sold", price_pln="x")
    assert r.status_code == 400 and "Conservative net" in r.text and "price must" in r.text


def test_referer_fallback_null_origin_and_host_allowlist(tmp_path):
    st, c, _ = build(tmp_path)
    d, _, _ = make_deal(st)
    url = f"/deal/{d.id}/mark"
    ok = c.post(url, data={"status": "bought"}, headers={"Referer": "http://testserver/deal/1"}, follow_redirects=False)
    assert ok.status_code == 303
    assert c.post(url, data={"status": "bought"}, headers={"Referer": "http://evil/x"}).status_code == 403
    assert c.post(url, data={"status": "bought"}, headers={"Origin": "null"}).status_code == 403
    assert c.post(url, data={"status": "bought"}, headers={"Origin": ""}).status_code == 403
    # same-origin but Host not on the allowlist (DNS rebinding style)
    st2, c2, _ = build(tmp_path, hosts=("rig.lan", "192.168.1.5"))
    r = c2.post(url, data={"status": "bought"}, headers={"Origin": "http://testserver"})
    assert r.status_code == 403
    r = c2.post(url, data={"status": "bought"}, headers={"Origin": "http://rig.lan:8711", "Host": "rig.lan:8711"},
                follow_redirects=False)
    assert r.status_code == 303
    assert st.conn.execute("select count(*) from marks").fetchone()[0] == 2


def test_default_allowed_hosts_include_localhost():
    from dh.web.app import default_allowed_hosts
    h = default_allowed_hosts()
    assert "localhost" in h and "127.0.0.1" in h


def test_pagination(tmp_path):
    st, c, _ = build(tmp_path)
    d, _, _ = make_deal(st)
    st.conn.executemany(
        "insert into deals select null, listing_ref, key || ?, tier, star, effort, confidence, cons_net_pln, exp_net_pln,"
        " cons_net_usd, exp_net_usd, capital_usd, breakdown_json, evidence_json, reasons, created_at, verified_at, status"
        " from deals where id=?", [(f"-{i:03d}", d.id) for i in range(104)])
    st.conn.commit()
    p1, p2 = c.get("/").text, c.get("/?page=2").text
    assert p1.count('href="/deal/') == 100 and "page 1 of 2" in p1 and "next" in p1
    assert p2.count('href="/deal/') == 5 and "page 2 of 2" in p2 and "prev" in p2
    assert c.get("/?page=3").status_code == 200
    for q in ("page=0", "page=x", "page=-1"):
        assert c.get("/?" + q).status_code == 400


def test_books_realised_net_margin_scheme_hand_computed(tmp_path):
    """Private PL seller, no VAT invoice, PLN, allegro exit: margin scheme. Sold at 3000, hand computation:
    fees 8% x 3000 + 1 = 241.00; VAT due 23/123 x (3000 - 2000) = 186.99; input VAT on services
    23/123 x (241 + 25) = 45.07 + 4.67 = 49.74; returns reserve 5% x 3000 = 150; risk haircut 0.02 x 2000 = 40;
    pre-tax = 3000 - 241 - 2000 - 20 - 186.99 + 49.74 - 25 - 150 - 40 = 386.75; tax 23.9% = 92.43;
    realised net = 294.32 PLN."""
    st, c, _ = build(tmp_path)
    d, _, lst = make_deal(st)
    assert lst.is_business is False and not json.loads(d.evidence_json)["net_inputs"]["has_vat_invoice"]
    post(c, d.id, status="sold", price_pln="3000")
    t = c.get("/books").text
    assert 'class="n">294.32</td>' in t and "294.32 PLN" in t


def test_existing_card_only_touched_via_set_outcome(tmp_path, monkeypatch):
    st, c, _ = build(tmp_path)
    d, _, _ = make_deal(st)
    cards.write(d, tmp_path / "var/cards")
    calls = []
    monkeypatch.setattr(cards, "write", lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(AssertionError))
    assert post(c, d.id, status="bought").status_code == 303 and not calls


def test_pagination_links_urlencoded(tmp_path):
    st, c, _ = build(tmp_path)
    d, _, _ = make_deal(st)
    st.conn.executemany(
        "insert into deals select null, listing_ref, key, tier, star, effort, confidence, cons_net_pln, exp_net_pln,"
        " cons_net_usd, exp_net_usd, capital_usd, breakdown_json, evidence_json, reasons, created_at, verified_at, status"
        " from deals where id=?", [(d.id,)] * 101)
    st.conn.commit()
    t = c.get("/?source=kleinanzeigen&since=2026-01-01").text
    assert 'href="/?page=2&amp;source=kleinanzeigen&amp;since=2026-01-01"' in t
