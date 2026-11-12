import asyncio
import json
import subprocess
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from dh import config, publish
from dh.models import Deal
from dh.store import Store

NOW = datetime(2026, 10, 29, 12, tzinfo=timezone.utc)


def git(cwd, *a):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout


def make_remote(tmp_path):
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "log", str(bare)], check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "-q", str(bare), str(seed)], check=True, capture_output=True)
    git(seed, "checkout", "-q", "-b", "log")
    (seed / "README.md").write_text("log\n")
    git(seed, "add", "-A"); git(seed, "commit", "-qm", "init"); git(seed, "push", "-q", "origin", "log")
    return bare


def make_env(tmp_path):
    bare = make_remote(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "publish.yaml").write_text(
        f"interval_min: 30\nremote: {bare}\nbranch: log\nlog_dir: {tmp_path / 'clone'}\ncards_dir: var/cards/deals\n")
    cfg = config.load(tmp_path)
    st = Store(tmp_path / "t.db")
    return bare, cfg, st


def add_deal(st, tmp_path, tier=2, star=False, net="100", key="RTX 4090"):
    d = Deal(id=None, listing_ref=1, key=key, tier=tier, star=star, effort="easy", confidence="high",
             cons_net_pln=Decimal("1"), exp_net_pln=Decimal("1"), cons_net_usd=Decimal(net),
             exp_net_usd=Decimal(net), capital_usd=Decimal("1"), breakdown_json="{}", evidence_json="{}",
             reasons=[], created_at=NOW)
    did = st.save_deal(d)
    cdir = tmp_path / "var/cards/deals/2026-10-29"
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / f"deal-{did}.md").write_text(
        f"---\ndeal_id: {did}\noutcome: {{status: open, price_pln: null, date: null, note: ''}}\n---\n"
        f"# {key}\n[listing](https://olx.pl/x{did})\n")
    (cdir / f"deal-{did}.json").write_text(json.dumps({"deal_id": did}))
    return did


def clone2(tmp_path, bare):
    c = tmp_path / "phone"
    subprocess.run(["git", "clone", "-q", "-b", "log", str(bare), str(c)], check=True, capture_output=True)
    return c


def test_outcome_edit_becomes_mark(tmp_path):
    bare, cfg, st = make_env(tmp_path)
    did = add_deal(st, tmp_path)
    r = asyncio.run(publish.sync(st, cfg, tmp_path))
    assert r["pushed"] and r["cards"] == 1
    # edit in the "GitHub app"
    phone = clone2(tmp_path, bare)
    md = phone / "deals/2026-10-29" / f"deal-{did}.md"
    md.write_text(md.read_text().replace("{status: open, price_pln: null, date: null, note: ''}",
                                         "{status: bought, price_pln: 3100, date: 2026-10-30, note: picked up}"))
    git(phone, "commit", "-qam", "mark bought"); git(phone, "push", "-q", "origin", "log")
    r = asyncio.run(publish.sync(st, cfg, tmp_path))
    assert r["marks"] == 1
    m = st.conn.execute("select * from marks").fetchall()
    assert len(m) == 1 and m[0]["status"] == "bought" and m[0]["source"] == "github"
    assert Decimal(m[0]["price_pln"]) == 3100 and "picked up" in m[0]["note"]
    # unchanged -> no duplicate, and card sync did not clobber the edit
    asyncio.run(publish.sync(st, cfg, tmp_path))
    assert st.conn.execute("select count(*) from marks").fetchone()[0] == 1
    assert "bought" in (tmp_path / "clone/deals/2026-10-29" / f"deal-{did}.md").read_text()


def test_index_order():
    def c(i, tier, star, net):
        return publish.CardFile(None, None, i, tier, star, Decimal(net), NOW)
    cards = [c(1, 2, False, "500"), c(2, 1, False, "300"), c(3, 3, False, "900"), c(4, 2, True, "100"),
             c(5, 1, False, "800"), c(6, 2, False, "600")]
    assert [x.deal_id for x in publish.index_order(cards)] == [5, 2, 4, 6, 1, 3]


def test_index_file_written(tmp_path):
    bare, cfg, st = make_env(tmp_path)
    a = add_deal(st, tmp_path, tier=3, net="900")
    b = add_deal(st, tmp_path, tier=1, net="300")
    asyncio.run(publish.sync(st, cfg, tmp_path))
    idx = (tmp_path / "clone/INDEX.md").read_text()
    assert idx.index(f"deal-{b}") < idx.index(f"deal-{a}")
    assert (tmp_path / "clone/daily/2026-10-29.md").is_file()
    assert (tmp_path / "clone/weekly/2026-W44.md").is_file()


