import json
from datetime import timedelta
from decimal import Decimal

import pytest
from starlette.testclient import TestClient

from dealfix import NOW, make_deal
from dh import config, publish
from dh.store import Store
from dh.web.app import create_app


def build(tmp_path):
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / "publish.yaml").write_text("cards_dir: var/cards/deals\n")
    cfg = config.load(tmp_path)
    db = tmp_path / "t.db"
    st = Store(db)
    return st, TestClient(create_app(db, cfg, tmp_path)), cfg


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
