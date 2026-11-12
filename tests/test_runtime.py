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
    assert sr[:3] == ["systemd-run", "--user", "--unit=deal-hunter-test"]  # conftest: never the live unit's name
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
    assert ["systemctl", "--user", "stop", "deal-hunter-test"] in sysrun.calls
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


# ------------------------------------------------------------------ final review I1: pgrep -af -- PATTERN


FAKE_PGREP = r"""#!/usr/bin/env python3
# Fake pgrep: parses options like procps (a pattern starting with '-' is an option unless it follows '--') and
# prints the lines listed in $FAKE_PGREP_OUT, one "<pid> <cmdline>" per line.
import os, sys
args, pattern, opts_done = sys.argv[1:], None, False
for a in args:
    if not opts_done and a == "--":
        opts_done = True
    elif not opts_done and a.startswith("-"):
        if any(c not in "af" for c in a[1:]):
            sys.stderr.write(f"pgrep: invalid option -- '{a[1]}'\n")
            sys.exit(2)
    else:
        pattern = a
if pattern is None:
    sys.exit(2)
lines = [l for l in open(os.environ["FAKE_PGREP_OUT"]).read().splitlines() if pattern in l]
print("\n".join(lines))
sys.exit(0 if lines else 1)
"""


@pytest.fixture
def fake_pgrep(tmp_path, monkeypatch):
    """A fake `pgrep` FIRST on PATH (only pgrep: systemctl is never run for real from these tests)."""
    import os
    import stat
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    exe = bindir / "pgrep"
    exe.write_text(FAKE_PGREP)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    out = tmp_path / "pgrep.out"
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_PGREP_OUT", str(out))

    def lines(*extra):
        me, parent = os.getpid(), os.getppid()
        out.write_text("\n".join([f"{me} python -m pytest -k x -m dh.runtime serve",  # ourselves
                                  f"{parent} bash -c 'sleep 1; python -m dh.runtime serve'",  # an ancestor
                                  *extra]) + "\n")
    lines()
    return lines


def pgrep_only(sysfake):
    """subprocess.run stand-in: pgrep runs for real (the fake on PATH), everything else goes to the Sys fake."""
    def run(cmd, **kw):
        if cmd[0] == "pgrep":
            return subprocess.run(cmd, **kw)
        return sysfake(cmd, **kw)
    return run


def test_serve_processes_passes_the_pattern_after_double_dash_and_skips_itself(fake_pgrep):
    """I1: `pgrep -af "-m dh.runtime serve"` read the pattern as options (exit 2), so no process was ever seen."""
    assert runtime.serve_processes() == []  # only ourselves and our parent match
    fake_pgrep("4242 /venv/bin/python -m dh.runtime serve")
    assert runtime.serve_processes() == ["4242 /venv/bin/python -m dh.runtime serve"]


def test_stop_and_status_see_a_leftover_serve_through_real_pgrep(tmp_path, fake_pgrep):
    fake_pgrep("4242 /venv/bin/python -m dh.runtime serve")
    out = io.StringIO()
    rc = runtime.stop(cfg(), tmp_path, run=pgrep_only(Sys(active=False)), http_get=Swap([]), out=out)
    assert rc == 1 and "STILL RUNNING" in out.getvalue() and "4242" in out.getvalue()
    s = runtime.status(cfg(), tmp_path, run=pgrep_only(Sys(active=False)), http_get=Swap([]))
    assert s["processes"] == ["4242 /venv/bin/python -m dh.runtime serve"]
    fake_pgrep()
    out = io.StringIO()
    assert runtime.stop(cfg(), tmp_path, run=pgrep_only(Sys(active=False)), http_get=Swap([]), out=out) == 0
    assert "nothing left running" in out.getvalue()


def test_agent_running_sees_a_serve_outside_the_unit(tmp_path, fake_pgrep, monkeypatch):
    from dh.improve import agent_running
    monkeypatch.setattr(runtime, "is_active", lambda run=None: False)  # never ask the real systemd from a test
    assert agent_running(tmp_path) is False
    fake_pgrep("4242 /venv/bin/python -m dh.runtime serve")
    assert agent_running(tmp_path) is True


# ------------------------------------------------------------------ final review minor: frozen change pauses