def test_push_failure_keeps_state(tmp_path):
    bare, cfg, st = make_env(tmp_path)
    did = add_deal(st, tmp_path)
    asyncio.run(publish.sync(st, cfg, tmp_path))
    phone = clone2(tmp_path, bare)
    md = phone / "deals/2026-10-29" / f"deal-{did}.md"
    md.write_text(md.read_text().replace("status: open", "status: skipped"))
    git(phone, "commit", "-qam", "skip"); git(phone, "push", "-q", "origin", "log")
    add_deal(st, tmp_path, tier=1, net="400")
    # remote becomes unreachable
    moved = tmp_path / "remote.gone"
    bare.rename(moved)
    r = asyncio.run(publish.sync(st, cfg, tmp_path))  # must not raise
    assert not r["pushed"]
    assert st.conn.execute("select count(*) from health where kind='push_failed'").fetchone()[0] == 1
    # state kept: new card committed locally, marks table untouched
    assert "deal-2" in git(tmp_path / "clone", "show", "--stat", "HEAD")
    # remote back: the mark (made while it was up) is imported, backlog pushed
    moved.rename(bare)
    r = asyncio.run(publish.sync(st, cfg, tmp_path))
    assert r["pushed"] and r["marks"] == 1
    assert st.conn.execute("select status from marks").fetchone()["status"] == "skipped"
    assert git(tmp_path / "clone", "rev-list", "--count", "origin/log..HEAD").strip() == "0"


def test_mark_imported_even_when_push_fails(tmp_path):
    bare, cfg, st = make_env(tmp_path)
    did = add_deal(st, tmp_path)
    asyncio.run(publish.sync(st, cfg, tmp_path))
    phone = clone2(tmp_path, bare)
    md = phone / "deals/2026-10-29" / f"deal-{did}.md"
    md.write_text(md.read_text().replace("status: open", "status: sold"))
    git(phone, "commit", "-qam", "sold"); git(phone, "push", "-q", "origin", "log")
    # pull works, then push is rejected by a failing hook
    hook = bare / "hooks/pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n"); hook.chmod(0o755)
    r = asyncio.run(publish.sync(st, cfg, tmp_path))
    assert r["marks"] == 1 and not r["pushed"]
    assert st.conn.execute("select count(*) from marks where status='sold'").fetchone()[0] == 1
    # card in clone still carries the sold outcome after later syncs
    asyncio.run(publish.sync(st, cfg, tmp_path))
    assert "sold" in (tmp_path / "clone/deals/2026-10-29" / f"deal-{did}.md").read_text()
    assert st.conn.execute("select count(*) from marks").fetchone()[0] == 1


def seller_card(tmp_path, st):
    did = add_deal(st, tmp_path)
    cdir = tmp_path / "var/cards/deals/2026-10-29"
    (cdir / f"deal-{did}.md").write_text(
        f"---\ndeal_id: {did}\nseller_hash: abc\noutcome: {{status: open, price_pln: null, date: null, note: ''}}\n---\n"
        "# RTX\nSeller: Jan Kowalski +48 600 100 200\n- **Seller rating** 4.9\nok line\n")
    (cdir / f"deal-{did}.json").write_text(json.dumps({
        "deal_id": did, "key": "RTX 4090", "seller_hash": "h", "seller_name": "Jan",
        "listing": {"url": "https://olx/1", "title": "t", "price": "1", "seller_id": "9", "phone": "1"},
        "comps": [{"url": "u", "price": 1, "date": "d", "kind": "sold", "seller_hash": "x", "buyer": "y"}],
        "breakdown": {"lines": [{"seller_fee": 1, "label": "a"}]}, "raw_html": "<x>"}))
    return did


def test_no_seller_data_published(tmp_path):
    bare, cfg, st = make_env(tmp_path)
    did = seller_card(tmp_path, st)
    asyncio.run(publish.sync(st, cfg, tmp_path))
    d = tmp_path / "clone/deals/2026-10-29"
    md, js = (d / f"deal-{did}.md").read_text(), (d / f"deal-{did}.json").read_text()
    assert "ok line" in md and "Kowalski" not in md and "seller" not in md.lower()
    data = json.loads(js)
    assert "seller" not in js.lower() and "raw_html" not in data and "phone" not in js
    assert data["comps"] == [{"url": "u", "price": 1, "date": "d", "kind": "sold"}]
    assert data["listing"]["url"] == "https://olx/1" and "seller_id" not in data["listing"]


