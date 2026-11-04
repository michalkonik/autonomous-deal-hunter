"""Integration wiring of the improvement loop: the round hook (serve and `dh round`), exception isolation, the weekly
report, and the CLI (parsing and calls, with fakes)."""
import asyncio
import json
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from dealfix import make_deal  # noqa: E402

from dh import cli, config, ledger, publish, runtime  # noqa: E402
from dh import improve  # noqa: E402
from dh.scheduler import RoundStats  # noqa: E402
from dh.store import Store  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
ROLES = ["gemma-4-26b-a4b-pool", "qwen3.8-flash-next-uncensored"]


@pytest.fixture
def droot(tmp_path, monkeypatch):
    shutil.copytree(REPO / "config", tmp_path / "config", ignore=shutil.ignore_patterns("local"))
    shutil.copytree(REPO / "playbook", tmp_path / "playbook")
    (tmp_path / "var").mkdir()
    monkeypatch.setenv("DH_ROOT", str(tmp_path))
    st = Store(tmp_path / "var" / "dh.db")
    make_deal(st)
    st.close()
    return tmp_path


def _snapshot(db: Path) -> dict:
    """Row contents of the tables the loop owns, for 'unchanged' assertions."""
    con = sqlite3.connect(db)
    try:
        out = {}
        for t in ("playbook_versions", "improve_state", "improve_commands", "proposals"):
            try:
                out[t] = con.execute(f"select * from {t} order by 1").fetchall()
            except sqlite3.Error:
                out[t] = None
        return out
    finally:
        con.close()


class Recorder:
    """Stand-in for improve.cli_*: records the call, returns (text, code)."""

    def __init__(self, code=0):
        self.calls, self.code = [], code

    def __call__(self, *a, **kw):
        self.calls.append((a, kw))
        return "OUT", self.code


# ------------------------------------------------------------------------------------------------ CLI


@pytest.mark.parametrize("argv, fn, args", [
    (["playbook"], "cli_playbook_show", ()),
    (["playbook", "--rollback", "3"], "cli_rollback", (3,)),
    (["playbook", "--maintenance", "on"], "cli_maintenance", (True,)),
    (["playbook", "--maintenance", "off"], "cli_maintenance", (False,)),
    (["playbook", "--pending"], "cli_pending", ()),
    (["playbook", "--label", "7", "not_deal"], "cli_label", (7, "not_deal")),
    (["playbook", "--label", "7", "key:gpu:rtx-3060:12"], "cli_label", (7, "key:gpu:rtx-3060:12")),
    (["eval"], "cli_eval", (None,)),
    (["eval", "--version", "2"], "cli_eval", (2,)),
    (["report", "--weekly"], "cli_weekly", ()),
])
def test_cli_dispatch_calls_the_improve_function(droot, monkeypatch, capsys, argv, fn, args):
    rec = Recorder()
    monkeypatch.setattr(improve, fn, rec)
    assert cli.main(argv) == 0
    assert len(rec.calls) == 1
    a, kw = rec.calls[0]
    assert a == args and Path(kw["root"]) == droot
    assert capsys.readouterr().out.strip() == "OUT"


def test_cli_apply_dispatch_needs_the_running_agent(droot, monkeypatch, capsys):
    rec = Recorder()
    monkeypatch.setattr(improve, "cli_apply", rec)
    monkeypatch.setattr(runtime, "is_active", lambda run=None: True)
    assert cli.main(["playbook", "--apply", "5"]) == 0
    a, kw = rec.calls[0]
    assert a == (5,) and kw["running"] is True
    monkeypatch.setattr(runtime, "is_active", lambda run=None: False)
    rec.calls.clear()
    assert cli.main(["playbook", "--apply", "5"]) == 1
    assert rec.calls == [] and "refused: --apply needs the running agent" in capsys.readouterr().out


def test_cli_propagates_the_exit_code(droot, monkeypatch, capsys):
    monkeypatch.setattr(improve, "cli_eval", Recorder(code=1))
    assert cli.main(["eval"]) == 1
    monkeypatch.setattr(improve, "cli_rollback", Recorder(code=1))
    assert cli.main(["playbook", "--rollback", "9"]) == 1


