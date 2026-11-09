"""Local web page (spec section 12): deals list, deal cards with Bought/Skipped/Sold forms, rounds, playbook,
books, health. Server-rendered Jinja2 (autoescape ON: listing titles are untrusted), no JavaScript.

Each request opens its own SQLite connection. Only `POST /deal/{id}/mark` writes: a `marks` row
(source "web") plus the card file's `outcome:` block, so the publisher pushes it to the log repo.
"""
from __future__ import annotations

import difflib
import json
import re
import socket
from datetime import date, datetime, timezone
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
PAGE_SIZE = 100
_PRICE_RE = re.compile(r"\d{1,9}([.,]\d{1,2})?")
_TEMPLATES = Path(__file__).parent / "templates"
_ENV = jinja2.Environment(loader=jinja2.FileSystemLoader(str(_TEMPLATES)),
                          autoescape=jinja2.select_autoescape(["html"], default=True), undefined=jinja2.StrictUndefined)


def _bad(msg: str) -> HTTPException:
    return HTTPException(400, msg)


def _q2(x) -> str:
    return str(Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _parse_filters(params) -> dict:
    f: dict = {}
    tier = (params.get("tier") or "").strip()
    if tier:
        if tier not in ("1", "2", "3", "4"):
            raise _bad("tier must be 1-4")
        f["tier"] = int(tier)
    for k in ("category", "country", "source"):
        v = (params.get(k) or "").strip()
        if v:
            if len(v) > 64:
                raise _bad(f"{k} too long")
            f[k] = v
    since = (params.get("since") or "").strip()
    if since:
        try:
            f["since"] = date.fromisoformat(since)
        except ValueError:
            raise _bad("since must be YYYY-MM-DD") from None
    return f


def default_allowed_hosts() -> list[str]:
    """Rig hostname, its LAN addresses, localhost. mDNS (`rig.local`) and Tailscale (`rig.tailnet.ts.net`,
    100.x addresses) names are NOT guessed: add them to `web.allowed_hosts` in config/local/web.yaml."""
    hosts = {"localhost", "127.0.0.1"}
    try:
        name = socket.gethostname()
        hosts.add(name)
        hosts.update(a for a in socket.gethostbyname_ex(name)[2])
    except OSError:
        pass
    return sorted(hosts)


def _host_only(h: str) -> str:
    h = h.strip().lower()
    if h.startswith("["):
        return h[1:].split("]")[0]
    return h.split(":")[0]


def create_app(store_path, cfg, root, *, fx_factory=None, customs_factory=None) -> Starlette:
    """`fx_factory(store)` / `customs_factory(store)` build the objects used for the books' realised net
    (default: the same cached NBP `Fx` and ISZTAR `Customs` the verifier uses)."""
    root = Path(root)
    fx_factory = fx_factory or (lambda st: Fx(st))
    customs_factory = customs_factory or (lambda st: Customs(st))
    web_cfg = cfg.get("web")
    # web.allowed_hosts: Host names/IPs (port ignored) accepted on POST, e.g. mDNS "rig.local" or a Tailscale name.
    allowed = (web_cfg.get("allowed_hosts") if web_cfg is not None else None) or default_allowed_hosts()
    allowed_hosts = {_host_only(str(h)) for h in allowed}
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
            if "since" in f:
                sql += " and d.created_at >= ?"
                args.append(datetime(f["since"].year, f["since"].month, f["since"].day,
                                     tzinfo=timezone.utc).isoformat())
            deals = [st._deal(r) for r in st.conn.execute(sql, args)]
            deals.sort(key=lambda d: (not d.star, d.tier, -d.cons_net_usd, d.id))
            info = {r["id"]: r for r in st.conn.execute(
                "select l.id, l.source, l.country, l.title, e.category from listings l "
                "left join extractions e on e.listing_id=l.id")}
            last = {r["deal_id"]: r["status"] for r in st.conn.execute(
                "select deal_id, status from marks where id in (select max(id) from marks group by deal_id)")}
            opts = {
                "source": [r[0] for r in st.conn.execute("select distinct source from listings order by 1")],
                "country": [r[0] for r in st.conn.execute(
                    "select distinct country from listings where country is not null order by 1")],
                "category": [r[0] for r in st.conn.execute(
                    "select distinct category from extractions where category is not null order by 1")],
            }
        finally:
            st.close()
        total = len(deals)
        pages = max(1, -(-total // PAGE_SIZE))
        deals = deals[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]
        rows = [{"d": d, "l": info.get(d.listing_ref), "mark": last.get(d.id), "cons_usd": _q2(d.cons_net_usd),
                 "exp_usd": _q2(d.exp_net_usd)} for d in deals]
        return render(request, "deals.html", rows=rows, f={k: str(v) for k, v in f.items()}, opts=opts,
                      page_no=page, pages=pages, total=total,
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
        _same_origin(request, allowed_hosts)
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
            outcome = {"status": status, "price_pln": None if price is None else str(price),
                       "date": datetime.now(timezone.utc).date().isoformat(), "note": note}
            # (a) DB first, one transaction: the mark and its mark_imports hash. record_import does not
            # commit; add_mark's commit commits both, and any failure rolls both back.
            st.ensure_table(publish.MARK_IMPORTS_SQL)
            try:
                publish.record_import(st.conn, deal_id, outcome)
                st.add_mark(deal_id, status, price, note, "web")
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

    async def http_error(request: Request, exc: HTTPException):
        return PlainTextResponse(f"{exc.status_code}: {exc.detail}\n", status_code=exc.status_code)

    return Starlette(
        routes=[Route("/", deals_page), Route("/deal/{id}", deal_page),
                Route("/deal/{id}/mark", mark_post, methods=["POST"]),
                Route("/rounds", rounds_page), Route("/playbook", playbook_page),
                Route("/books", books_page), Route("/health", health_page)],
        exception_handlers={HTTPException: http_error})


def _deal_id(request: Request) -> int:
    v = request.path_params["id"]
    if not v.isdigit() or len(v) > 12:
        raise HTTPException(404, "no such deal")
    return int(v)


def _same_origin(request: Request, allowed_hosts: set[str]) -> None:
    """CSRF: the Origin (else Referer) host must equal Host, and Host must be on the allowlist
    (`web.allowed_hosts`; default: rig hostname, its LAN IPs, localhost). A POST with neither header is refused."""
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
