"""Runtime: dh start/stop/status guards (RAM, llama-swap), the systemd-run unit, model release, process check."""
import asyncio
import io
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from dh import config, ledger, runtime
from dh.store import Store

REPO = Path(__file__).resolve().parents[1]
ROLES = ["gemma-4-26b-a4b-pool", "qwen3.8-flash-next-uncensored"]


def meminfo(tmp_path, gb: float) -> Path:
    p = tmp_path / "meminfo"
    p.write_text(f"MemTotal:       62914560 kB\nMemFree:          524288 kB\nMemAvailable:   {int(gb * 1024 * 1024)} kB\n")
    return p


class Sys:
    """Fake subprocess.run for systemctl / systemd-run / pgrep."""

    def __init__(self, active=False, pids=""):
        self.calls, self.active, self.pids = [], active, pids

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        out, rc = "", 0
        if cmd[:3] == ["systemctl", "--user", "is-active"]:
            out, rc = ("active\n", 0) if self.active else ("inactive\n", 3)
        elif cmd[0] == "systemd-run":
            self.active = True
        elif cmd[:3] == ["systemctl", "--user", "stop"]:
            self.active = False
        elif cmd[:3] == ["systemctl", "--user", "show"]:
            out = f"ActiveState={'active' if self.active else 'inactive'}\nMainPID={4242 if self.active else 0}\n"
        elif cmd[0] == "pgrep":
            out, rc = (self.pids, 0) if self.pids else ("", 1)
        return subprocess.CompletedProcess(cmd, rc, out, "")


class Swap:
    """Fake llama-swap: /running lists models; GET /unload empties it."""

    def __init__(self, running=None, up=True):
        self.running, self.up, self.gets = list(running or []), up, []

    def __call__(self, url):
        self.gets.append(url)
        if not self.up:
            raise OSError("connection refused")
        if url.endswith("/running"):
            return 200, json.dumps({"running": [{"model": m, "state": "ready"} for m in self.running]})
        if url.endswith("/unload"):
            self.running = []
            return 200, "OK"
        return 404, ""


def cfg():
    return config.load(REPO)


def test_start_refuses_when_ram_low(tmp_path):
    sysrun, swap, out = Sys(), Swap(), io.StringIO()
    rc = runtime.start(cfg(), tmp_path, run=sysrun, http_get=swap, meminfo=meminfo(tmp_path, 8), out=out)
    assert rc == 1
    assert "free RAM 8.0 GB < 12 GB" in out.getvalue()
    assert not any(c[0] == "systemd-run" for c in sysrun.calls)


def test_start_refuses_when_llama_swap_runs_a_foreign_model(tmp_path):
    sysrun, out = Sys(), io.StringIO()
    swap = Swap(["qwen3.6-35b-a3b", ROLES[1]])
    rc = runtime.start(cfg(), tmp_path, run=sysrun, http_get=swap, meminfo=meminfo(tmp_path, 30), out=out)
    assert rc == 1 and "qwen3.6-35b-a3b" in out.getvalue() and "evict" in out.getvalue()
    assert not any(c[0] == "systemd-run" for c in sysrun.calls)
    assert not any(u.endswith("/unload") for u in swap.gets)  # never touches someone else's model


def test_start_launches_systemd_run_unit(tmp_path):
    sysrun, swap, out = Sys(), Swap([ROLES[0]]), io.StringIO()
    rc = runtime.start(cfg(), tmp_path, run=sysrun, http_get=swap, meminfo=meminfo(tmp_path, 30), out=out,
                       python="/venv/bin/python", wait_s=0)
    assert rc == 0, out.getvalue()
    sr = next(c for c in sysrun.calls if c[0] == "systemd-run")
    assert sr[:3] == ["systemd-run", "--user", "--unit=deal-hunter"]
    assert "--property=KillMode=control-group" in sr and sr[-4:] == ["/venv/bin/python", "-m", "dh.runtime", "serve"]
    st = json.loads((tmp_path / "var" / "run" / "state.json").read_text())
    assert st["models_before"] == [ROLES[0]]
    # already running: no second unit
    rc2 = runtime.start(cfg(), tmp_path, run=sysrun, http_get=swap, meminfo=meminfo(tmp_path, 30), out=out, wait_s=0)
    assert rc2 == 0 and sum(c[0] == "systemd-run" for c in sysrun.calls) == 1


def test_stop_unloads_only_own_role_models_and_checks_processes(tmp_path):
    (tmp_path / "var").mkdir()
    st = Store(tmp_path / "var" / "dh.db")
    ledger.open_window(st, "kleinanzeigen", datetime(2026, 10, 29, 10, tzinfo=timezone.utc))
    st.close()
    (tmp_path / "var" / "run").mkdir()
    (tmp_path / "var" / "run" / "state.json").write_text(json.dumps({"models_before": []}))
    sysrun, swap, out = Sys(active=True), Swap(ROLES), io.StringIO()
    rc = runtime.stop(cfg(), tmp_path, run=sysrun, http_get=swap, out=out)
    assert rc == 0, out.getvalue()
    assert ["systemctl", "--user", "stop", "deal-hunter"] in sysrun.calls
    assert any(u.endswith("/unload") for u in swap.gets) and swap.running == []
    assert "nothing left running" in out.getvalue()
    st = Store(tmp_path / "var" / "dh.db")
    assert st.open_windows() == []


