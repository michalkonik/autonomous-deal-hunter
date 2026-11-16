"""`dh recheck` (review of the 30 Oct wave): comparables stored by an older matcher are re-judged by the current one."""
import json
from datetime import datetime, timezone
from decimal import Decimal

from dh import cli, ledger, recheck, runtime
from dh.models import Deal, Extraction, Listing
from dh.store import Store

NOW = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)


def _listing(st, sid, title, price):
    l = Listing(source="allegro_lokalnie", source_id=sid, url=f"https://allegrolokalnie.pl/oferta/{sid}", title=title,
                description="", price=Decimal(price), currency="PLN", country="PL", city=None, lat=None, lon=None,
                seller_hash=None, seller_age_days=None, seller_ratings=None, seller_score=None, ships=True,
                pickup_only=False, is_business=False, photos=[], posted_at=None, seen_at=NOW)
    lid = st.upsert_listing(l)
    st.record_observation(lid, l.price, NOW)
    return lid


def _old_verdict(st, lid, key):
    """What the pre-fix matcher stored: a certain card, a comparable."""
    from dh.scheduler import EVAL_SQL
    st.ensure_table(EVAL_SQL)
    st.save_extraction(lid, Extraction(key=key, category="gpu", certainty=0.95, condition="used", method="rules"))
    st.conn.execute("insert or replace into evaluations (listing_id, price, title_hash, at, tier, note) "
                    "values (?,?,?,?,?,?)", (lid, "1", "h", NOW.isoformat(), None, ""))
    st.conn.commit()


def _seed(tmp_path):
    st = Store(tmp_path / "dh.db")
    ids = {
        "laptop": _listing(st, "a", "Msi - Inch Core™ Ultra 9 275hx 32 Gb 2 Tb Geforce Rtx™ 5090", "14000"),
        "block": _listing(st, "b", "Blok wodny Alphacool Rtx 4070TI Asus rog Strix", "380"),
        "card": _listing(st, "c", "MSI RTX 5090 Suprim 32GB", "12000"),
    }
    _old_verdict(st, ids["laptop"], "gpu:rtx-5090:32")
    _old_verdict(st, ids["block"], "gpu:rtx-4070-ti:12")
    _old_verdict(st, ids["card"], "gpu:rtx-5090:32")
    st.save_extraction(_listing(st, "d", "RTX 3080 laptop", "900"),  # an LLM verdict: left alone
                       Extraction(key="gpu:rtx-3080:10", category="gpu", certainty=0.9, condition="used", method="llm",
                                  notes=["prompt_sha:x"]))
    did = st.save_deal(Deal(id=None, listing_ref=ids["block"], key="gpu:rtx-4070-ti:12", tier=3, star=False,
                            effort="easy", confidence="low", cons_net_pln=Decimal(1), exp_net_pln=Decimal(1),
                            cons_net_usd=Decimal(1), exp_net_usd=Decimal(1), capital_usd=Decimal(1),
                            breakdown_json="{}", evidence_json="{}", reasons=[], created_at=NOW))
    return st, ids, did


def _asks(st, key):
    return st.conn.execute(f"select count(*) from listings l join extractions e on e.listing_id=l.id "
                           f"where e.key=? and {ledger._SALEABLE}", (key,)).fetchone()[0]


def test_dry_run_reports_and_changes_nothing(tmp_path):
    st, ids, did = _seed(tmp_path)
    res = recheck.recheck(st)
    assert res["checked"] == 3 and res["changed"] == 2 and res["applied"] is False
    assert res["by_key"] == {"gpu:rtx-5090:32": 1, "gpu:rtx-4070-ti:12": 1} and res["deals"] == [did]
    assert _asks(st, "gpu:rtx-5090:32") == 2 and _asks(st, "gpu:rtx-4070-ti:12") == 1  # untouched


def test_apply_drops_them_from_the_comparables_and_requeues_them(tmp_path):
    st, ids, did = _seed(tmp_path)
    res = recheck.recheck(st, apply=True)
    assert res["changed"] == 2
    assert _asks(st, "gpu:rtx-5090:32") == 1 and _asks(st, "gpu:rtx-4070-ti:12") == 0  # the real card stays
    lap = st.conn.execute("select * from extractions where listing_id=?", (ids["laptop"],)).fetchone()
    assert lap["key"] is None or (lap["certainty"] <= 0.5 and json.loads(lap["bundle_items"]))  # not a card
    left = {r[0] for r in st.conn.execute("select listing_id from evaluations")}
    assert left == {ids["card"]}  # the two changed listings are evaluated again (with the LLM) when re-queued
    assert st.conn.execute("select method from extractions where key='gpu:rtx-3080:10'").fetchone()[0] == "llm"
    assert st.get_deal(did).tier == 3  # deals are reported, never changed
    assert recheck.recheck(st)["changed"] == 0  # idempotent


def test_cli_is_refused_while_the_agent_holds_the_lock(tmp_path, monkeypatch, capsys):
    st, ids, did = _seed(tmp_path)
    st.close()
    (tmp_path / "var").mkdir()
    (tmp_path / "dh.db").rename(tmp_path / "var" / "dh.db")
    monkeypatch.setenv("DH_ROOT", str(tmp_path))
    holder = runtime.AgentLock(tmp_path)
    assert holder.acquire()
    try:
        assert cli.main(["recheck", "--apply"]) == 1 and "refused" in capsys.readouterr().out
    finally:
        holder.release()
    assert cli.main(["recheck", "--apply"]) == 0
    out = capsys.readouterr().out
    assert "no longer accepted by the current matcher: 2" in out and f"[{did}]" in out