def test_conflict_recovery_imports_remote_mark_and_pushes(tmp_path):
    bare, cfg, st = make_env(tmp_path)
    did = add_deal(st, tmp_path)
    asyncio.run(publish.sync(st, cfg, tmp_path))
    # unpushed local commit touching the same card (push blocked)
    hook = bare / "hooks/pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n"); hook.chmod(0o755)
    card = tmp_path / f"var/cards/deals/2026-10-29/deal-{did}.md"
    card.write_text(card.read_text() + "local change\n")
    r = asyncio.run(publish.sync(st, cfg, tmp_path))
    assert not r["pushed"] and r["committed"]
    # remote edit to the same card (conflicting)
    hook.unlink()
    phone = clone2(tmp_path, bare)
    md = phone / "deals/2026-10-29" / f"deal-{did}.md"
    md.write_text(md.read_text().replace("# RTX 4090", "# RTX 4090 edited").replace("status: open", "status: bought"))
    git(phone, "commit", "-qam", "bought"); git(phone, "push", "-q", "origin", "log")
    r = asyncio.run(publish.sync(st, cfg, tmp_path))
    assert r["marks"] == 1 and r["pushed"]
    assert st.conn.execute("select status from marks").fetchone()["status"] == "bought"
    assert git(tmp_path / "clone", "rev-list", "--count", "origin/log..HEAD").strip() == "0"
    assert "local change" in (tmp_path / "clone/deals/2026-10-29" / f"deal-{did}.md").read_text()
    assert "bought" in (tmp_path / "clone/deals/2026-10-29" / f"deal-{did}.md").read_text()


def test_poison_files_do_not_stop_sync(tmp_path):
    bare, cfg, st = make_env(tmp_path)
    good = add_deal(st, tmp_path)
    cdir = tmp_path / "var/cards/deals/2026-10-29"
    (cdir / "deal-99.md").write_text("---\n: : [broken\n---\nx")
    (cdir / "deal-99.json").write_text("{not json")
    (cdir / "deal-98.md").write_text("---\ndeal_id: 98\n---\nx")
    (cdir / "deal-98.json").write_text("[]")
    r = asyncio.run(publish.sync(st, cfg, tmp_path))
    assert r["pushed"] and (tmp_path / f"clone/deals/2026-10-29/deal-{good}.md").is_file()
    assert st.conn.execute("select count(*) from health where kind='bad_card'").fetchone()[0] >= 1


def test_broken_yaml_edit_kept_and_other_cards_import(tmp_path):
    bare, cfg, st = make_env(tmp_path)
    a, b = add_deal(st, tmp_path), add_deal(st, tmp_path)
    asyncio.run(publish.sync(st, cfg, tmp_path))
    phone = clone2(tmp_path, bare)
    pa = phone / "deals/2026-10-29" / f"deal-{a}.md"
    pb = phone / "deals/2026-10-29" / f"deal-{b}.md"
    pa.write_text("---\ndeal_id: %d\noutcome: {status: bought, \n---\nmy notes\n" % a)
    pb.write_text(pb.read_text().replace("status: open", "status: sold"))
    git(phone, "commit", "-qam", "edit"); git(phone, "push", "-q", "origin", "log")
    asyncio.run(publish.sync(st, cfg, tmp_path))
    assert "my notes" in (tmp_path / "clone/deals/2026-10-29" / f"deal-{a}.md").read_text()
    assert [r["deal_id"] for r in st.conn.execute("select deal_id from marks")] == [b]
    assert st.conn.execute("select count(*) from health where kind='bad_card'").fetchone()[0] == 1


def test_front_matter_id_beats_filename_and_nonfinite_price_rejected(tmp_path):
    st = Store(tmp_path / "t.db")
    ok = "---\ndeal_id: 7\noutcome: {status: bought, price_pln: 10}\n---\n"
    nan = "---\ndeal_id: 8\noutcome: {status: bought, price_pln: .nan}\n---\n"
    n, bad = publish.import_texts(st, [("deal-3.md", ok), ("deal-8.md", nan)])
    assert n == 1 and bad == {8}
    assert [r["deal_id"] for r in st.conn.execute("select deal_id from marks")] == [7]


def test_wrong_branch_refused(tmp_path):
    bare, cfg, st = make_env(tmp_path)
    (tmp_path / "config" / "publish.yaml").write_text(
        (tmp_path / "config" / "publish.yaml").read_text().replace("branch: log", "branch: main"))
    r = asyncio.run(publish.sync(st, config.load(tmp_path), tmp_path))
    assert not r["pushed"] and st.conn.execute("select count(*) from health where kind='bad_branch'").fetchone()[0] == 1


def _env_with_remote(tmp_path, bare):
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / "publish.yaml").write_text(
        f"interval_min: 30\nremote: {bare}\nbranch: log\nlog_dir: {tmp_path / 'clone'}\ncards_dir: var/cards/deals\n")
    return config.load(tmp_path), Store(tmp_path / "t.db")


