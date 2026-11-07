"""Fix wave A: config policy keys, quiet hours, paid-API cap."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import yaml

from dh import books, config, notify
from dh.config import Config, PolicyViolation
from dh.llm import Client, PaidCapReached
from dh.store import Store

REPO = Path(__file__).resolve().parents[1]
SCHEMA = {"type": "object", "required": ["a"], "properties": {"a": {"type": "integer"}}}


# ------------------------------------------------------------------ no-circumvention policy keys


@pytest.mark.parametrize("key", ["proxies", "solve_captcha", "logged_in"])
def test_config_refuses_to_load_with_a_circumvention_key_set(tmp_path, key):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "sources.yaml").write_text(yaml.safe_dump({"fetch": {key: True, "min_interval_s": 4}}))
    with pytest.raises(PolicyViolation, match=key):
        config.load(tmp_path)


def test_local_override_cannot_turn_a_policy_key_on(tmp_path):
    (tmp_path / "config" / "local").mkdir(parents=True)
    (tmp_path / "config" / "sources.yaml").write_text(yaml.safe_dump({"fetch": {"proxies": False}}))
    (tmp_path / "config" / "local" / "sources.yaml").write_text(yaml.safe_dump({"fetch": {"proxies": ["http://p:1"]}}))
    with pytest.raises(PolicyViolation):
        config.load(tmp_path)


def test_shipped_config_has_the_keys_false_and_loads():
    f = config.load(REPO).sources.fetch
    assert f.proxies is False and f.solve_captcha is False and f.logged_in is False


def test_cli_start_refuses_with_a_policy_key(tmp_path, monkeypatch):
    import shutil
    shutil.copytree(REPO / "config", tmp_path / "config", ignore=shutil.ignore_patterns("local"))
    (tmp_path / "config" / "local").mkdir()
    (tmp_path / "config" / "local" / "sources.yaml").write_text("fetch: {solve_captcha: true}\n")
    monkeypatch.setenv("DH_ROOT", str(tmp_path))
    from dh import cli
    with pytest.raises(PolicyViolation):
        cli.main(["start"])


# ------------------------------------------------------------------ quiet hours


def _cfg(tmp_path, quiet):
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / "notify.yaml").write_text(yaml.safe_dump({
        "ntfy": {"server": "https://ntfy.test", "topic": None, "tier1_priority": "urgent"},
        "email_fallback": {"enabled": False}, "quiet_hours": quiet}))
    return config.load(tmp_path)


def _deal():
    from dh.models import Deal
    return Deal(id=1, listing_ref=1, key="RTX 4090", tier=1, star=False, effort="easy", confidence="high",
                cons_net_pln=Decimal("2500"), exp_net_pln=Decimal("3000"), cons_net_usd=Decimal("639.6"),
                exp_net_usd=Decimal("700"), capital_usd=Decimal("1500"), breakdown_json="{}", evidence_json="{}",
                reasons=[], created_at=datetime.now(timezone.utc))


def test_quiet_until_overnight_window(tmp_path):
    cfg = _cfg(tmp_path, {"start": "23:00", "end": "07:00", "tz": "Europe/Warsaw"})
    utc = lambda *a: datetime(*a, tzinfo=timezone.utc)  # noqa: E731  (Warsaw = UTC+1 on 2026-01-15)
    assert notify.quiet_until(cfg, utc(2026, 1, 15, 21, 30)) is None  # 22:30 local: before 23:00
    assert notify.quiet_until(cfg, utc(2026, 1, 15, 22, 30)) == utc(2026, 1, 16, 6, 0)  # 23:30 local
    assert notify.quiet_until(cfg, utc(2026, 1, 16, 4, 0)) == utc(2026, 1, 16, 6, 0)  # 05:00 local
    assert notify.quiet_until(cfg, utc(2026, 1, 16, 6, 0)) is None  # 07:00 local: window over
    assert notify.quiet_until(cfg, utc(2026, 1, 15, 12, 0)) is None


def test_no_quiet_hours_by_default_and_malformed_means_none(tmp_path):
    assert config.load(REPO).notify.get("quiet_hours") is None
    now = datetime(2026, 1, 15, 23, 30, tzinfo=timezone.utc)
    assert notify.quiet_until(config.load(REPO), now) is None
    assert notify.quiet_until(_cfg(tmp_path, {"start": "late", "end": "early"}), now) is None


def test_tier1_push_inside_quiet_hours_is_queued_until_the_window_ends(tmp_path):
    cfg = _cfg(tmp_path, {"start": "23:00", "end": "07:00", "tz": "Europe/Warsaw"})
    st = Store(tmp_path / "t.db")
    sent = []
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: sent.append(1) or httpx.Response(200)))
    night = datetime(2026, 1, 15, 23, 30, tzinfo=timezone.utc)  # 00:30 Warsaw
    ok = asyncio.run(notify.push_tier1(_deal(), "https://card", store=st, cfg=cfg, root=tmp_path, client=client,
                                       listing_url="https://olx/1", now=night))
    assert ok is False and sent == []
    row = st.conn.execute("select * from pending_pushes").fetchone()
    assert row["not_before"].startswith("2026-01-16T06:00")
    # still quiet: the retry leaves it alone (and does not expire it)
    assert asyncio.run(notify.retry_pending(st, cfg, tmp_path, client=client, now=night + timedelta(hours=3))) == 0
    assert sent == [] and st.conn.execute("select count(*) from pending_pushes").fetchone()[0] == 1
    # after the window: delivered. The 6 h retry window runs from the window's end, not from the queueing time.
    morning = datetime(2026, 1, 16, 6, 5, tzinfo=timezone.utc)
    assert asyncio.run(notify.retry_pending(st, cfg, tmp_path, client=client, now=morning)) == 1
    assert sent == [1] and st.conn.execute("select count(*) from pending_pushes").fetchone()[0] == 0
    # outside the window: sent at once
    day = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
    assert asyncio.run(notify.push_tier1(_deal(), "https://card", store=st, cfg=cfg, root=tmp_path, client=client,
                                         now=day)) is True


# ------------------------------------------------------------------ paid API cap


def _paid_cfg(cap, enabled=True):
    return Config({"models": {"base_url": "http://local/v1", "roles": {"worker": "w", "advisor": "a"},
                              "paid_api": {"enabled": enabled, "monthly_cap_usd": cap, "base_url": "https://paid.test/v1",
                                           "roles": {"advisor": "big-model"}, "usd_per_mtok_in": 10,
                                           "usd_per_mtok_out": 30}}})


def _reply():
    return httpx.Response(200, json={"choices": [{"message": {"content": '{"a": 1}'}, "finish_reason": "stop"}],
                                     "usage": {"prompt_tokens": 1000, "completion_tokens": 500}})


def test_paid_role_is_refused_with_the_default_cap_of_zero():
    hits = []
    c = Client(_paid_cfg(0), transport=httpx.MockTransport(lambda r: hits.append(str(r.url)) or _reply()))
    with pytest.raises(PaidCapReached):
        asyncio.run(c.chat_json("advisor", "s", "u", SCHEMA))
    assert hits == []  # nothing left the machine
    assert asyncio.run(c.chat_json("worker", "s", "u", SCHEMA)) == {"a": 1}  # local roles are unaffected
    assert hits == ["http://local/v1/chat/completions"]


def test_paid_role_is_refused_when_paid_api_is_disabled():
    c = Client(_paid_cfg(100, enabled=False), transport=httpx.MockTransport(lambda r: _reply()))
    # not a paid role at all when disabled: it is simply the local model of that role
    assert asyncio.run(c.chat_json("advisor", "s", "u", SCHEMA)) == {"a": 1}
    assert c.paid_roles == {}


def test_paid_call_is_refused_once_the_month_to_date_spend_reaches_the_cap():
    hits, spent, billed = [], [0.0], []
    c = Client(_paid_cfg(1.0), transport=httpx.MockTransport(lambda r: hits.append(str(r.url)) or _reply()),
               paid_spend=lambda: spent[0], on_paid=lambda role, usd: billed.append(usd))
    assert asyncio.run(c.chat_json("advisor", "s", "u", SCHEMA)) == {"a": 1}
    assert hits == ["https://paid.test/v1/chat/completions"]
    assert billed == [pytest.approx(0.025)]  # 1000 x $10/M + 500 x $30/M
    spent[0] = 0.99  # the books say $0.99 so far this month; this client's own $0.025 pushes it over
    with pytest.raises(PaidCapReached):
        asyncio.run(c.chat_json("advisor", "s", "u", SCHEMA))
    assert len(hits) == 1


def test_books_track_paid_spend_by_month(tmp_path):
    cfg = Config({"books": {"tariff_pln_kwh": 1.1, "base_w": 100}, "models": {"paid_api": {"pln_per_usd": 4.0}}})
    st = Store(tmp_path / "t.db")
    now = datetime(2026, 10, 29, 12, tzinfo=timezone.utc)
    r_old = st.log_round(now - timedelta(days=40))
    st.add_cost(r_old, "paid_api", Decimal("40"), 10.0)  # last month: not counted
    rid = st.log_round(now - timedelta(hours=1))
    out = books.record_round(st, rid, cfg, seconds=10, gpu_watts=[100.0],
                             llm={"calls": 1, "prompt_tokens": 1, "completion_tokens": 1, "paid_usd": 0.25}, fetches=0)
    assert out["paid_api_pln"] == Decimal("1.00")
    assert books.paid_month_usd(st, now) == pytest.approx(0.25)


def test_paid_cap_counts_spend_once_across_rounds_and_a_month_boundary(tmp_path):
    """books.paid_month_usd is the paid_spend; each round's cost row is booked and then leaves the client's unbooked
    counter, so books + unbooked == the truth (no double count), and a new month starts again from zero."""
    st = Store(tmp_path / "t.db")
    clock = [datetime(2026, 10, 30, 12, tzinfo=timezone.utc)]
    cfg = Config({"books": {"tariff_pln_kwh": 1.1, "base_w": 100}, **_paid_cfg(1.0).to_dict()})
    c = Client(cfg, transport=httpx.MockTransport(lambda r: _reply()),
               paid_spend=lambda: books.paid_month_usd(st, clock[0]))  # $0.025 per call
    usage = {"paid_usd": 0.0}
    c._on_paid = lambda role, usd: usage.__setitem__("paid_usd", usage["paid_usd"] + usd)

    def end_round(started):
        rid = st.log_round(started)
        books.record_round(st, rid, cfg, seconds=1, gpu_watts=[], llm={"paid_usd": usage["paid_usd"]}, fetches=0)
        c.paid_booked(usage["paid_usd"])
        usage["paid_usd"] = 0.0

    for _ in range(20):  # round 1: 20 calls = $0.50
        asyncio.run(c.chat_json("advisor", "s", "u", SCHEMA))
    end_round(clock[0])
    assert books.paid_month_usd(st, clock[0]) == pytest.approx(0.5) and c._paid_session == pytest.approx(0.0)
    for _ in range(19):  # round 2: 19 more = $0.475; truth $0.975 < cap. A double count would have refused at ~$0.5+
        asyncio.run(c.chat_json("advisor", "s", "u", SCHEMA))
    end_round(clock[0] + timedelta(minutes=10))
    assert books.paid_month_usd(st, clock[0]) == pytest.approx(0.975)
    with pytest.raises(PaidCapReached):  # one more would reach $1.00: refused only now (0.975 + 0.025 is allowed once, then 1.0 >= cap)
        asyncio.run(c.chat_json("advisor", "s", "u", SCHEMA))
        asyncio.run(c.chat_json("advisor", "s", "u", SCHEMA))
    # month rollover: the books' month-to-date restarts at 0, the cap is open again
    clock[0] = datetime(2026, 11, 1, 0, 5, tzinfo=timezone.utc)
    c.paid_booked(usage["paid_usd"])
    usage["paid_usd"] = 0.0
    assert books.paid_month_usd(st, clock[0]) == 0.0 and c._paid_session == pytest.approx(0.0)
    assert asyncio.run(c.chat_json("advisor", "s", "u", SCHEMA)) == {"a": 1}


def test_negative_net_tier4_row_cannot_make_the_bandit_update_raise():
    """A trend tier-4 deal can carry a negative expected net (min_cons_net_usd < 0): value_units goes negative and
    Bandit.update refuses negatives, so the scheduler clamps the arm's value at 0."""
    from dh.bandit import value_units
    assert value_units([(4, -500.0)]) < 0  # the raw sum is negative ...
    import inspect
    from dh import scheduler
    assert "max(0.0, value_units(" in inspect.getsource(scheduler._Round.run)  # ... and the update site clamps it