def test_stop_never_unloads_foreign_or_preexisting_models(tmp_path):
    out = io.StringIO()
    swap = Swap([ROLES[0], "someone-elses-model"])
    runtime.stop(cfg(), tmp_path, run=Sys(active=True), http_get=swap, out=out)
    assert not any(u.endswith("/unload") for u in swap.gets) and "someone-elses-model" in out.getvalue()
    (tmp_path / "var" / "run").mkdir(parents=True, exist_ok=True)
    (tmp_path / "var" / "run" / "state.json").write_text(json.dumps({"models_before": [ROLES[0]]}))
    swap2 = Swap([ROLES[0]])  # was already loaded before dh start: somebody else's session
    runtime.stop(cfg(), tmp_path, run=Sys(active=True), http_get=swap2, out=io.StringIO())
    assert not any(u.endswith("/unload") for u in swap2.gets)


def test_stop_reports_leftover_process(tmp_path):
    out = io.StringIO()
    rc = runtime.stop(cfg(), tmp_path, run=Sys(active=False, pids="777 python -m dh.runtime serve\n"),
                      http_get=Swap([]), out=out)
    assert rc == 1 and "777" in out.getvalue()


def test_status_json(tmp_path):
    (tmp_path / "var").mkdir()
    st = Store(tmp_path / "var" / "dh.db")
    st.log_round(datetime(2026, 10, 29, 10, tzinfo=timezone.utc), datetime(2026, 10, 29, 10, 5, tzinfo=timezone.utc),
                 None, 12, 0, json.dumps({"listings_seen": 12, "deals_by_tier": {}}))
    st.close()
    (tmp_path / "config" / "local").mkdir(parents=True)
    (tmp_path / "config" / "local" / "notify.yaml").write_text("ntfy: {topic: abcdefghijklmnopqrstuvwx}\n")
    s = runtime.status(cfg(), tmp_path, run=Sys(active=True), http_get=Swap([ROLES[0]]))
    assert s["running"] is True and s["pid"] == 4242 and s["rounds_total"] == 1
    assert s["last_round"]["listings_seen"] == 12 and s["ntfy_topic"] == "abcdefghijklmnopqrstuvwx"
    assert s["llama_swap"]["running"] == [ROLES[0]]
    json.dumps(s)  # serialisable for other agents


def test_serve_runs_rounds_until_stopped_and_closes_windows(tmp_path, monkeypatch):
    """serve(): recovery, rounds loop, web app, publisher; a stop event ends it gracefully with windows closed."""
    calls = []

    async def fake_round(ctx):
        calls.append(ctx.dry)
        ledger.observe(ctx.store, "kleinanzeigen", [], ctx.now(), arm=None)
        ledger.open_window(ctx.store, "kleinanzeigen", ctx.now())
        if len(calls) == 2:
            ctx.stop.set()
        from dh.scheduler import RoundStats
        return RoundStats(round_id=len(calls))

    async def fake_publish(store, cfg, root, stop):
        await stop.wait()

    warmed = []

    async def fake_warm(cfg, **kw):
        warmed.append(1)
        return {m: "ok" for m in ROLES}
    monkeypatch.setattr(runtime, "warm_models", fake_warm)  # never load real models from a test
    monkeypatch.setattr(runtime.scheduler, "run_round", fake_round)
    monkeypatch.setattr(runtime.publish, "run_loop", fake_publish)
    data = cfg().to_dict()
    data["scheduler"] = {**data.get("scheduler", {}), "round_interval_s": 0}
    data["web"] = {"host": "127.0.0.1", "port": 0}
    c = config.Config(data, REPO)
    stop = asyncio.Event()
    rc = asyncio.run(runtime.serve_async(c, tmp_path, stop=stop, install_signals=False, web=False))
    assert rc == 0 and calls == [False, False] and warmed == [1]
    st = Store(tmp_path / "var" / "dh.db")
    assert st.open_windows() == []


def test_warm_models_one_request_per_role_model_never_raises():
    seen = []

    async def post(url, body):
        seen.append(body["model"])
        if body["model"] == ROLES[1]:
            raise OSError("timeout")
        return 200
    out = asyncio.run(runtime.warm_models(cfg(), post=post))
    assert sorted(seen) == sorted(ROLES)  # advisor = senior: one request per distinct model
    assert out[ROLES[0]] == "ok" and out[ROLES[1]].startswith("OSError")


def test_start_refuses_while_a_foreground_round_runs(tmp_path):
    import os
    (tmp_path / "var" / "run").mkdir(parents=True)
    runtime.round_pid_path(tmp_path).write_text(str(os.getpid()))  # a live pid
    sysrun, out = Sys(), io.StringIO()
    rc = runtime.start(cfg(), tmp_path, run=sysrun, http_get=Swap([]), meminfo=meminfo(tmp_path, 30), out=out)
    assert rc == 1 and "foreground `dh round`" in out.getvalue()
    assert not any(c[0] == "systemd-run" for c in sysrun.calls)
    runtime.round_pid_path(tmp_path).write_text("999999999")  # stale pid file: ignored
    assert runtime.round_running(tmp_path) is None


def test_context_checks_llama_swap_for_foreign_models(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "llama_running", lambda c, http_get=None: ["qwen3.6-35b-a3b", ROLES[0]])
    st = Store(tmp_path / "t.db")
    ctx = runtime.build_context(cfg(), tmp_path, st, fetcher=None)
    assert asyncio.run(ctx.llm_check()) == ["qwen3.6-35b-a3b"]
    asyncio.run(ctx.llm.aclose())