def test_frozen_change_pauses_rounds_shows_in_status_and_pushes_once(tmp_path, monkeypatch):
    """A frozen file changed while running: the rounds pause (no round runs while it differs), `dh status` says
    "PAUSED: frozen file changed (<path>)", the owner gets ONE ntfy push (default priority); when the file is back
    the rounds resume."""
    from dh.config import FrozenChanged
    from dh.scheduler import RoundStats
    calls, pushes, seen_status = [], [], []

    async def fake_round(ctx):
        calls.append(1)
        if len(calls) == 1:
            raise FrozenChanged("frozen files changed: config/economics.yaml", ["config/economics.yaml"])
        ctx.stop.set()
        return RoundStats(round_id=len(calls))

    checks = {"n": 0}

    def changes(cfg_, frozen):
        checks["n"] += 1
        if checks["n"] == 2:
            seen_status.append(runtime.status(cfg(), tmp_path, run=Sys(active=True), http_get=Swap([])))
            assert len(calls) == 1  # paused: no round while the file differs
        return ["config/economics.yaml"] if checks["n"] < 4 else []

    async def fake_push(cfg_, root, title, body, **kw):
        pushes.append((title, body, kw))
        return True

    async def fake_publish(store, cfg_, root, stop):
        await stop.wait()

    async def fake_warm(cfg_, **kw):
        return {}
    monkeypatch.setattr(runtime, "warm_models", fake_warm)
    monkeypatch.setattr(runtime.scheduler, "run_round", fake_round)
    monkeypatch.setattr(runtime.publish, "run_loop", fake_publish)
    monkeypatch.setattr(runtime, "frozen_changes", changes)
    monkeypatch.setattr(runtime.notify, "push_owner", fake_push)
    data = cfg().to_dict()
    data["scheduler"] = {**data.get("scheduler", {}), "round_interval_s": 0}
    c = config.Config(data, REPO)
    rc = asyncio.run(runtime.serve_async(c, tmp_path, stop=asyncio.Event(), install_signals=False, web=False))
    assert rc == 0 and len(calls) == 2 and checks["n"] >= 4
    assert len(pushes) == 1
    title, body, kw = pushes[0]
    assert "PAUSED" in title and "config/economics.yaml" in body and kw.get("priority") == "default"
    p = seen_status[0]["paused"]
    assert p["paths"] == ["config/economics.yaml"] and "frozen file changed" in p["reason"]
    assert not runtime.paused_path(tmp_path).exists()  # resumed: the pause marker is gone
    st = Store(tmp_path / "var" / "dh.db")
    kinds = [r["kind"] for r in st.conn.execute("select kind from health where source='runtime'")]
    assert kinds.count("frozen_changed") == 1 and kinds.count("frozen_restored") == 1


def test_status_line_for_a_paused_agent(tmp_path, monkeypatch, capsys):
    from dh import cli
    monkeypatch.setenv("DH_ROOT", str(tmp_path))
    monkeypatch.setattr(runtime, "status", lambda c, r: {
        "running": True, "unit": "deal-hunter", "state": "active", "pid": 1, "uptime_s": 60, "processes": [],
        "paused": {"reason": "frozen file changed", "paths": ["config/economics.yaml", "dh/tiering.py"]},
        "health": {}, "llama_swap": {}, "log_repo": {}})
    monkeypatch.setattr(cli, "_ctx", lambda: (tmp_path, cfg()))
    assert cli.main(["status"]) == 0
    assert "PAUSED: frozen file changed (config/economics.yaml, dh/tiering.py)" in capsys.readouterr().out


def test_push_owner_uses_the_shared_header_builder_with_default_priority(tmp_path):
    import httpx
    from dh import notify
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "notify.yaml").write_text(
        "ntfy: {server: 'https://ntfy.test', topic: null, tier1_priority: urgent}\nemail_fallback: {enabled: false}\n")
    seen = []

    def h(req):
        seen.append(dict(req.headers))
        return httpx.Response(200)
    ok = asyncio.run(notify.push_owner(config.load(tmp_path), tmp_path, "Deal Hunter PAUSED", "body",
                                       priority="default", client=httpx.AsyncClient(transport=httpx.MockTransport(h))))
    assert ok and seen[0]["priority"] == "default" and "actions" not in seen[0] and seen[0]["title"] == "Deal Hunter PAUSED"


# ------------------------------------------------------------------ unit name of a second checkout (dev clone)