def test_cli_owner_actions_are_mutually_exclusive(droot):
    with pytest.raises(SystemExit):
        cli.main(["playbook", "--pending", "--apply", "1"])
    with pytest.raises(SystemExit):
        cli.main(["report"])  # --weekly is required


def test_no_stub_left_for_eval_and_playbook():
    assert cli.STUBS == ["replay"]
    assert "report" in cli.COMMANDS


def test_playbook_show_runs_for_real_on_a_temp_root(droot, capsys):
    assert cli.main(["playbook"]) == 0
    assert "active playbook" in capsys.readouterr().out


def test_eval_add_from_marks_is_refused_while_the_agent_runs(droot, monkeypatch, capsys):
    monkeypatch.setattr(runtime, "is_active", lambda run=None: True)
    assert cli.main(["eval", "--add-from-marks"]) == 1
    assert "stop the agent first" in capsys.readouterr().out


def test_eval_add_from_marks_when_stopped(droot, monkeypatch, capsys):
    monkeypatch.setattr(runtime, "is_active", lambda run=None: False)
    seen = []
    import evals.marks as em
    monkeypatch.setattr(em, "add_from_marks", lambda store, *a, **kw: seen.append(store) or 4)
    assert cli.main(["eval", "--add-from-marks"]) == 0
    assert len(seen) == 1 and "added from marks: 4" in capsys.readouterr().out


def test_report_weekly_publish_writes_into_the_log_repo(droot, monkeypatch, capsys):
    monkeypatch.setattr(improve, "cli_weekly", Recorder())
    monkeypatch.setattr(runtime, "is_active", lambda run=None: False)
    calls = []

    async def fake_pub(store, cfg, root, *, now=None, force=False):
        calls.append(force)
        return Path("/x/weekly/2026-10-29.md")
    monkeypatch.setattr(publish, "publish_weekly", fake_pub)
    assert cli.main(["report", "--weekly", "--publish"]) == 0
    assert calls == [True] and "weekly/2026-10-29.md" in capsys.readouterr().out

    async def none_pub(*a, **kw):
        return None
    monkeypatch.setattr(publish, "publish_weekly", none_pub)
    assert cli.main(["report", "--weekly", "--publish"]) == 1


