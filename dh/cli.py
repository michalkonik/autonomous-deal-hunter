"""dh command line. JSON output (`--json`) is for other agents; the project root is $DH_ROOT or the repo."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import signal
import sys
from datetime import datetime, timezone
from decimal import Decimal

STUBS = ["replay", "eval", "playbook"]  # later milestones
COMMANDS = ["start", "stop", "status", "deals", "show", "verify", "replay", "eval", "playbook", "books", "round"]


def _stub(args: argparse.Namespace) -> int:
    print("not implemented yet")
    return 0


def _ctx():
    from . import config, runtime
    root = runtime.root_dir()
    return root, config.load(root)


def _store(root, must_exist: bool = True):
    from . import runtime
    from .store import Store
    p = runtime.db_path(root)
    if must_exist and not p.is_file():
        print(f"no database yet at {p}: run `dh round --once` or `dh start` first", file=sys.stderr)
        return None
    p.parent.mkdir(parents=True, exist_ok=True)
    return Store(p)


def _dump(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


# ------------------------------------------------------------------------------------------------ start / stop / status


def cmd_start(args) -> int:
    from . import runtime
    root, cfg = _ctx()
    return runtime.start(cfg, root)


def cmd_stop(args) -> int:
    from . import runtime
    root, cfg = _ctx()
    return runtime.stop(cfg, root)


def cmd_status(args) -> int:
    from . import runtime
    root, cfg = _ctx()
    s = runtime.status(cfg, root)
    if args.json:
        _dump(s)
        return 0
    up = f", up {s['uptime_s'] // 60} min" if s.get("uptime_s") else ""
    print(f"deal-hunter: {'RUNNING' if s['running'] else 'stopped'} (unit {s['unit']}, state {s['state']}"
          f"{', pid ' + str(s['pid']) if s.get('pid') else ''}{up})")
    if s.get("processes"):
        print("  processes: " + "; ".join(s["processes"]))
    print(f"  rounds: {s.get('rounds_total', 0)} total, {s.get('rounds_since_start', 0)} since start; "
          f"watched hours {s.get('watched_hours')}")
    lr = s.get("last_round")
    if lr:
        print(f"  last round #{lr['id']} {lr.get('started_at')}: {lr.get('seconds')} s, listings {lr.get('listings_seen')}, "
              f"candidates {lr.get('candidates')}, LLM calls {lr.get('llm_calls')}, deals {lr.get('deals_by_tier')}")
    print(f"  deals today by tier: {s.get('deals_today') or '{}'}; LLM tokens today {s.get('llm_today', {}).get('tokens')}; "
          f"cost today {s.get('cost_today_pln', 0)} PLN")
    h = s.get("health") or {}
    print(f"  health (24 h): blocked {h.get('blocked') or '-'}; disabled {h.get('disabled') or '-'}")
    ls = s.get("llama_swap") or {}
    print(f"  llama-swap: running {ls.get('running')}; roles {ls.get('roles')}")
    print(f"  ntfy topic: {s.get('ntfy_topic')}")
    lr = s.get("log_repo") or {}
    print(f"  log repo: {lr.get('log_dir')} clone={lr.get('clone')} last={lr.get('last_commit')} remote={lr.get('remote')}")
    return 0


# ------------------------------------------------------------------------------------------------ round


async def _round(args) -> int:
    from . import config, ledger, runtime, scheduler
    from .fetch import Fetcher
    root, cfg = _ctx()
    if runtime.is_active():
        print("refused: the deal-hunter service is running (dh status); stop it before a foreground round")
        return 1
    reasons, before, warnings = runtime.preflight(cfg, need_llm=not args.no_llm)
    for w in warnings:
        print(f"warning: {w}")
    if reasons:
        print("dh round refused:")
        for r in reasons:
            print(f"  - {r}")
        return 1
    store = _store(root, must_exist=False)
    ledger.close_all_windows(store, datetime.now(timezone.utc))  # recovery
    fetcher = Fetcher(cfg, store, root / "var")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop.set)
    ctx = runtime.build_context(cfg, root, store, fetcher, dry=args.dry, no_llm=args.no_llm, stop=stop,
                                frozen=config.frozen_digest(root))
    if ctx.llm is not None:
        print(f"warming the role models: {await runtime.warm_models(cfg)}")
    rc = 0
    try:
        while True:
            st = await scheduler.run_round(ctx)
            if args.json:
                _dump(st.to_dict())
            else:
                print(scheduler.summary_line(st))
                for src, v in sorted(st.sources.items()):
                    print(f"  {src}: searches {v['searches']}, ok {v['ok']}, failed {v['failed']}, listings {v['listings']}")
                if st.dropped:
                    print(f"  dropped: {st.dropped}")
                for d in st.deals:
                    print(f"  DEAL #{d['id']} T{d['tier']}{' *' if d['star'] else ''} {d['key']} cons ${d['cons_net_usd']} "
                          f"exp ${d['exp_net_usd']} {d['confidence']}/{d['effort']} via {d['channel']} "
                          f"{'verified ' if d['verified'] else ''}{d['url']}")
                for e in st.errors[:10]:
                    print(f"  error: {e}")
            if args.once or stop.is_set():
                break
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), float(scheduler.sched_cfg(cfg)["round_interval_s"]))
            if stop.is_set():
                break
    except config.FrozenChanged as e:
        print(f"refused: {e}")
        rc = 1
    finally:
        n = ledger.close_all_windows(store, datetime.now(timezone.utc))
        print(f"observation windows closed: {n}")
        await fetcher.aclose()
        if ctx.llm is not None:
            await ctx.llm.aclose()
            runtime.release_models(cfg, before)
        store.close()
    return rc


def cmd_round(args) -> int:
    return asyncio.run(_round(args))


# ------------------------------------------------------------------------------------------------ deals / show / verify


def _deal_row(store, d) -> dict:
    r = store.conn.execute("select source, url, title, price, currency, country, city from listings where id=?",
                           (d.listing_ref,)).fetchone()
    lst = dict(r) if r else {}
    return {"id": d.id, "tier": d.tier, "star": d.star, "key": d.key, "effort": d.effort, "confidence": d.confidence,
            "cons_net_usd": str(d.cons_net_usd), "exp_net_usd": str(d.exp_net_usd), "capital_usd": str(d.capital_usd),
            "status": d.status, "created_at": d.created_at.isoformat(),
            "verified_at": d.verified_at.isoformat() if d.verified_at else None, "listing": lst}


def cmd_deals(args) -> int:
    root, _ = _ctx()
    store = _store(root)
    if store is None:
        return 1
    try:
        ds = store.deals({"tier": args.tier} if args.tier else None)
        ds.sort(key=lambda d: (not d.star, d.tier, -Decimal(d.cons_net_usd)))
        rows = [_deal_row(store, d) for d in ds[: args.limit]]
    finally:
        store.close()
    if args.json:
        _dump(rows)
        return 0
    if not rows:
        print("no deals")
    for r in rows:
        l = r["listing"]
        print(f"#{r['id']} T{r['tier']}{' *' if r['star'] else ''} {r['key']} cons ${r['cons_net_usd']} exp ${r['exp_net_usd']} "
              f"{r['confidence']}/{r['effort']} {l.get('price')} {l.get('currency')} {l.get('source')} {l.get('url')}")
    return 0


def cmd_show(args) -> int:
    from . import cards
    root, _ = _ctx()
    store = _store(root)
    if store is None:
        return 1
    try:
        d = store.get_deal(args.id)
        if d is None:
            print(f"no deal {args.id}", file=sys.stderr)
            return 1
        md, data = cards.from_deal(d, root)
    finally:
        store.close()
    if args.json:
        _dump(data)
    else:
        print(md)
    return 0


async def _verify(args) -> int:
    from . import verify
    from .customs import Customs
    from .fetch import Fetcher
    from .money import Fx
    from .sources import registry
    root, cfg = _ctx()
    store = _store(root)
    if store is None:
        return 1
    fetcher = Fetcher(cfg, store, root / "var")
    try:
        if store.get_deal(args.id) is None:
            print(f"no deal {args.id}", file=sys.stderr)
            return 1
        res = await verify.verify_deal(store, args.id, cfg, Fx(store), Customs(store), registry(cfg, root), fetcher)
        verify.record_verification(store, args.id, res.ok)
    finally:
        await fetcher.aclose()
        store.close()
    out = {"deal_id": args.id, "ok": res.ok, "alive": res.alive, "price_now": res.price_now,
           "net_now_cons_pln": res.net_now_cons_pln, "net_stored_cons_pln": res.net_stored_cons_pln, "diffs": res.diffs}
    if args.json:
        _dump(out)
    else:
        print(f"deal {args.id}: {'OK' if res.ok else 'NOT OK'}; alive={res.alive} price_now={res.price_now}; "
              f"net now {res.net_now_cons_pln} PLN vs stored {res.net_stored_cons_pln} PLN")
        for d in res.diffs:
            print(f"  - {d}")
    return 0 if res.ok else 1


def cmd_verify(args) -> int:
    return asyncio.run(_verify(args))


def cmd_books(args) -> int:
    from . import books, ledger
    root, _ = _ctx()
    store = _store(root)
    if store is None:
        return 1
    try:
        today = datetime.now(timezone.utc).date().isoformat()
        out = {"all_time": books.summary(store), "today": books.summary(store, since=today),
               "watched_hours": round(ledger.watched_hours(store), 2),
               "deals": {str(r["tier"]): r["n"] for r in store.conn.execute(
                   "select tier, count(*) n from deals group by tier order by tier")},
               "forecast_cons_net_pln": str(sum((Decimal(r[0]) for r in store.conn.execute(
                   "select cons_net_pln from deals")), Decimal(0)))}
    finally:
        store.close()
    if args.json:
        _dump(out)
        return 0
    for label in ("today", "all_time"):
        s = out[label]
        print(f"{label}: {s['rounds']} rounds, cost {s['total_pln']} PLN")
        for k, v in sorted(s["by_kind"].items()):
            print(f"  {k}: {v['pln']} PLN, units {v['units']:g}")
    print(f"watched hours {out['watched_hours']}; deals by tier {out['deals']}; "
          f"forecast conservative net {out['forecast_cons_net_pln']} PLN")
    return 0


# ------------------------------------------------------------------------------------------------ parser


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dh", description="Deal Hunter")
    sub = p.add_subparsers(dest="command", metavar="command")
    sp = sub.add_parser("start", help="start the agent (systemd user unit; refuses on low RAM or a foreign model)")
    sp.set_defaults(func=cmd_start)
    sp = sub.add_parser("stop", help="stop everything, close observation windows, release the models it loaded")
    sp.set_defaults(func=cmd_stop)
    sp = sub.add_parser("status", help="running?, rounds, last round, deals today, health, ntfy topic, log repo")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_status)
    sp = sub.add_parser("round", help="run rounds in the foreground (testing)")
    sp.add_argument("--once", action="store_true", help="one round, then exit")
    sp.add_argument("--dry", action="store_true", help="no ntfy push, no pending-push retries")
    sp.add_argument("--no-llm", action="store_true", help="rules matcher only; does not touch llama-swap")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_round)
    sp = sub.add_parser("deals", help="list deals (star, tier, net)")
    sp.add_argument("--tier", type=int, choices=[1, 2, 3, 4])
    sp.add_argument("--limit", type=int, default=100)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_deals)
    sp = sub.add_parser("show", help="print a deal card")
    sp.add_argument("id", type=int)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_show)
    sp = sub.add_parser("verify", help="recompute the net from stored evidence and re-check the listing live")
    sp.add_argument("id", type=int)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_verify)
    sp = sub.add_parser("books", help="costs (energy, LLM, fetches) and forecast")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_books)
    for name in STUBS:
        sp = sub.add_parser(name, help=f"{name} (later milestone)")
        sp.set_defaults(func=_stub)
    from . import probe

    sp = sub.add_parser("probe", help="run one source adapter search live and print the listings")
    probe.add_arguments(sp)
    sp.set_defaults(func=probe.run)
    return p


def main(argv: list[str] | None = None) -> int:
    p = build_parser()
    args = p.parse_args(argv)
    if not getattr(args, "func", None):
        p.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