def _root_with_runtime_yaml(tmp_path, body: str | None, local: bool = True) -> Path:
    root = tmp_path / "checkout"
    (root / "config" / "local").mkdir(parents=True)
    if body is not None:
        (root / "config" / ("local" if local else "") / "runtime.yaml").write_text(body)
    return root


def test_unit_name_default_config_and_env(tmp_path, monkeypatch):
    monkeypatch.delenv("DH_UNIT_NAME")  # conftest sets a dummy name for every test
    plain = _root_with_runtime_yaml(tmp_path / "a", None)
    assert runtime.unit_name(plain) == "deal-hunter"
    dev = _root_with_runtime_yaml(tmp_path / "b", "unit_name: deal-hunter-dev\n")
    assert runtime.unit_name(dev) == "deal-hunter-dev"
    shipped = _root_with_runtime_yaml(tmp_path / "c", "unit_name: dh-other.service\n", local=False)
    assert runtime.unit_name(shipped) == "dh-other"  # a ".service" suffix is accepted and dropped
    monkeypatch.setenv("DH_ROOT", str(dev))
    assert runtime.unit_name() == "deal-hunter-dev"  # the project root is DH_ROOT by default
    monkeypatch.setenv("DH_UNIT_NAME", "dh-env")
    assert runtime.unit_name(dev) == "dh-env"  # the environment wins over the config
    for bad in ("deal hunter", "-x", "a;rm", "../x"):
        monkeypatch.setenv("DH_UNIT_NAME", bad)
        with pytest.raises(ValueError, match="invalid unit name"):
            runtime.unit_name(dev)


def test_is_active_asks_only_about_this_checkouts_unit(monkeypatch):
    """The owner's unit `deal-hunter` is active; a dev clone named `deal-hunter-dev` is not refused because of it."""
    asked = []

    def run(cmd, **kw):
        asked.append(cmd[-1])
        on = cmd[-1] == "deal-hunter"
        return subprocess.CompletedProcess(cmd, 0 if on else 3, "active\n" if on else "inactive\n", "")
    monkeypatch.delenv("DH_UNIT_NAME")  # the shipped default, as on the owner's checkout
    assert runtime.is_active(run) is True
    monkeypatch.setenv("DH_UNIT_NAME", "deal-hunter-dev")
    assert runtime.is_active(run) is False
    assert asked == ["deal-hunter", "deal-hunter-dev"]


def test_start_stop_and_status_use_the_configured_unit(tmp_path, monkeypatch):
    monkeypatch.setenv("DH_UNIT_NAME", "deal-hunter-dev")
    sysrun, swap, out = Sys(), Swap([ROLES[0]]), io.StringIO()
    assert runtime.start(cfg(), tmp_path, run=sysrun, http_get=swap, meminfo=meminfo(tmp_path, 30), out=out,
                         python="/venv/bin/python", wait_s=0) == 0
    sr = next(c for c in sysrun.calls if c[0] == "systemd-run")
    assert sr[:3] == ["systemd-run", "--user", "--unit=deal-hunter-dev"]
    assert "--setenv=DH_UNIT_NAME=deal-hunter-dev" in sr  # serve and its children see the same name
    assert ["systemctl", "--user", "reset-failed", "deal-hunter-dev"] in sysrun.calls
    assert runtime.status(cfg(), tmp_path, run=sysrun, http_get=swap)["unit"] == "deal-hunter-dev"
    runtime.stop(cfg(), tmp_path, run=sysrun, http_get=swap, out=out)
    assert ["systemctl", "--user", "stop", "deal-hunter-dev"] in sysrun.calls
    named = [c for c in sysrun.calls if c[0] == "systemctl" and c[2] != "show"]
    assert named and all(c[-1] == "deal-hunter-dev" for c in named), named
    assert not any("deal-hunter" == c[-1] for c in sysrun.calls)


def test_serve_processes_ignores_another_checkouts_serve(tmp_path, fake_pgrep, monkeypatch):
    """pgrep sees every `-m dh.runtime serve` on the host; only this checkout's (or an unreadable one) counts."""
    here = tmp_path / "dev"
    here.mkdir()
    monkeypatch.setenv("DH_ROOT", str(here))
    roots = {4242: tmp_path / "owner", 4343: here, 4444: None}
    monkeypatch.setattr(runtime, "_proc_root", lambda pid: None if roots[pid] is None else roots[pid].resolve())
    fake_pgrep("4242 /owner/.venv/bin/python -m dh.runtime serve", "4343 /dev/.venv/bin/python -m dh.runtime serve",
               "4444 /x/python -m dh.runtime serve")
    assert runtime.serve_processes() == ["4343 /dev/.venv/bin/python -m dh.runtime serve",
                                         "4444 /x/python -m dh.runtime serve"]