def test_status_improvement_section_is_read_only_until_initialised(droot, monkeypatch, capsys):
    monkeypatch.setattr(runtime, "status", lambda cfg, root: {"running": False})
    db = droot / "var" / "dh.db"
    assert cli.main(["status", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["improvement"] == {"initialised": False}
    con = sqlite3.connect(db)
    assert con.execute("select count(*) from sqlite_master where name='improve_state'").fetchone()[0] == 0
    con.close()
    improve.cli_maintenance(False, root=droot, running=False)  # initialises (and commits) the controller state
    import hashlib
    sha = lambda: hashlib.sha256(db.read_bytes()).hexdigest()  # noqa: E731
    con = sqlite3.connect(db)
    con.execute("pragma wal_checkpoint(truncate)")
    con.close()
    before, snap = sha(), _snapshot(db)
    assert cli.main(["status", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["improvement"]["initialised"] is True and out["improvement"]["phase"] == "idle"
    assert out["improvement"]["watched_hours"] == 0.0 and out["improvement"]["owner_review"] == []
    assert sha() == before and _snapshot(db) == snap  # read-only: not one byte or row changed


def test_status_text_appends_the_improvement_section(droot, monkeypatch, capsys):
    full = {"running": False, "unit": "deal-hunter", "state": "inactive", "rounds_total": 0, "rounds_since_start": 0,
            "watched_hours": 0.0, "last_round": None, "deals_today": {}, "health": {}, "llama_swap": {},
            "ntfy_topic": "t", "log_repo": {}, "llm_today": {}}
    monkeypatch.setattr(runtime, "status", lambda cfg, root: full)
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "improvement loop:" in out and "not initialised yet" in out


# ------------------------------------------------------------------------------------------------ the round hook


class Hook:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    async def __call__(self, ctx, st, **kw):
        self.calls.append(st.round_id)
        if self.fail:
            raise RuntimeError("controller blew up")
        return []


def _serve(tmp_path, monkeypatch, hook, rounds=2, weekly_spy=None):
    async def fake_round(ctx):
        n = len(hook.calls) + 1
        ledger.observe(ctx.store, "kleinanzeigen", [], ctx.now(), arm=None)
        if n == rounds:
            ctx.stop.set()
        return RoundStats(round_id=n)

    async def fake_publish(store, cfg, root, stop):
        await stop.wait()

    async def fake_warm(cfg, **kw):
        return {m: "ok" for m in ROLES}
    monkeypatch.setattr(runtime, "warm_models", fake_warm)
    monkeypatch.setattr(runtime.scheduler, "run_round", fake_round)
    monkeypatch.setattr(runtime.publish, "run_loop", fake_publish)
    monkeypatch.setattr(improve, "after_round", hook)
    if weekly_spy is not None:
        monkeypatch.setattr(runtime.publish, "publish_weekly", weekly_spy)
    data = config.load(REPO).to_dict()
    data["scheduler"] = {**data.get("scheduler", {}), "round_interval_s": 0}
    c = config.Config(data, REPO)
    stop = asyncio.Event()
    return asyncio.run(runtime.serve_async(c, tmp_path, stop=stop, install_signals=False, web=False))


def test_serve_calls_after_round_once_per_round_and_the_weekly_check(tmp_path, monkeypatch):
    hook, weekly = Hook(), []

    async def spy(store, cfg, root, **kw):
        weekly.append(1)
        return None
    assert _serve(tmp_path, monkeypatch, hook, rounds=3, weekly_spy=spy) == 0
    assert hook.calls == [1, 2, 3] and len(weekly) == 3


def test_an_improve_exception_never_stops_the_round_loop(tmp_path, monkeypatch):
    hook = Hook(fail=True)
    assert _serve(tmp_path, monkeypatch, hook, rounds=3) == 0
    assert hook.calls == [1, 2, 3]  # every round still ran and still called the hook
    st = Store(tmp_path / "var" / "dh.db")
    rows = st.conn.execute("select source, detail from health where kind='improve_error'").fetchall()
    assert len(rows) == 3 and "controller blew up" in rows[0]["detail"]
    assert st.conn.execute("select count(*) from health where kind='round_failed'").fetchone()[0] == 0


def test_a_weekly_failure_is_a_health_event_not_a_crash(tmp_path, monkeypatch):
    async def bad(*a, **kw):
        raise OSError("disk full")
    assert _serve(tmp_path, monkeypatch, Hook(), rounds=2, weekly_spy=bad) == 0
    st = Store(tmp_path / "var" / "dh.db")
    assert st.conn.execute("select count(*) from health where kind='weekly_error'").fetchone()[0] == 2


def test_cancellation_is_not_swallowed_by_the_hook_isolation(tmp_path, monkeypatch):
    class Ctx:
        cfg = config.load(REPO)
        root = tmp_path
    ctx = Ctx()
    ctx.store = Store(tmp_path / "c.db")

    async def slow(c, st, **kw):
        await asyncio.sleep(30)
    monkeypatch.setattr(improve, "after_round", slow)

    async def go():
        t = asyncio.create_task(runtime.post_round(ctx, RoundStats(round_id=1)))
        await asyncio.sleep(0.05)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
    asyncio.run(go())


def test_dh_round_calls_after_round_once_per_round_and_no_weekly(droot, monkeypatch, capsys):
    hook, weekly = Hook(), []
    monkeypatch.setattr(improve, "after_round", hook)

    async def spy(*a, **kw):
        weekly.append(1)
    monkeypatch.setattr(publish, "publish_weekly", spy)
    monkeypatch.setattr(runtime, "is_active", lambda run=None: False)
    monkeypatch.setattr(runtime, "preflight", lambda cfg, need_llm=True: ([], None, []))

    async def fake_round(ctx):
        return RoundStats(round_id=11)
    monkeypatch.setattr(runtime.scheduler, "run_round", fake_round)
    assert cli.main(["round", "--once", "--dry", "--no-llm"]) == 0
    assert hook.calls == [11] and weekly == []


def test_dh_round_survives_an_improve_exception(droot, monkeypatch, capsys):
    hook = Hook(fail=True)
    monkeypatch.setattr(improve, "after_round", hook)
    monkeypatch.setattr(runtime, "is_active", lambda run=None: False)
    monkeypatch.setattr(runtime, "preflight", lambda cfg, need_llm=True: ([], None, []))

    async def fake_round(ctx):
        return RoundStats(round_id=12)
    monkeypatch.setattr(runtime.scheduler, "run_round", fake_round)
    assert cli.main(["round", "--once", "--dry", "--no-llm"]) == 0
    st = Store(droot / "var" / "dh.db")
    assert st.conn.execute("select count(*) from health where kind='improve_error'").fetchone()[0] == 1


# ------------------------------------------------------------------------------------------------ weekly report


def _wk_cfg(tmp_path, clone: bool):
    log = tmp_path / "log"
    if clone:
        (log / ".git").mkdir(parents=True)
    data = config.load(REPO).to_dict()
    data["publish"] = {**data["publish"], "log_dir": str(log)}
    return config.Config(data, REPO), log


def _watch(st, start: datetime, hours: float):
    w = ledger.open_window(st, "olx", start)
    st.close_window(w, start + timedelta(hours=hours))


def test_weekly_is_published_every_168_watched_hours(tmp_path, monkeypatch):
    from dh.improve import report
    texts = []

    async def fake_weekly(store, now=None, cfg=None):
        texts.append(now)
        return "# weekly\n"
    monkeypatch.setattr(report, "weekly_async", fake_weekly)
    cfg, log = _wk_cfg(tmp_path, clone=True)
    st = Store(tmp_path / "w.db")
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    now = t0 + timedelta(days=30)
    _watch(st, t0, 100)
    assert asyncio.run(publish.publish_weekly(st, cfg, tmp_path, now=now)) is None  # 100 h: not due
    _watch(st, t0 + timedelta(days=10), 80)  # 180 watched hours
    p = asyncio.run(publish.publish_weekly(st, cfg, tmp_path, now=now))
    assert p == log / "weekly" / "2026-01-31.md" and p.read_text() == "# weekly\n"
    assert asyncio.run(publish.publish_weekly(st, cfg, tmp_path, now=now)) is None  # not due again at once
    _watch(st, t0 + timedelta(days=20), 160)  # 340 hours: 160 since the last report
    assert asyncio.run(publish.publish_weekly(st, cfg, tmp_path, now=now)) is None
    _watch(st, t0 + timedelta(days=28), 10)  # 350: 170 since
    assert asyncio.run(publish.publish_weekly(st, cfg, tmp_path, now=now + timedelta(days=1))) is not None
    assert len(texts) == 2


def test_weekly_without_a_clone_waits_and_force_overrides_the_due_check(tmp_path, monkeypatch):
    from dh.improve import report

    async def fake_weekly(store, now=None, cfg=None):
        return "# w\n"
    monkeypatch.setattr(report, "weekly_async", fake_weekly)
    cfg, log = _wk_cfg(tmp_path, clone=False)
    st = Store(tmp_path / "w.db")
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    _watch(st, t0, 200)
    assert asyncio.run(publish.publish_weekly(st, cfg, tmp_path, now=t0 + timedelta(days=9))) is None
    assert st.conn.execute("select count(*) from health where kind='weekly_no_clone'").fetchone()[0] == 1
    assert st.conn.execute("select count(*) from weekly_reports").fetchone()[0] == 0  # retried next round
    (log / ".git").mkdir(parents=True)
    st2 = Store(tmp_path / "w2.db")  # nothing watched: force still writes
    assert asyncio.run(publish.publish_weekly(st2, cfg, tmp_path, now=t0, force=True)) is not None


# ------------------------------------------------------------------------------------------------ owner commands: queue vs apply


@pytest.fixture
def inited(droot):
    improve.cli_maintenance(False, root=droot, running=False)  # baseline playbook version + controller state, committed
    return droot / "var" / "dh.db"


@pytest.mark.parametrize("argv", [["playbook", "--rollback", "1"], ["playbook", "--maintenance", "on"],
                                  ["playbook", "--label", "3", "deal"], ["playbook", "--apply", "9"]])
def test_owner_commands_are_queued_while_the_unit_runs(inited, monkeypatch, capsys, argv):
    monkeypatch.setattr(runtime, "is_active", lambda run=None: True)
    before = _snapshot(inited)
    assert cli.main(argv) == 0
    assert "queued as command" in capsys.readouterr().out
    after = _snapshot(inited)
    assert len(after["improve_commands"]) == len(before["improve_commands"]) + 1  # only the queued row is new
    assert after["playbook_versions"] == before["playbook_versions"]
    assert after["improve_state"] == before["improve_state"]
    row = sqlite3.connect(inited).execute("select cmd, applied_at from improve_commands order by id desc limit 1").fetchone()
    assert row[0] == {"--rollback": "rollback", "--maintenance": "maintenance", "--label": "label",
                      "--apply": "apply"}[argv[1]] and row[1] is None  # waits for the agent's next round


def test_owner_commands_apply_at_once_when_the_unit_is_stopped(inited, monkeypatch, capsys):
    monkeypatch.setattr(runtime, "is_active", lambda run=None: False)
    assert cli.main(["playbook", "--maintenance", "on"]) == 0
    out = capsys.readouterr().out
    assert "queued" not in out and json.loads(out)["ok"] is True
    con = sqlite3.connect(inited)
    assert con.execute("select applied_at is not null from improve_commands").fetchone()[0] == 1
    assert json.loads(con.execute("select value from improve_state where key='controller'").fetchone()[0])["maintenance"]
    # a rollback to a version that does not exist is refused, applied (not queued), exit 1
    assert cli.main(["playbook", "--rollback", "99"]) == 1
    assert "queued" not in capsys.readouterr().out
    # a label of a disagreement that does not exist: applied at once, refused
    assert cli.main(["playbook", "--label", "424242", "deal"]) == 1
    assert "queued" not in capsys.readouterr().out
    # --apply with the unit stopped refuses before queueing anything
    n = con.execute("select count(*) from improve_commands").fetchone()[0]
    assert cli.main(["playbook", "--apply", "1"]) == 1
    assert "refused: --apply needs the running agent" in capsys.readouterr().out
    assert con.execute("select count(*) from improve_commands").fetchone()[0] == n


def test_report_publish_refuses_while_the_unit_runs(droot, monkeypatch, capsys):
    monkeypatch.setattr(improve, "cli_weekly", Recorder())
    monkeypatch.setattr(runtime, "is_active", lambda run=None: True)
    assert cli.main(["report", "--weekly", "--publish"]) == 1
    assert "refused: the agent is running" in capsys.readouterr().out


def test_weekly_tmp_file_is_written_outside_the_clone(tmp_path, monkeypatch):
    from dh.improve import report

    async def fake_weekly(store, now=None, cfg=None):
        return "# w\n"
    monkeypatch.setattr(report, "weekly_async", fake_weekly)
    cfg, log = _wk_cfg(tmp_path, clone=True)
    st = Store(tmp_path / "w.db")
    p = asyncio.run(publish.publish_weekly(st, cfg, tmp_path, now=datetime(2026, 1, 1, tzinfo=timezone.utc), force=True))
    assert p.is_file() and not list(log.rglob("*.tmp")) and not list(tmp_path.glob(".log-weekly.tmp"))


# ------------------------------------------------------------------------------------------------ post_round timeout


def test_post_round_cuts_a_stuck_improve_step_with_a_health_event(tmp_path, monkeypatch):
    async def stuck(c, st, **kw):
        await asyncio.sleep(30)
    monkeypatch.setattr(improve, "after_round", stuck)
    data = config.load(REPO).to_dict()
    data["improve"] = {"post_round_timeout_s": 0.2}

    class Ctx:
        cfg = config.Config(data, REPO)
        root = tmp_path
    ctx = Ctx()
    ctx.store = Store(tmp_path / "t.db")
    asyncio.run(runtime.post_round(ctx, RoundStats(round_id=1), weekly=False))
    assert ctx.store.conn.execute("select count(*) from health where kind='improve_timeout'").fetchone()[0] == 1


def test_post_round_timeout_default_is_300(tmp_path):
    assert config.load(REPO).improve.post_round_timeout_s == 300
