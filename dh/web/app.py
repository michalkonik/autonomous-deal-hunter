"""Local web page (spec section 12): deals list, deal cards with Bought/Skipped/Sold forms, rounds, playbook,
books, health. Server-rendered Jinja2 (autoescape ON: listing titles are untrusted), no JavaScript.

Each request opens its own SQLite connection. Only `POST /deal/{id}/mark` writes: a `marks` row
(source "web") plus the card file's `outcome:` block, so the publisher pushes it to the log repo.
"""
from __future__ import annotations

import difflib
import json
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit

import jinja2
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from starlette.routing import Route

from .. import cards, ledger, playbook_io, publish
from ..store import Store

MARK_STATUSES = ("bought", "skipped", "sold")
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


def create_app(store_path, cfg, root) -> Starlette:
    root = Path(root)
    try:
        cd = Path(str(cfg.publish.cards_dir))
    except AttributeError:
        cd = Path("var/cards/deals")
    cards_dir = cd if cd.is_absolute() else root / cd
    cards_root = cards_dir.parent  # cards.write puts files under <root>/deals/<day>/

    def open_store() -> Store:
        return Store(store_path)

    def render(request: Request, name: str, status: int = 200, **ctx) -> HTMLResponse:
        ctx.setdefault("page", name.removesuffix(".html"))
        return HTMLResponse(_ENV.get_template(name).render(**ctx), status_code=status)

    # ---------------------------------------------------------------- deals list
    async def deals_page(request: Request):
        f = _parse_filters(request.query_params)
        st = open_store()
        try:
            sql = ("select d.* from deals d join listings l on l.id=d.listing_ref "
                   "left join extractions e on e.listing_id=d.listing_ref where 1=1")
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
        rows = [{"d": d, "l": info.get(d.listing_ref), "mark": last.get(d.id), "cons_usd": _q2(d.cons_net_usd),
                 "exp_usd": _q2(d.exp_net_usd)} for d in deals]
        return render(request, "deals.html", rows=rows, f={k: str(v) for k, v in f.items()}, opts=opts)

    # ---------------------------------------------------------------- deal card
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
        _, card = cards.from_deal(deal, cards_root)
        return render(request, "deal.html", deal=deal, c=card, marks=marks, error=None)

    # ---------------------------------------------------------------- mark
    async def mark_post(request: Request):
        _same_origin(request)
        deal_id = _deal_id(request)
        form = await request.form()
        status = str(form.get("status") or "").strip().lower()
        if status not in MARK_STATUSES:
            raise _bad("status must be bought, skipped or sold")
        raw = str(form.get("price_pln") or "").strip().replace(",", ".")
        price: Decimal | None = None
        if status == "sold" or raw:
            try:
                price = Decimal(raw)
            except InvalidOperation:
                raise _bad("price_pln must be a number") from None
            if not price.is_finite() or price <= 0:
                raise _bad("price_pln must be greater than 0")
            price = price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if price <= 0:
                raise _bad("price_pln must be greater than 0")
        note = " ".join(str(form.get("reason") or form.get("note") or "").split())[:500]
        st = open_store()
        try:
            deal = st.get_deal(deal_id)
            if deal is None:
                raise HTTPException(404, "no such deal")
            outcome = {"status": status, "price_pln": None if price is None else str(price),
                       "date": datetime.now(timezone.utc).date().isoformat(), "note": note}
            _write_outcome(st, deal, outcome)
            st.add_mark(deal_id, status, price, note, "web")
        finally:
            st.close()
        return RedirectResponse(f"/deal/{deal_id}", status_code=303)

    def _write_outcome(st: Store, deal, outcome: dict) -> None:
        """Put the outcome in the card file (created if missing) and register it as already imported."""
        md_path = cards.write(deal, cards_root)
        text = cards._swap_outcome(md_path.read_text(), outcome)
        md_path.write_text(text)
        js = md_path.with_suffix(".json")
        try:
            data = json.loads(js.read_text())
        except (OSError, ValueError):
            data = cards.from_deal(deal, cards_root)[1]
        data["outcome"] = outcome
        js.write_text(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
        # the publisher imports outcome blocks as marks; this one is already a mark, so record its hash
        st.ensure_table(publish.MARK_IMPORTS_SQL)
        norm = publish.parse_outcome(text)
        st.conn.execute("insert or replace into mark_imports (deal_id, hash, outcome) values (?,?,?)",
                        (deal.id, publish._ohash(norm), json.dumps(norm)))
        st.conn.commit()

    # ---------------------------------------------------------------- other pages
    async def rounds_page(request: Request):
        st = open_store()
        try:
            rounds = st.conn.execute("select * from rounds order by id desc limit 200").fetchall()
            costs = {r["round_id"]: r["total"] for r in st.conn.execute(
                "select round_id, sum(cast(amount_pln as real)) total from costs group by round_id")}
        finally:
            st.close()
        return render(request, "rounds.html", rounds=rounds, costs={k: _q2(v) for k, v in costs.items() if v is not None})

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
            by_kind = st.conn.execute(
                "select kind, count(*) n, sum(cast(amount_pln as real)) pln, sum(units) units from costs "
                "group by kind order by kind").fetchall()
            cost_total = sum(r["pln"] or 0 for r in by_kind)
            exp = sum(Decimal(r[0]) for r in st.conn.execute("select exp_net_pln from deals"))
            cons = sum(Decimal(r[0]) for r in st.conn.execute("select cons_net_pln from deals"))
            ndeals = st.conn.execute("select count(*) from deals").fetchone()[0]
            latest = st.conn.execute(
                "select status, price_pln from marks where id in (select max(id) from marks group by deal_id)").fetchall()
        finally:
            st.close()
        sold = [Decimal(r["price_pln"]) for r in latest if r["status"] == "sold" and r["price_pln"]]
        books = {"cost_total": _q2(cost_total), "exp": _q2(exp), "cons": _q2(cons), "deals": ndeals,
                 "bought": sum(r["status"] == "bought" for r in latest), "sold_n": len(sold),
                 "sold_pln": _q2(sum(sold, Decimal(0))), "empty": not by_kind and not ndeals and not latest}
        kinds = [{"kind": r["kind"], "n": r["n"], "pln": _q2(r["pln"] or 0), "units": r["units"]} for r in by_kind]
        return render(request, "books.html", books=books, kinds=kinds)

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


def _same_origin(request: Request) -> None:
    """CSRF: the Origin (else Referer) host must equal Host. A POST carrying neither is refused."""
    src = request.headers.get("origin") or request.headers.get("referer")
    host = request.headers.get("host")
    try:
        ok = bool(src and host and urlsplit(src).netloc == host)
    except ValueError:
        ok = False
    if not ok:
        raise HTTPException(403, "cross-origin request refused")
