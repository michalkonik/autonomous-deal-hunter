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
    assert out == data and set(data) == cards.PUBLIC_KEYS
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


def test_seller_text_is_escaped_in_markdown(tmp_path):
    st = Store(tmp_path / "t.db")
    d, res, lst = make_deal(st)
    lst.title = "RTX [click](https://evil.example) | x\n# Injected\nseller: Jan"
    md, data = cards.render(d, lst, estimate(), res, risk())
    line = [l for l in md.split("\n") if l.startswith("- Listing:")]
    assert len(line) == 1 and "\\[click\\]" in line[0] and "\\|" in line[0]
    assert "\n# Injected" not in md and "[click](https" not in md


def test_screenshot_paths_relative_to_root(tmp_path):
    import json as _j
    from dh import verify
    st = Store(tmp_path / "t.db")
    d, res, lst = make_deal(st)
    verify.record_verification(st, d.id, True, tmp_path / "shots" / "v.png")
    verify.record_verification(st, d.id, True, "/etc/elsewhere/leak.png")  # outside root: basename only
    d = st.get_deal(d.id)
    ev = _j.loads(d.evidence_json)
    ev["screenshots"]["discovery"] = str(tmp_path / "shots" / "d.png")
    d.evidence_json = _j.dumps(ev)
    _, data = cards.from_deal(d, tmp_path)
    assert data["screenshots"] == {"discovery": "shots/d.png", "verification": "leak.png"}
    md, data2 = cards.render(d, lst, estimate(), res, risk(), tmp_path)
    assert str(tmp_path) not in md and data2["screenshots"]["discovery"] == "shots/d.png"
    p = cards.write(d, tmp_path)
    assert str(tmp_path / "shots") not in p.read_text()


def test_write_is_atomic_no_temp_left(tmp_path):
    st = Store(tmp_path / "t.db")
    d, res, lst = make_deal(st)
    cards.write(d, tmp_path)
    cards.write(d, tmp_path)
    assert not list(tmp_path.glob("deals/*/*.tmp"))


def test_link_escapes_once_and_keeps_parentheses_inside_the_url():
    """Deferred minors: the label was escaped twice on the no-URL fallback, and a ')' in a URL ended the link."""
    label = cards._esc("RTX [4090] | OC")
    assert cards._link(label, None) == "RTX \\[4090\\] \\| OC"  # escaped once, not "\\\\["
    assert cards._link(label, "ftp://x") == "ftp://x"  # not a web URL: shown as escaped text, no link
    u = "https://www.kleinanzeigen.de/s-anzeige/rtx-4090-(oc)/123?x=a b"
    out = cards._link("listing", u)
    assert out == "[listing](https://www.kleinanzeigen.de/s-anzeige/rtx-4090-%28oc%29/123?x=a%20b)"
    assert out.count("(") == 1 and out.count(")") == 1  # Markdown sees one link, not a truncated one


def test_card_listing_line_with_parenthesised_url(tmp_path):
    st = Store(tmp_path / "t.db")
    d, res, lst = make_deal(st)
    lst.url = "https://allegrolokalnie.pl/oferta/karta-rtx-3080-(10gb)-123"
    md, _ = cards.render(d, lst, estimate(), res, risk())
    line = next(l for l in md.split("\n") if l.startswith("- Listing:"))
    assert line.endswith("(https://allegrolokalnie.pl/oferta/karta-rtx-3080-%2810gb%29-123)"), line


def test_comparables_legend_explains_discounted_asks(tmp_path):
    """Validation: an owner reading a card saw "active_ask 3219.99 PLN" and a linked page asking 3499.99 PLN.
    A card with asks among its comparables now says they are shown at x ask_to_sale_ratio."""
    import dataclasses
    from datetime import datetime, timezone
    from decimal import Decimal

    from dh.models import CompRef
    st = Store(tmp_path / "t.db")
    d, res, lst = make_deal(st)
    md, _ = cards.render(d, lst, estimate(), res, risk())
    assert "asking price" not in md  # sales only: no legend
    e = estimate()
    e = dataclasses.replace(e, comps=[*e.comps, CompRef("active_ask", "allegro", Decimal("3219.99"),
                                                         datetime(2026, 10, 29, tzinfo=timezone.utc), 0.3,
                                                         "https://allegro.pl/a/1")])
    md, _ = cards.render(d, lst, e, res, risk())
    assert "An active ask is shown at x0.92 of its asking price" in md


def test_p_scam_is_shown_with_three_decimals(tmp_path):
    """A live card showed "p_scam: 0.043107254941086116"."""
    import dataclasses
    st = Store(tmp_path / "t.db")
    d, res, lst = make_deal(st)
    md, data = cards.render(d, lst, estimate(), res, dataclasses.replace(risk(), p_scam=0.043107254941086116))
    assert "- p_scam: 0.043\n" in md