@pytest.mark.skipif(not Path("/proc/self/environ").exists(), reason="needs /proc (Linux)")
def test_proc_root_reads_dh_root_then_the_working_directory(tmp_path):
    import os
    import sys
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    code = "import time; time.sleep(30)"
    with_env = subprocess.Popen([sys.executable, "-c", code], env={**os.environ, "DH_ROOT": str(a)}, cwd=str(b))
    env = {k: v for k, v in os.environ.items() if k != "DH_ROOT"}
    without = subprocess.Popen([sys.executable, "-c", code], env=env, cwd=str(b))
    try:
        assert runtime._proc_root(with_env.pid) == a.resolve()
        assert runtime._proc_root(without.pid) == b.resolve()
    finally:
        for p in (with_env, without):
            p.kill()
            p.wait()
    assert runtime._proc_root(with_env.pid) is None  # gone


def test_repository_config_in_tests_ignores_config_local(tmp_path):
    """conftest: the suite is hermetic against an owner's config/local/ in the repository (a role mapping, a unit
    name); a tmp root still merges its own local overrides."""
    root = tmp_path / "r"
    (root / "config" / "local").mkdir(parents=True)
    (root / "config" / "models.yaml").write_text("roles: {worker: a}\n")
    (root / "config" / "local" / "models.yaml").write_text("roles: {worker: b}\n")
    assert config.load(root).models.roles.worker == "b"
    assert config.load(root, local=False).models.roles.worker == "a"
    assert config.load(REPO).to_dict() == config.load(REPO, local=False).to_dict()


# ------------------------------------------------------------------ validation F10: the improvement step's LLM


def test_post_round_runs_the_improvement_step_without_the_llm_while_a_foreign_model_is_loaded(tmp_path, monkeypatch):
    """An advisor call after the round must not swap out a model another workload loaded meanwhile."""
    from dh import improve
    from dh.llm import LLMUnavailable
    from dh.store import Store
    results, checks = [], []

    async def fake_after_round(ctx, st):
        try:
            results.append(await ctx.llm.chat_json("advisor", "s", "u", {}))
        except LLMUnavailable as e:
            results.append(str(e))
    monkeypatch.setattr(improve, "after_round", fake_after_round)
    foreign = [["gemma-4-26b-a4b"]]

    async def running():
        checks.append(1)
        return foreign[0]

    class Model:
        def __init__(self):
            self.calls = 0

        async def chat_json(self, *a, **kw):
            self.calls += 1
            return {"ok": True}
    st = Store(tmp_path / "t.db")
    llm = Model()
    ctx = type("Ctx", (), {})()
    ctx.cfg, ctx.store, ctx.root, ctx.llm, ctx.llm_check = cfg(), st, tmp_path, llm, running
    asyncio.run(runtime.post_round(ctx, None, weekly=False))
    foreign[0] = []
    asyncio.run(runtime.post_round(ctx, None, weekly=False))
    assert "outside the roles" in results[0] and results[1] == {"ok": True} and llm.calls == 1
    assert ctx.llm is llm  # the caller's context is never changed
    assert st.conn.execute("select count(*) from health where source='improve' and kind='foreign_model'").fetchone()[0] == 1


def test_post_round_makes_no_llama_swap_request_when_the_step_makes_no_llm_call(tmp_path, monkeypatch):
    from dh import improve
    from dh.store import Store
    checks = []

    async def fake_after_round(ctx, st):
        return None

    async def running():
        checks.append(1)
        return []
    monkeypatch.setattr(improve, "after_round", fake_after_round)
    ctx = type("Ctx", (), {})()
    ctx.cfg, ctx.store, ctx.root, ctx.llm, ctx.llm_check = cfg(), Store(tmp_path / "t.db"), tmp_path, object(), running
    asyncio.run(runtime.post_round(ctx, None, weekly=False))
    assert checks == []


def test_the_suite_never_uses_the_live_unit_name():
    assert runtime.unit_name() == "deal-hunter-test"