def test_empty_remote_without_a_log_branch_gets_log_pushed(tmp_path):
    bare = tmp_path / "empty.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    cfg, st = _env_with_remote(tmp_path, bare)
    asyncio.run(publish.sync(st, cfg, tmp_path))  # no deals yet: the index is published; log is created on the remote
    did = add_deal(st, tmp_path)
    r = asyncio.run(publish.sync(st, cfg, tmp_path))
    assert r["pushed"] and r["committed"]
    heads = git(bare, "for-each-ref", "--format=%(refname)").split()
    assert heads == ["refs/heads/log"]  # log exists, main was never created
    assert f"deal-{did}.md" in git(bare, "ls-tree", "-r", "--name-only", "log")
    assert st.conn.execute("select count(*) from health where source='publish' and kind in "
                           "('clone_failed','pull_failed','fetch_failed','push_failed')").fetchone()[0] == 0


def test_remote_with_only_main_gets_log_branch_and_main_is_untouched(tmp_path):
    bare = tmp_path / "m.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "-q", str(bare), str(seed)], check=True, capture_output=True)
    (seed / "README.md").write_text("hello\n")
    git(seed, "add", "-A"); git(seed, "commit", "-qm", "init"); git(seed, "branch", "-M", "main"); git(seed, "push", "-q", "origin", "main")
    main_sha = git(bare, "rev-parse", "main").strip()
    cfg, st = _env_with_remote(tmp_path, bare)
    did = add_deal(st, tmp_path)
    r = asyncio.run(publish.sync(st, cfg, tmp_path))
    assert r["pushed"]
    assert git(bare, "rev-parse", "main").strip() == main_sha  # never pushes the default branch
    assert f"deal-{did}.md" in git(bare, "ls-tree", "-r", "--name-only", "log")
    assert git(tmp_path / "clone", "symbolic-ref", "HEAD").strip() == "refs/heads/log"
    r2 = asyncio.run(publish.sync(st, cfg, tmp_path))  # second cycle: log now exists remotely; plain pull path
    assert r2["pulled"] and not r2["committed"]


def test_index_shows_gone_deals_after_live_ones(tmp_path):
    bare, cfg, st = make_env(tmp_path)
    a = add_deal(st, tmp_path, tier=1, net="900")
    b = add_deal(st, tmp_path, tier=3, net="100")
    st.conn.execute("update deals set status='gone' where id=?", (a,))
    st.conn.commit()
    asyncio.run(publish.sync(st, cfg, tmp_path))
    idx = (tmp_path / "clone/INDEX.md").read_text()
    line = next(l for l in idx.splitlines() if f"deal-{a}" in l)
    assert "gone" in line and idx.index(f"deal-{b}") < idx.index(f"deal-{a}")


def test_conflict_reset_keeps_clone_only_cards_and_weekly_reports(tmp_path):
    """Deferred minor: the conflict recovery's `reset --hard origin/log` dropped every unpushed file the local card
    dir can no longer regenerate (a card whose source was pruned, a weekly report written but not pushed yet)."""
    bare, cfg, st = make_env(tmp_path)
    did = add_deal(st, tmp_path)
    asyncio.run(publish.sync(st, cfg, tmp_path))
    hook = bare / "hooks/pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n"); hook.chmod(0o755)  # pushes fail for a while
    did2 = add_deal(st, tmp_path, tier=3, net="80", key="RX 7900 XTX")
    (tmp_path / "clone" / "weekly").mkdir(exist_ok=True)
    (tmp_path / "clone" / "weekly" / "2026-10-29.md").write_text("# weekly\n")
    card = tmp_path / f"var/cards/deals/2026-10-29/deal-{did}.md"
    card.write_text(card.read_text().replace("# RTX 4090", "# RTX 4090 local"))  # same line as the remote edit
    r = asyncio.run(publish.sync(st, cfg, tmp_path))
    assert r["committed"] and not r["pushed"]
    for ext in (".md", ".json"):  # the second card's source is gone (pruned): only the clone has it now
        (tmp_path / f"var/cards/deals/2026-10-29/deal-{did2}{ext}").unlink()
    hook.unlink()
    phone = clone2(tmp_path, bare)
    md = phone / "deals/2026-10-29" / f"deal-{did}.md"
    md.write_text(md.read_text().replace("# RTX 4090", "# RTX 4090 edited").replace("status: open", "status: bought"))
    git(phone, "commit", "-qam", "bought"); git(phone, "push", "-q", "origin", "log")
    r = asyncio.run(publish.sync(st, cfg, tmp_path))
    assert not r["pulled"]  # a real conflict: the recovery path (fetch, import, reset) ran
    assert r["pushed"] and r["marks"] == 1 and r.get("restored", 0) >= 2
    tree = git(tmp_path / "clone", "ls-tree", "-r", "--name-only", "origin/log")
    assert f"deals/2026-10-29/deal-{did2}.md" in tree and f"deals/2026-10-29/deal-{did2}.json" in tree
    assert "weekly/2026-10-29.md" in tree
    assert "bought" in (tmp_path / "clone/deals/2026-10-29" / f"deal-{did}.md").read_text()  # origin's mark kept
