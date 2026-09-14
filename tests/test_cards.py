import json

import yaml

from dealfix import estimate, listing, make_deal, risk
from dh import cards, publish
from dh.store import Store


def test_card_contains_required_sections(tmp_path):
    st = Store(tmp_path / "t.db")
    d, res, lst = make_deal(st)
    md, data = cards.render(d, lst, estimate(), res, risk())
    fm = yaml.safe_load(md.split("---\n")[1])
    assert fm["deal_id"] == d.id and fm["outcome"] == {"status": "open", "price_pln": None, "date": None, "note": ""}
    for h in ("## Headline", "## Net calculation", "## Evidence", "### Comparables", "## Risk", "## Why this tier",
              "## How to buy safely"):
        assert h in md
    for needle in ("Buy: 2000", "Exit P25: 2600.00", "median: 2800.00", f"Conservative net: {d.cons_net_pln}",
                   f"Expected net: {d.exp_net_pln}", "Confidence: medium", "capital: 553.00 USD", "days to sell: 6.0",
                   "FX: 1 PLN per PLN (NBP mid, 2026-10-29)", "Retail ceiling: 3100.00", "https://allegro.pl/c/1",
                   "https://www.kleinanzeigen.de/s-anzeige/x/1", "- [ ] Pay only through", "clears the easy floor"):
        assert needle in md, needle
    for line in res.lines_cons + res.lines_exp:
        assert f"| {line.label} | {line.pln:.2f} |" in md
    assert len([c for c in data["comps"] if c["url"].startswith("https://")]) >= 3
    assert data["outcome"]["status"] == "open"


def test_card_json_only_public_keys_and_no_seller_text(tmp_path):
    st = Store(tmp_path / "t.db")
    d, res, lst = make_deal(st)
    md, data = cards.render(d, lst, estimate(), res, risk())
    assert set(data) <= cards.PUBLIC_KEYS
    assert all(set(c) <= cards.PUBLIC_COMP_KEYS for c in data["comps"])
    assert set(data["listing"]) <= cards.PUBLIC_LISTING_KEYS
    blob = json.dumps(data) + md
    assert "s1" not in data["listing"].values() and lst.seller_hash not in blob
    assert "seller_score" not in blob and data["risk"] == {"p_scam": 0.02, "flags": ["new_account"]}


def test_every_emitted_key_survives_publish_sanitiser(tmp_path):
    st = Store(tmp_path / "t.db")
    d, res, lst = make_deal(st)
    md, data = cards.render(d, lst, estimate(), res, risk())
    out = json.loads(publish.sanitize_json(json.dumps(data)))
    assert set(out) == set(data)
    assert out["comps"] == data["comps"] and out["listing"] == data["listing"]
    # the md survives too: nothing is dropped as a "seller" line, including the checklist item
    assert publish.sanitize_md(md) == md
    assert publish.CARD_KEYS is cards.PUBLIC_KEYS


def test_write_path_roundtrip_and_outcome_kept(tmp_path):
    st = Store(tmp_path / "t.db")
    d, res, lst = make_deal(st)
    p = cards.write(d, tmp_path, cards.render(d, lst, estimate(), res, risk()))
    assert p == tmp_path / "deals/2026-10-29" / f"rtx-4090-{d.id}.md"
    assert p.with_suffix(".json").is_file()
    assert publish.parse_outcome(p.read_text()) == publish.DEFAULT_OUTCOME
    # user marks it sold; a rewrite (e.g. after verify) must keep that
    p.write_text(publish._with_outcome(p.read_text(), {"status": "sold", "price_pln": "2900", "date": "2026-11-01", "note": "ok"}))
    cards.write(d, tmp_path)
    assert publish.parse_outcome(p.read_text())["status"] == "sold"
    assert json.loads(p.with_suffix(".json").read_text())["outcome"]["status"] == "sold"
    # id parses via publish's own logic
    assert publish._deal_id(p.name, p.read_text()) == d.id


def test_from_deal_matches_render(tmp_path):
    st = Store(tmp_path / "t.db")
    d, res, lst = make_deal(st)
    assert cards.from_deal(d)[1] == cards.render(d, lst, estimate(), res, risk())[1]


def test_slug():
    assert cards.slug("Karta Graśczna RTX 4090 / Ti!") == "karta-grasczna-rtx-4090-ti"
    assert cards.slug("\u0141\u00f3d\u017a G\u00f6rlitz") == "lodz-gorlitz"
    assert cards.slug(None) == "deal"
