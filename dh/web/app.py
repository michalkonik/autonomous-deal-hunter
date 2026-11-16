"""Local web page (spec section 12): deals list, deal cards with Bought/Skipped/Sold forms, rounds, playbook,
books, health. Server-rendered Jinja2 (autoescape ON: listing titles are untrusted), no JavaScript.

Each request opens its own SQLite connection. Only `POST /deal/{id}/mark` writes: a `marks` row
(source "web") plus the card file's `outcome:` block, so the publisher pushes it to the log repo.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import socket
import subprocess
import time
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import jinja2
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from starlette.routing import Route

from .. import cards, ledger, playbook_io, publish
from ..books import realised_net
from ..customs import Customs
from ..money import Fx
from ..store import Store

MARK_STATUSES = ("bought", "skipped", "sold")
# Sortable columns of the deals table (owner request 2026-11-07): header links pass ?sort=<col>&dir=asc|desc.
SORT_COLS = ("tier", "deal", "src", "ctry", "date", "cons", "exp", "conf", "ship")
# Countries per adapter (display only, for the Current Parameters page; feeds included).
PAGE_SIZE = 100
_PRICE_RE = re.compile(r"\d{1,9}([.,]\d{1,2})?")
_TIME_RE = re.compile(r"([01]\d|2[0-3]):[0-5]\d")
_TEMPLATES = Path(__file__).parent / "templates"
_ENV = jinja2.Environment(loader=jinja2.FileSystemLoader(str(_TEMPLATES)),
                          autoescape=jinja2.select_autoescape(["html"], default=True), undefined=jinja2.StrictUndefined)


def _bad(msg: str) -> HTTPException:
    return HTTPException(400, msg)


def _q2(x) -> str:
    return str(Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _skey(sort: str, d, i) -> object:
    """Sort key for a deal row (`i`: its joined listing/extraction info row or None). Unknown ship_pl sorts last."""
    if sort == "tier":
        return (d.tier, -int(d.star))
    if sort == "deal":
        return d.key or ""
    if sort == "src":
        return (i["source"] or "") if i else ""
    if sort == "ctry":
        return (i["country"] or "") if i else ""
    if sort == "date":
        return d.created_at
    if sort == "cons":
        return d.cons_net_usd
    if sort == "exp":
        return d.exp_net_usd
    if sort == "conf":
        return d.confidence
    return (d.ship_pl is None, d.ship_pl if d.ship_pl is not None else -1)


def _parse_filters(params) -> dict:
    f: dict = {}
    sort = (params.get("sort") or "").strip()
    if sort:
        if sort not in SORT_COLS:
            raise _bad(f"sort must be one of {','.join(SORT_COLS)}")
        f["sort"] = sort
        d = (params.get("dir") or "").strip()
        if d:
            if d not in ("asc", "desc"):
                raise _bad("dir must be asc or desc")
            f["dir"] = d
    tier = (params.get("tier") or "").strip()
    if tier:
        if tier not in ("1", "2", "3", "4", "5"):
            raise _bad("tier must be 1-5")
        f["tier"] = int(tier)
    for k in ("category", "country", "source"):
        v = (params.get(k) or "").strip()
        if v:
            if len(v) > 64:
                raise _bad(f"{k} too long")
            f[k] = v
    day = (params.get("date") or "").strip()
    if day:
        try:
            date.fromisoformat(day)
        except ValueError:
            raise _bad("date must be YYYY-MM-DD") from None
        f["date"] = day
    tm = (params.get("time") or "").strip()
    if tm:
        if not _TIME_RE.fullmatch(tm):
            raise _bad("time must be HH:MM")
        f["time"] = tm
    since = (params.get("since") or "").strip()
    if since:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2} ([01]\d|2[0-3]):00", since):
            raise _bad("since must look like 2026-11-07 08:00 (a full hour)")
        try:
            datetime.strptime(since, "%Y-%m-%d %H:%M")
        except ValueError:
            raise _bad("since must be a real date and hour") from None
        f["since"] = since
    fp = (params.get("fp") or "").strip()
    if fp:
        if fp not in ("yes", "no"):
            raise _bad("fp must be yes or no")
        f["fp"] = fp
    return f


def _local_ipv4s() -> set[str]:
    """IPv4 address of every interface that is UP, read live (Linux SIOCGIFADDR ioctl; empty elsewhere).
    `gethostbyname_ex(hostname)` misses DHCP LAN addresses on hosts whose name maps only to 127.0.1.1."""
    import fcntl
    import struct
    out: set[str] = set()
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError:
        return out
    try:
        for _, name in socket.if_nameindex():
            try:
                packed = fcntl.ioctl(sock.fileno(), 0x8915, struct.pack("256s", name[:15].encode()))
            except OSError:  # interface without IPv4 (down, IPv6-only, ...) - skip
                continue
            out.add(socket.inet_ntoa(packed[20:24]))
    except OSError:
        pass
    finally:
        sock.close()
    return out


def default_allowed_hosts() -> list[str]:
    """Current local addresses: localhost, the rig hostname and its addresses, and every address currently
    configured on a local interface (so a DHCP change after startup does not lock the owner out of POSTs).
    mDNS (`rig.local`) and Tailscale (`rig.tailnet.ts.net`, 100.x addresses) names are NOT guessed:
    add them to `web.allowed_hosts` in config/local/web.yaml (an explicit list disables this discovery)."""
    hosts = {"localhost", "127.0.0.1", "::1"}
    try:
        name = socket.gethostname()
        hosts.add(name)
        hosts.update(a for a in socket.gethostbyname_ex(name)[2])
    except OSError:
        pass
    hosts.update(_local_ipv4s())
    return sorted(hosts)


_HOSTS_CACHE: tuple[float, set[str]] | None = None


def _auto_hosts() -> set[str]:
    """default_allowed_hosts() with a 60 s cache: fresh enough for a DHCP lease change, cheap for POSTs."""
    global _HOSTS_CACHE
    now = time.monotonic()
    if _HOSTS_CACHE is None or now - _HOSTS_CACHE[0] > 60:
        _HOSTS_CACHE = (now, set(default_allowed_hosts()))
    return _HOSTS_CACHE[1]


def _host_only(h: str) -> str:
    h = h.strip().lower()
    if h.startswith("["):
        return h[1:].split("]")[0]
    return h.split(":")[0]


def _default_hunter_status():
    """Is the hunter unit active right now? True/False, or None when systemctl cannot answer (other OS,
    systemd missing). The page server gets `DH_HUNTER_UNIT` from `dh web start`/`dh start`."""
    unit = os.environ.get("DH_HUNTER_UNIT") or os.environ.get("DH_UNIT_NAME") or "deal-hunter"
    try:
        r = subprocess.run(["systemctl", "--user", "is-active", unit], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() in ("active", "activating", "reloading")


def _self_term() -> None:
    """"stop the page": terminate THIS page-server process shortly after the button's response is sent.
    As a transient systemd unit (`<unit>-web`) the unit exits with it; the hunter is never touched."""
    import asyncio
    import signal as _signal

    def kill():
        os.kill(os.getpid(), _signal.SIGTERM)

    try:
        asyncio.get_running_loop().call_later(0.5, kill)
    except RuntimeError:
        kill()


def create_app(store_path, cfg, root, *, fx_factory=None, customs_factory=None,
               hunter_status=None, page_stop=None) -> Starlette:
    """`fx_factory(store)` / `customs_factory(store)` build the objects used for the books' realised net
    (default: the same cached NBP `Fx` and ISZTAR `Customs` the verifier uses)."""
    root = Path(root)
    fx_factory = fx_factory or (lambda st: Fx(st))
    customs_factory = customs_factory or (lambda st: Customs(st))
    hunter_status = hunter_status or _default_hunter_status
    page_stop = page_stop or _self_term
    web_cfg = cfg.get("web")
    # web.allowed_hosts: an explicit Host list (port ignored) accepted on POST, e.g. mDNS "rig.local" or a
    # Tailscale name. Unset (default): the current local addresses, re-discovered per request (_auto_hosts).
    override = web_cfg.get("allowed_hosts") if web_cfg is not None else None
    allowed_hosts = {_host_only(str(h)) for h in override} if override else None
    try:
        cd = Path(str(cfg.publish.cards_dir))
    except AttributeError:
        cd = Path("var/cards/deals")
    cards_dir = cd if cd.is_absolute() else root / cd
    cards_root = cards_dir.parent  # cards.write puts files under <root>/deals/<day>/

    def card_path(deal) -> Path:
        day = deal.created_at.astimezone(timezone.utc).strftime("%Y-%m-%d")
        return cards_root / "deals" / day / f"{cards.slug(deal.key)}-{deal.id}.md"

    def open_store() -> Store:
        return Store(store_path)

    def render(request: Request, name: str, status: int = 200, **ctx) -> HTMLResponse:
        ctx.setdefault("page", name.removesuffix(".html"))
        try:  # the nav badge must never break a page
            ctx.setdefault("hunter", hunter_status())
        except Exception:  # noqa: BLE001
            ctx.setdefault("hunter", None)
        return HTMLResponse(_ENV.get_template(name).render(**ctx), status_code=status)

    # ---------------------------------------------------------------- deals list
    async def deals_page(request: Request):
        f = _parse_filters(request.query_params)
        page = (request.query_params.get("page") or "").strip() or "1"
        if not page.isdigit() or not 1 <= int(page) <= 100000:
            raise _bad("page must be a positive integer")
        page = int(page)
        st = open_store()
        try:
            sql = ("select d.* from deals d join listings l on l.id=d.listing_ref "
                   "left join extractions e on e.listing_id=d.listing_ref "
                   "and e.rowid=(select max(rowid) from extractions where listing_id=d.listing_ref) where 1=1")
            args: list = []
            for col, key in (("d.tier", "tier"), ("e.category", "category"), ("l.country", "country"),
                             ("l.source", "source")):
                if key in f:
                    sql += f" and {col}=?"
                    args.append(f[key])
            deals = [st._deal(r) for r in st.conn.execute(sql, args)]
            # date/time/fp compare against LOCAL values (created_at is UTC, marks need a join per deal): done
            # after fetch, like the sort - the whole result set is already in memory for pagination.
            if "date" in f or "time" in f or "since" in f:
                def _keep_local(d) -> bool:
                    lt = d.created_at.astimezone()  # machine-local tz (Europe/Warsaw on the rig)
                    if "date" in f and lt.strftime("%Y-%m-%d") != f["date"]:
                        return False
                    if "time" in f and lt.strftime("%H:%M") < f["time"]:
                        return False
                    if "since" in f and lt.strftime("%Y-%m-%d %H:00") < f["since"]:
                        return False
                    return True
                deals = [d for d in deals if _keep_local(d)]
            info = {r["id"]: r for r in st.conn.execute(
                "select l.id, l.source, l.country, l.title, l.url, e.category from listings l "
                "left join extractions e on e.listing_id=l.id")}
            last = {r["deal_id"]: {"status": r["status"], "fp": bool(r["false_positive"])} for r in st.conn.execute(
                "select deal_id, status, false_positive from marks where id in (select max(id) from marks group by deal_id)")}
            opts = {
                "source": [r[0] for r in st.conn.execute("select distinct source from listings order by 1")],
                "country": [r[0] for r in st.conn.execute(
                    "select distinct country from listings where country is not null order by 1")],
                "category": [r[0] for r in st.conn.execute(
                    "select distinct category from extractions where category is not null order by 1")],
            }
        finally:
            st.close()
        if "fp" in f:
            want = f["fp"] == "yes"
            deals = [d for d in deals if bool((last.get(d.id) or {}).get("fp")) == want]
        if "sort" in f:
            deals.sort(key=lambda d: _skey(f["sort"], d, info.get(d.listing_ref)), reverse=f.get("dir") == "desc")
        else:  # default order: starred first, then tier, then the best conservative net
            deals.sort(key=lambda d: (not d.star, d.tier, -d.cons_net_usd, d.id))
        shd = {}
        for col in SORT_COLS:
            q2 = {k: str(v) for k, v in f.items()}
            q2["sort"] = col
            q2["dir"] = "desc" if (f.get("sort") == col and f.get("dir", "asc") == "asc") else "asc"
            shd[col] = "/?" + urlencode(q2)
        arrows = {col: "" for col in SORT_COLS}
        if "sort" in f:
            arrows[f["sort"]] = " &#9650;" if f.get("dir", "asc") == "asc" else " &#9660;"
        total = len(deals)
        pages = max(1, -(-total // PAGE_SIZE))
        deals = deals[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]
        rows = [{"d": d, "l": info.get(d.listing_ref), "mark": (last.get(d.id) or {}).get("status"),
                 "fp": bool((last.get(d.id) or {}).get("fp")), "cons_usd": _q2(d.cons_net_usd),
                 "exp_usd": _q2(d.exp_net_usd), "date": d.created_at.astimezone().strftime("%Y-%m-%d"),
                 "time": d.created_at.astimezone().strftime("%H:%M"),
                 "ship": ("yes" if d.ship_pl == 1 else "no" if d.ship_pl == 0 else "-"),
                 "url": (info[d.listing_ref]["url"] or "") if d.listing_ref in info else ""} for d in deals]
        # "since" dropdown: every full hour of the last 7 days, newest first (local time, like the filters)
        now_h = datetime.now().astimezone().replace(minute=0, second=0, microsecond=0)
        since_opts = [(now_h - timedelta(hours=i)).strftime("%Y-%m-%d %H:00") for i in range(168)]
        return render(request, "deals.html", rows=rows, f={k: str(v) for k, v in f.items()}, opts=opts,
                      page_no=page, pages=pages, total=total, since_opts=since_opts, shd=shd, arrows=arrows,
                      qs=("&" + urlencode({k: str(v) for k, v in f.items()})) if f else "")

    # ---------------------------------------------------------------- deal card
    def deal_response(request: Request, deal, marks, error: str | None, status: int = 200):
        _, card = cards.from_deal(deal, cards_root)
        return render(request, "deal.html", status=status, deal=deal, c=card, marks=marks, error=error)

    async def deal_page(request: Request):
        deal_id = _deal_id(request)
        st = open_store()
        try:
            deal = st.get_deal(deal_id)
            if deal is None:
                raise HTTPException(404, "no such deal")
            marks = st.conn.execute("select * from marks where deal_id=? order by id desc", (deal_id,)).fetchall()
        finally:
            st.close()
        return deal_response(request, deal, marks, None)

    # ---------------------------------------------------------------- mark
    async def mark_post(request: Request):
        _same_origin(request, allowed_hosts or _auto_hosts())
        deal_id = _deal_id(request)
        form = await request.form()
        st = open_store()
        try:
            deal = st.get_deal(deal_id)
            if deal is None:
                raise HTTPException(404, "no such deal")
            marks = st.conn.execute("select * from marks where deal_id=? order by id desc", (deal_id,)).fetchall()

            def fail(msg: str):
                return deal_response(request, deal, marks, msg, 400)

            status = str(form.get("status") or "").strip().lower()
            if status not in MARK_STATUSES:
                return fail("status must be bought, skipped or sold")
            raw = str(form.get("price_pln") or "").strip()
            price: Decimal | None = None
            if status == "sold" or raw:
                if not _PRICE_RE.fullmatch(raw):
                    return fail("price must look like 2900 or 2900.50 (at most 9 digits, 2 decimals)")
                price = Decimal(raw.replace(",", "."))
                if price <= 0:
                    return fail("price must be greater than 0")
            note = " ".join(str(form.get("reason") or form.get("note") or "").split())[:500]
            false_positive = str(form.get("false_positive") or "").strip().lower() in ("1", "on", "true", "yes")
            outcome = {"status": status, "price_pln": None if price is None else str(price),
                       "date": datetime.now(timezone.utc).date().isoformat(), "note": note}
            # (a) DB first, one transaction: the mark and its mark_imports hash. record_import does not
            # commit; add_mark's commit commits both, and any failure rolls both back.
            st.ensure_table(publish.MARK_IMPORTS_SQL)
            try:
                publish.record_import(st.conn, deal_id, outcome)
                st.add_mark(deal_id, status, price, note, "web", false_positive=false_positive)
            except Exception:
                st.conn.rollback()
                raise
            # (b) then the card files (atomic replace). A failure here loses nothing: the mark is in the DB and
            # the publisher writes the outcome from mark_imports into the log copy next cycle.
            try:
                md = card_path(deal)
                if not md.is_file():  # an existing card is only touched through set_outcome
                    md = cards.write(deal, cards_root)
                cards.set_outcome(md, outcome)
            except Exception as e:
                st.add_health("web", "card_write_failed", f"deal {deal_id}: {e!r}")
        finally:
            st.close()
        return RedirectResponse(f"/deal/{deal_id}", status_code=303)

    # ---------------------------------------------------------------- other pages
    async def rounds_page(request: Request):
        st = open_store()
        try:
            rounds = st.conn.execute("select * from rounds order by id desc limit 200").fetchall()
            costs: dict = {}
            for r in st.conn.execute("select round_id, amount_pln from costs"):
                costs[r["round_id"]] = costs.get(r["round_id"], Decimal(0)) + Decimal(r["amount_pln"])
        finally:
            st.close()
        return render(request, "rounds.html", rounds=rounds, costs={k: _q2(v) for k, v in costs.items()})

    async def playbook_page(request: Request):
        try:
            items = playbook_io.load_items(root)
            err = None
        except (ValueError, OSError) as e:
            items, err = [], str(e)
        st = open_store()
        try:
            vers = st.conn.execute("select * from playbook_versions order by id").fetchall()
            marks = {r["status"]: r["n"] for r in st.conn.execute(
                "select status, count(*) n from marks group by status")}
            try:
                watched = round(ledger.watched_hours(st), 1)
            except Exception:  # scoreboard is informational; never break the page
                watched = None
            ndeals = st.conn.execute("select count(*) from deals").fetchone()[0]
        finally:
            st.close()
        diffs = []
        for prev, cur in zip([None, *vers], vers):
            old = (prev["content"] if prev else "").splitlines()
            lines = list(difflib.unified_diff(old, (cur["content"] or "").splitlines(),
                                              prev["version"] if prev else "(empty)", cur["version"], lineterm="", n=2))
            diffs.append({"v": cur, "diff": "\n".join(lines)})
        diffs.reverse()
        score = {"deals": ndeals, "marks": marks, "watched_hours": watched}
        return render(request, "playbook.html", items=items, error=err, diffs=diffs, score=score)

    async def books_page(request: Request):
        st = open_store()
        try:
            kinds_d: dict = {}
            for r in st.conn.execute("select kind, amount_pln, units from costs order by kind"):
                k = kinds_d.setdefault(r["kind"], {"kind": r["kind"], "n": 0, "pln": Decimal(0), "units": 0.0})
                k["n"] += 1
                k["pln"] += Decimal(r["amount_pln"])
                k["units"] += r["units"] or 0.0
            exp = sum((Decimal(r[0]) for r in st.conn.execute("select exp_net_pln from deals")), Decimal(0))
            cons = sum((Decimal(r[0]) for r in st.conn.execute("select cons_net_pln from deals")), Decimal(0))
            ndeals = st.conn.execute("select count(*) from deals").fetchone()[0]
            latest = st.conn.execute(
                "select deal_id, status, price_pln, created_at from marks "
                "where id in (select max(id) from marks group by deal_id) order by deal_id").fetchall()
            sold_rows, realised_pln, realised_usd = [], Decimal(0), Decimal(0)
            fx, cu = fx_factory(st), customs_factory(st)
            for m in latest:
                if m["status"] != "sold" or not m["price_pln"]:
                    continue
                deal = st.get_deal(m["deal_id"])
                if deal is None:
                    continue
                price = Decimal(m["price_pln"])
                row = {"id": deal.id, "key": deal.key, "sold": _q2(price), "cons": _q2(deal.cons_net_pln),
                       "net_pln": None, "net_usd": None, "error": None}
                try:
                    res = await realised_net(st, deal, price, cfg, fx, cu)
                    usd = await fx.pln_to_usd(res.cons_net_pln, date.fromisoformat(str(m["created_at"])[:10]))
                    row.update(net_pln=_q2(res.cons_net_pln), net_usd=_q2(usd))
                    realised_pln += res.cons_net_pln
                    realised_usd += Decimal(str(usd))
                except Exception as e:  # missing evidence, FX or duty lookup: show the deal, flag the row
                    row["error"] = f"{type(e).__name__}: {e}"[:200]
                sold_rows.append(row)
        finally:
            st.close()
        books = {"cost_total": _q2(sum((k["pln"] for k in kinds_d.values()), Decimal(0))), "exp": _q2(exp),
                 "cons": _q2(cons), "deals": ndeals, "bought": sum(m["status"] == "bought" for m in latest),
                 "sold_n": len(sold_rows), "realised_pln": _q2(realised_pln), "realised_usd": _q2(realised_usd),
                 "unpriced": sum(1 for r in sold_rows if r["error"]),
                 "empty": not kinds_d and not ndeals and not latest}
        kinds = [{**k, "pln": _q2(k["pln"])} for k in kinds_d.values()]
        return render(request, "books.html", books=books, kinds=kinds, sold_rows=sold_rows)

    async def params_page(request: Request):
        """Current Parameters: the agent's live configuration, human-readable. Names and rules only - never
        secrets (mailbox credentials and API keys live in bw-secret, they are not in cfg)."""
        data = cfg.to_dict()
        sources = data.get("sources") or {}
        adapters = sources.get("adapters") or {}
        rows = [{"name": n, "country": SOURCE_COUNTRY.get(n, ""), "enabled": bool((a or {}).get("enabled")),
                 "via": (a or {}).get("via") or "polling", "reason": (a or {}).get("reason") or ""}
                for n, a in sorted(adapters.items())]
        retail = sources.get("retail") or {}
        watches = [{"shop": w.get("shop", ""), "url": w.get("url", "")} for w in retail.get("watch") or []]
        countries = sorted({c for n, a in adapters.items() if (a or {}).get("enabled")
                            for c in SOURCE_COUNTRY.get(n, "").split(", ")})
        items = playbook_io.load_items(root)
        queries = [{"value": it["value"], "target": it.get("target") or "all",
                    "min_pln": it.get("min_pln"), "max_pln": it.get("max_pln")}
                   for it in items if it["kind"] == "query"]
        rules, sched, models = data.get("rules") or {}, data.get("scheduler") or {}, data.get("models") or {}
        money = {"floors": rules.get("floors") or {}, "t1_min": (rules.get("tiers") or {}).get("t1_min_cons_net_usd"),
                 "star_usd": rules.get("star_usd"), "capital_cap_usd": rules.get("capital_cap_usd")}
        cadence = {k: sched.get(k) for k in ("round_interval_s", "budget", "budget_max", "max_searches_per_source",
                                             "max_llm_items", "reeval_hours", "round_timeout_s")}
        roles = models.get("roles") or {}
        web = data.get("web") or {}
        return render(request, "params.html", rows=rows, watches=watches, drop_pct=retail.get("drop_pct"),
                      countries=countries, queries=queries, money=money, cadence=cadence, roles=roles,
                      paid_enabled=bool((models.get("paid_api") or {}).get("enabled")),
                      feed_sources=sources.get("feed_sources") or sched.get("feed_sources") or [],
                      web_host=web.get("host", "0.0.0.0"), web_port=web.get("port", 8711))

    async def health_page(request: Request):
        st = open_store()
        try:
            events = st.conn.execute("select * from health order by id desc limit 200").fetchall()
            summary = st.conn.execute(
                "select source, kind, count(*) n, max(at) last from health group by source, kind "
                "order by last desc").fetchall()
            windows = st.conn.execute("select * from windows where closed_at is null order by id").fetchall()
        finally:
            st.close()
        return render(request, "health.html", events=events, summary=summary, windows=windows)

    # ---------------------------------------------------------------- stop the page
    async def page_stop_post(request: Request):
        """The "stop the page" button: stops ONLY the standalone page server (never the hunter). The reply is
        the last thing this process sends; it terminates itself right after (see _self_term)."""
        _same_origin(request, allowed_hosts or _auto_hosts())
        page_stop()
        return render(request, "stopped.html")

    async def http_error(request: Request, exc: HTTPException):
        return PlainTextResponse(f"{exc.status_code}: {exc.detail}\n", status_code=exc.status_code)

    return Starlette(
        routes=[Route("/", deals_page), Route("/deal/{id}", deal_page),
                Route("/deal/{id}/mark", mark_post, methods=["POST"]),
                Route("/page/stop", page_stop_post, methods=["POST"]),
                Route("/rounds", rounds_page), Route("/playbook", playbook_page),
                Route("/books", books_page), Route("/health", health_page), Route("/params", params_page)],
        exception_handlers={HTTPException: http_error})


def _deal_id(request: Request) -> int:
    v = request.path_params["id"]
    if not v.isdigit() or len(v) > 12:
        raise HTTPException(404, "no such deal")
    return int(v)


SOURCE_COUNTRY = {"allegro": "PL", "allegro_lokalnie": "PL", "sprzedajemy": "PL", "olx": "PL", "pepper_pl": "PL",
                  "vinted": "PL", "ebay": "PL, DE", "kleinanzeigen": "DE", "willhaben": "AT", "sbazar": "CZ",
                  "bazos_cz": "CZ", "bazos_sk": "SK", "bazos_at": "AT", "hardverapro": "HU", "marktplaats": "NL",
                  "hotukdeals": "UK"}


def _same_origin(request: Request, allowed_hosts: set[str]) -> None:
    """CSRF: the Origin (else Referer) host must equal Host, and Host must be on the allowlist
    (`web.allowed_hosts` if set; by default the rig's own current addresses - see default_allowed_hosts).
    A POST with neither header is refused."""
    src = request.headers.get("origin") or request.headers.get("referer")
    host = request.headers.get("host")
    try:
        ok = bool(src and host and urlsplit(src).netloc == host)
    except ValueError:
        ok = False
    if not ok:
        raise HTTPException(403, "cross-origin request refused")
    if _host_only(host) not in allowed_hosts:
        raise HTTPException(403, "host not allowed")
