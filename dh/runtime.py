"""Runtime: `dh start` / `dh stop` / `dh status` and the `serve` process (spec §4, §15a item 1).

The owner starts and stops the agent by hand. There is no autostart of any kind (no enabled unit, timer or cron):
`dh start` launches a transient systemd user unit,
    systemd-run --user --unit=deal-hunter --collect --property=KillMode=control-group <venv python> -m dh.runtime serve
which disappears when it stops. KillMode=control-group means `systemctl --user stop` kills every process the agent
started (headless Chrome included).

Guards (`dh start` and a foreground `dh round` refuse and say why, they never evict another workload):
- free RAM (MemAvailable in /proc/meminfo) >= models.min_free_ram_gb (12 GB);
- llama-swap `/running` lists no model outside the configured roles (models.roles). llama-swap unreachable is not a
  refusal (the round runs on rules alone and the LLM breaker opens), but it is reported.

`dh stop`: stops the unit, closes every observation window (also done by `serve` itself on SIGTERM), then releases
the models: `GET /unload` (POST answers 405 on this llama-swap build) ONLY when `/running` shows nothing but role
models AND none of them was already loaded before `dh start` (recorded in var/run/state.json): `/unload` unloads
everything, so a model somebody else was using is never touched. Finally it checks that nothing is left
(`systemctl --user is-active` and `pgrep -f "dh.runtime serve"`).

`serve` = rounds loop (pacing `scheduler.round_interval_s`, start to start) + the web page (uvicorn on web.host:port,
default 0.0.0.0:8711) + the log publisher loop, in one asyncio process. Startup recovery closes windows a crash left
open. SIGTERM/SIGINT: the current round is cancelled, the web server and publisher stop, windows are closed, exit 0.
A changed frozen file stops the rounds (health event `frozen_changed`), as dh/config.py requires.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import random
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import config as dhconfig
from . import ledger, notify, publish, scheduler
from .bandit import Bandit
from .config import FrozenChanged
from .store import Store

log = logging.getLogger("dh.runtime")

UNIT = "deal-hunter"
SERVE_PATTERN = "-m dh.runtime serve"
ROOT = Path(__file__).resolve().parents[1]


def root_dir() -> Path:
    return Path(os.environ.get("DH_ROOT") or ROOT)


def db_path(root: Path) -> Path:
    return Path(root) / "var" / "dh.db"


def state_path(root: Path) -> Path:
    return Path(root) / "var" / "run" / "state.json"


def round_pid_path(root: Path) -> Path:
    return Path(root) / "var" / "run" / "round.pid"


def round_running(root: Path) -> int | None:
    """pid of a foreground `dh round` that is still alive, else None (a stale pid file is ignored)."""
    try:
        pid = int(round_pid_path(root).read_text().strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        return pid
    return pid


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------------------------------------ probes


def default_http_get(url: str) -> tuple[int, str]:
    import httpx
    r = httpx.get(url, timeout=10)
    return r.status_code, r.text


def mem_available_gb(meminfo: str | Path = "/proc/meminfo") -> float:
    for line in Path(meminfo).read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) / (1024 * 1024)
    raise RuntimeError(f"no MemAvailable in {meminfo}")


def swap_url(cfg) -> str:
    m = cfg.models.to_dict()
    url = m.get("llama_swap_url") or str(m.get("base_url", "http://localhost:8080/v1")).rsplit("/v1", 1)[0]
    return str(url).rstrip("/")


def role_models(cfg) -> set[str]:
    return {str(v) for v in cfg.models.roles.to_dict().values() if v}


def llama_running(cfg, http_get=default_http_get) -> list[str] | None:
    """Model names llama-swap has loaded, or None when it cannot be reached."""
    try:
        status, text = http_get(f"{swap_url(cfg)}/running")
        if status != 200:
            return None
        return [str(r.get("model")) for r in json.loads(text).get("running", []) if r.get("model")]
    except Exception:  # noqa: BLE001 - unreachable / malformed: unknown
        return None


def preflight(cfg, *, http_get=default_http_get, meminfo: str | Path = "/proc/meminfo",
              need_llm: bool = True) -> tuple[list[str], list[str] | None, list[str]]:
    """(refusal reasons, models running before start or None, warnings)."""
    reasons, warnings = [], []
    need = float(cfg.models.get("min_free_ram_gb", 12))
    try:
        free = mem_available_gb(meminfo)
        if free < need:
            reasons.append(f"free RAM {free:.1f} GB < {need:g} GB (models.min_free_ram_gb): another workload is "
                           f"using the rig; not starting")
    except (OSError, RuntimeError, ValueError) as e:
        reasons.append(f"cannot read free RAM: {e}")
    running = llama_running(cfg, http_get) if need_llm else None
    if need_llm:
        if running is None:
            warnings.append(f"llama-swap at {swap_url(cfg)} is not reachable: rounds run on the rules matcher only")
        else:
            foreign = [m for m in running if m not in role_models(cfg)]
            if foreign:
                reasons.append("llama-swap is running model(s) outside the configured roles: " + ", ".join(foreign)
                               + ". Not starting: loading the role models would evict another workload.")
    return reasons, running, warnings


def _sys(run, *cmd: str, timeout: float = 60) -> subprocess.CompletedProcess:
    return run(list(cmd), capture_output=True, text=True, timeout=timeout)


def is_active(run=subprocess.run) -> bool:
    try:
        return _sys(run, "systemctl", "--user", "is-active", UNIT).stdout.strip() in ("active", "activating", "reloading")
    except (OSError, subprocess.SubprocessError):
        return False


def serve_processes(run=subprocess.run) -> list[str]:
    try:
        r = _sys(run, "pgrep", "-af", SERVE_PATTERN)
    except (OSError, subprocess.SubprocessError):
        return []
    me = str(os.getpid())
    return [ln for ln in r.stdout.splitlines() if ln.strip() and ln.split()[0] != me]


def release_models(cfg, before: list[str] | None, *, http_get=default_http_get, out=sys.stdout) -> bool:
    """GET /unload only when llama-swap runs nothing but role models and none of them predates our start."""
    running = llama_running(cfg, http_get)
    if running is None:
        print("llama-swap not reachable: nothing to unload", file=out)
        return False
    if not running:
        print("llama-swap: no model loaded", file=out)
        return False
    roles = role_models(cfg)
    foreign = [m for m in running if m not in roles]
    if foreign:
        print("llama-swap: not unloading, other model(s) loaded: " + ", ".join(foreign), file=out)
        return False
    pre = [m for m in running if m in set(before or [])]
    if pre:
        print("llama-swap: not unloading " + ", ".join(pre) + " (already loaded before dh start: another user)",
              file=out)
        return False
    try:
        status, _ = http_get(f"{swap_url(cfg)}/unload")
    except Exception as e:  # noqa: BLE001
        print(f"llama-swap: unload failed: {e}", file=out)
        return False
    after = llama_running(cfg, http_get)
    print(f"llama-swap: unloaded {', '.join(running)} (HTTP {status}); running now: {after}", file=out)
    return status == 200


async def warm_models(cfg, *, timeout_s: float = 300.0, post=None) -> dict[str, str]:
    """Load the role models before the first round: one 1-token request per distinct model, with a long timeout
    (a cold llama-swap load takes minutes; the round's LLM client would time out at 60 s and open its breaker).
    Returns {model: "ok" | error}. Never raises."""
    import httpx
    base = str(cfg.models.base_url).rstrip("/")
    out: dict[str, str] = {}
    for model in sorted(role_models(cfg)):
        body = {"model": model, "max_tokens": 1, "messages": [{"role": "user", "content": "ok"}],
                "chat_template_kwargs": {"enable_thinking": False}}
        try:
            if post is not None:
                status = await post(f"{base}/chat/completions", body)
            else:
                async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_s, connect=5)) as c:
                    status = (await c.post(f"{base}/chat/completions", json=body)).status_code
            out[model] = "ok" if status == 200 else f"HTTP {status}"
        except Exception as e:  # noqa: BLE001
            out[model] = f"{type(e).__name__}: {e}"[:200]
    return out


# ------------------------------------------------------------------------------------------------ start / stop / status


def start(cfg, root: Path, *, run=subprocess.run, http_get=default_http_get, meminfo: str | Path = "/proc/meminfo",
          out=sys.stdout, python: str | None = None, wait_s: float = 3.0) -> int:
    root = Path(root)
    if is_active(run):
        print(f"deal-hunter is already running (systemctl --user status {UNIT})", file=out)
        return 0
    rp = round_running(root)
    if rp is not None:
        print(f"dh start refused: a foreground `dh round` is running (pid {rp}); let it finish or stop it first",
              file=out)
        return 1
    reasons, running, warnings = preflight(cfg, http_get=http_get, meminfo=meminfo)
    for w in warnings:
        print(f"warning: {w}", file=out)
    if reasons:
        print("dh start refused:", file=out)
        for r in reasons:
            print(f"  - {r}", file=out)
        return 1
    state_path(root).parent.mkdir(parents=True, exist_ok=True)
    state_path(root).write_text(json.dumps({"started_at": _now().isoformat(), "models_before": running or []}))
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        _sys(run, "systemctl", "--user", "reset-failed", UNIT)
    cmd = ["systemd-run", "--user", f"--unit={UNIT}", "--collect", "--property=KillMode=control-group",
           f"--working-directory={root}", f"--setenv=DH_ROOT={root}", "--setenv=PYTHONUNBUFFERED=1",
           python or sys.executable, "-m", "dh.runtime", "serve"]
    r = _sys(run, *cmd)
    if r.returncode != 0:
        print(f"systemd-run failed ({r.returncode}): {r.stderr.strip()}", file=out)
        return 1
    if wait_s:
        time.sleep(wait_s)
    if not is_active(run):
        print(f"deal-hunter did not stay up: see journalctl --user -u {UNIT}", file=out)
        return 1
    web = cfg.get("web")
    port = (web.to_dict() if web is not None else {}).get("port", 8711)
    print(f"deal-hunter started (unit {UNIT}); web page on port {port}; stop with `dh stop`", file=out)
    return 0


def stop(cfg, root: Path, *, run=subprocess.run, http_get=default_http_get, out=sys.stdout) -> int:
    root = Path(root)
    was = is_active(run)
    try:
        r = _sys(run, "systemctl", "--user", "stop", UNIT, timeout=150)
        print(f"systemctl --user stop {UNIT}: " + ("stopped" if was else "was not running")
              + (f" ({r.stderr.strip()})" if r.returncode and r.stderr.strip() else ""), file=out)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"systemctl stop failed: {e}", file=out)
    if db_path(root).is_file():  # serve closes them on SIGTERM; this covers a crash
        st = Store(db_path(root))
        try:
            n = ledger.close_all_windows(st, _now())
            print(f"observation windows closed: {n}", file=out)
        finally:
            st.close()
    before = None
    with contextlib.suppress(OSError, ValueError):
        before = json.loads(state_path(root).read_text()).get("models_before")
    release_models(cfg, before, http_get=http_get, out=out)
    left = is_active(run)
    procs = serve_processes(run)
    if left or procs:
        print("STILL RUNNING: " + ("unit active; " if left else "") + "; ".join(procs), file=out)
        return 1
    print("nothing left running", file=out)
    return 0


def _show(run) -> dict:
    try:
        r = _sys(run, "systemctl", "--user", "show", UNIT, "-p", "ActiveState", "-p", "MainPID", "-p",
                 "ActiveEnterTimestamp")
    except (OSError, subprocess.SubprocessError):
        return {}
    return dict(ln.split("=", 1) for ln in r.stdout.splitlines() if "=" in ln)


def status(cfg, root: Path, *, run=subprocess.run, http_get=default_http_get) -> dict:
    root = Path(root)
    show = _show(run)
    active = is_active(run)
    out: dict[str, Any] = {"running": active, "unit": UNIT, "state": show.get("ActiveState"),
                           "pid": int(show.get("MainPID") or 0) or None, "since": show.get("ActiveEnterTimestamp"),
                           "uptime_s": None, "processes": serve_processes(run)}
    try:
        stt = json.loads(state_path(root).read_text())
        out["started_at"] = stt.get("started_at")
        if active and stt.get("started_at"):
            out["uptime_s"] = round((_now() - datetime.fromisoformat(stt["started_at"])).total_seconds())
    except (OSError, ValueError):
        out["started_at"] = None
    out["llama_swap"] = {"url": swap_url(cfg), "running": llama_running(cfg, http_get), "roles": sorted(role_models(cfg))}
    out["ntfy_topic"] = notify.topic(root)
    if db_path(root).is_file():
        st = Store(db_path(root))
        try:
            out.update(_db_status(st, out.get("started_at")))
        finally:
            st.close()
    else:
        out.update({"rounds_total": 0, "rounds_since_start": 0, "watched_hours": 0.0, "last_round": None,
                    "deals_today": {}, "health": {"blocked": [], "disabled": []}, "llm_today": {"tokens": 0}})
    out["log_repo"] = _log_repo(cfg)
    return out


def _db_status(st: Store, started_at: str | None) -> dict:
    day = _now().date().isoformat()
    rounds_total = st.conn.execute("select count(*) from rounds").fetchone()[0]
    since = st.conn.execute("select count(*) from rounds where started_at >= ?", (started_at or "9999",)).fetchone()[0]
    last = st.conn.execute("select * from rounds order by id desc limit 1").fetchone()
    last_round = None
    if last is not None:
        try:
            n = json.loads(last["notes"] or "{}")
        except ValueError:
            n = {}
        keep = ("round_id", "seconds", "arms", "sentinel_arms", "feed_arms", "budget", "sources", "listings_seen",
                "processed", "candidates", "llm_items", "llm_calls", "llm_tokens", "llm_deferred", "deals_by_tier",
                "deals", "duplicates", "dropped", "disappeared", "alive_cleared", "energy_pln", "errors")
        last_round = {"id": last["id"], "started_at": last["started_at"], "ended_at": last["ended_at"],
                      **{k: n.get(k) for k in keep if k in n}}
        if "listings_seen" not in last_round:
            last_round["listings_seen"] = last["listings_seen"]
        if isinstance(last_round.get("errors"), list):
            last_round["errors"] = last_round["errors"][:10]
    tiers = {str(r["tier"]): r["n"] for r in st.conn.execute(
        "select tier, count(*) n from deals where substr(created_at,1,10)=? group by tier order by tier", (day,))}
    blocked = [r["source"] for r in st.conn.execute(
        "select distinct source from health where kind='blocked' and at >= datetime('now','-1 day')")]
    disabled = [r["source"] for r in st.conn.execute(
        "select distinct source from health where kind in ('disabled','no_keys','no_credentials','not_granted') "
        "and at >= datetime('now','-1 day')")]
    tok = st.conn.execute("select coalesce(sum(c.units),0) from costs c join rounds r on r.id=c.round_id "
                          "where c.kind='llm' and substr(r.started_at,1,10)=?", (day,)).fetchone()[0]
    energy = st.conn.execute("select coalesce(sum(cast(c.amount_pln as real)),0) from costs c join rounds r "
                             "on r.id=c.round_id where substr(r.started_at,1,10)=?", (day,)).fetchone()[0]
    try:
        watched = round(ledger.watched_hours(st), 2)
    except Exception:  # noqa: BLE001
        watched = None
    return {"rounds_total": rounds_total, "rounds_since_start": since, "watched_hours": watched,
            "last_round": last_round, "deals_today": tiers, "health": {"blocked": blocked, "disabled": disabled},
            "llm_today": {"tokens": int(tok)}, "cost_today_pln": round(float(energy), 2)}


def _log_repo(cfg) -> dict:
    try:
        pc = cfg.publish.to_dict()
    except AttributeError:
        return {"configured": False}
    d = Path(str(pc.get("log_dir", ""))).expanduser()
    info: dict[str, Any] = {"log_dir": str(d), "remote": pc.get("remote"), "clone": (d / ".git").is_dir()}
    if info["clone"]:
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            r = subprocess.run(["git", "-C", str(d), "log", "-1", "--format=%h %cI %s"], capture_output=True,
                               text=True, timeout=10)
            info["last_commit"] = r.stdout.strip() or None
    return info


# ------------------------------------------------------------------------------------------------ serve


async def post_round(ctx, st, *, weekly: bool = True) -> None:
    """Hooks that run after every round (`serve` and `dh round`): the self-improvement step, then (serve only) the
    weekly report when 168 watched hours have passed. Neither can end the round loop: a failure becomes a health
    event (`improve_error`, `weekly_error`); only cancellation (SIGTERM) propagates."""
    from . import improve
    try:
        imp = ctx.cfg.get("improve")
        limit = float((imp.to_dict() if imp is not None else {}).get("post_round_timeout_s", 300))
        await asyncio.wait_for(improve.after_round(ctx, st), limit if limit > 0 else None)
    except asyncio.TimeoutError:
        log.error("improve.after_round exceeded %.0f s", limit)
        ctx.store.add_health("improve", "improve_timeout", f"after_round cut after {limit:.0f}s (improve.post_round_timeout_s)")
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 - the improvement loop is optional; the rounds are not
        log.exception("improve.after_round failed")
        ctx.store.add_health("improve", "improve_error", f"{type(e).__name__}: {e}"[:500])
    if weekly:
        try:
            await publish.publish_weekly(ctx.store, ctx.cfg, ctx.root)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("weekly report failed")
            ctx.store.add_health("publish", "weekly_error", f"{type(e).__name__}: {e}"[:500])


def build_context(cfg, root: Path, store: Store, fetcher, *, dry: bool = False, no_llm: bool = False,
                  stop: asyncio.Event | None = None, frozen: dict | None = None) -> scheduler.RoundContext:
    """Everything a round needs, wired to the real modules (shared by `serve` and `dh round`)."""
    from .customs import Customs
    from .money import Fx
    from .sources import registry
    from .sources.ebay_insights import EbayInsights

    root = Path(root)
    try:
        cards_dir = Path(str(cfg.publish.cards_dir))
    except AttributeError:
        cards_dir = Path("var/cards/deals")
    cards_dir = cards_dir if cards_dir.is_absolute() else root / cards_dir
    ctx = scheduler.RoundContext(
        cfg=cfg, root=root, store=store, fetcher=fetcher, adapters=registry(cfg, root), llm=None, fx=Fx(store),
        customs=Customs(store), bandit=Bandit(store), rng=random.Random(), cards_root=cards_dir.parent,
        shots_root=root / "var" / "screenshots", dry=dry, stop=stop, frozen_digest=frozen,
        insights=EbayInsights(store=store))
    if not no_llm:
        from .llm import Client
        ctx.llm = Client(cfg, on_usage=ctx.on_usage)

        async def foreign_models() -> list[str]:
            running = await asyncio.to_thread(llama_running, cfg)
            return [] if running is None else [m for m in running if m not in role_models(cfg)]
        ctx.llm_check = foreign_models
    return ctx


class _QuietServer:
    """uvicorn.Server without its own signal handling: serve() owns SIGTERM."""

    def __new__(cls, config):
        import uvicorn

        class S(uvicorn.Server):
            def install_signal_handlers(self) -> None:  # older uvicorn
                pass

            @contextlib.contextmanager
            def capture_signals(self):  # uvicorn >= 0.29
                yield

        return S(config)


async def serve_async(cfg, root: Path, *, stop: asyncio.Event | None = None, install_signals: bool = True,
                      web: bool = True) -> int:
    from .fetch import Fetcher

    root = Path(root)
    stop = stop or asyncio.Event()
    if install_signals:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
    db_path(root).parent.mkdir(parents=True, exist_ok=True)
    frozen = dhconfig.frozen_digest(cfg._root) if getattr(cfg, "_root", None) else None
    store = Store(db_path(root))
    n = ledger.close_all_windows(store, _now())  # recovery: a crash may have left windows open
    if n:
        store.add_health("runtime", "recovery", f"closed {n} window(s) left open by a previous run")
    fetcher = Fetcher(cfg, store, root / "var")
    ctx = build_context(cfg, root, store, fetcher, stop=stop, frozen=frozen)
    if ctx.llm is not None:
        warm = await warm_models(cfg)
        log.info("models warmed: %s", warm)
        if any(v != "ok" for v in warm.values()):
            store.add_health("runtime", "model_warmup", json.dumps(warm)[:500])
    sc = scheduler.sched_cfg(cfg)
    tasks: list[asyncio.Task] = []
    server = None
    if web:
        import uvicorn

        from .web.app import create_app
        w = cfg.get("web")
        w = w.to_dict() if w is not None else {}
        server = _QuietServer(uvicorn.Config(create_app(db_path(root), cfg, root), host=str(w.get("host", "0.0.0.0")),
                                             port=int(w.get("port", 8711)), log_level="warning", lifespan="off"))
        tasks.append(asyncio.create_task(server.serve(), name="web"))
    tasks.append(asyncio.create_task(publish.run_loop(store, cfg, root, stop), name="publish"))

    async def rounds() -> None:
        while not stop.is_set():
            t0 = time.monotonic()
            try:
                st = await scheduler.run_round(ctx)
                log.info(scheduler.summary_line(st))
                await post_round(ctx, st)
            except asyncio.CancelledError:
                raise
            except FrozenChanged as e:
                store.add_health("runtime", "frozen_changed", str(e)[:500])
                log.error("frozen layer changed, rounds stopped: %s", e)
                return
            except Exception as e:  # noqa: BLE001 - a broken round is logged; the next one runs
                log.exception("round failed")
                store.add_health("runtime", "round_failed", f"{type(e).__name__}: {e}"[:500])
            wait = max(0.0, float(sc["round_interval_s"]) - (time.monotonic() - t0))
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), wait)

    rt = asyncio.create_task(rounds(), name="rounds")
    try:
        await stop.wait()
    finally:
        rt.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await rt
        if server is not None:
            server.should_exit = True
        stop.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        n = ledger.close_all_windows(store, _now())
        log.info("stopped; %d observation window(s) closed", n)
        with contextlib.suppress(Exception):
            await fetcher.aclose()
        if ctx.llm is not None:
            with contextlib.suppress(Exception):
                await ctx.llm.aclose()
        store.close()
    return 0


def serve(root: Path | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = Path(root or root_dir())
    return asyncio.run(serve_async(dhconfig.load(root), root))


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["serve"]:
        return serve()
    print("usage: python -m dh.runtime serve   (use `dh start` instead)", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
