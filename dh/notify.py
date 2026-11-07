"""Tier-1 notifier: ntfy push -> SMTP email fallback -> pending_pushes queue (retried for up to 6 h)."""
from __future__ import annotations

import asyncio
import base64
import json
import secrets
import smtplib
import string
import subprocess
from types import SimpleNamespace
from urllib.parse import quote, urlsplit
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Callable

import httpx
import yaml

from . import config
from .taxonomy import display_name

PENDING_SQL = """create table if not exists pending_pushes (
  id integer primary key, payload text not null, created_at text not null,
  attempts integer default 0, last_error text
)"""
RETRY_WINDOW = timedelta(hours=6)
_ALPHABET = string.ascii_lowercase + string.digits


def _hhmm(v) -> tuple[int, int]:
    h, m = str(v).split(":")
    return int(h), int(m)


def quiet_until(cfg, now: datetime) -> datetime | None:
    """End (UTC) of the quiet window `now` falls in, or None. config/notify.yaml `quiet_hours: null` (default: pushes
    any time) or `{start: "23:00", end: "07:00", tz: Europe/Warsaw}` (overnight windows wrap). Tier-1 pushes inside
    the window are queued until it ends."""
    try:
        q = cfg.notify.get("quiet_hours")
        q = q.to_dict() if hasattr(q, "to_dict") else q
        if not q:
            return None
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(str(q.get("tz", "Europe/Warsaw")))
        (sh, sm), (eh, em) = _hhmm(q["start"]), _hhmm(q["end"])
    except Exception:  # noqa: BLE001 - a malformed window must not silence tier-1 pushes: no quiet hours
        return None
    local = now.astimezone(tz)
    t = (local.hour, local.minute)
    start, end = (sh, sm), (eh, em)
    if start <= end:
        inside, day_off = start <= t < end, 0
    else:
        inside = t >= start or t < end
        day_off = 1 if t >= start else 0
    if not inside:
        return None
    return (local.replace(hour=eh, minute=em, second=0, microsecond=0) + timedelta(days=day_off)).astimezone(timezone.utc)


def topic(root: Path) -> str:
    """Random 24-char ntfy topic, generated on first use and kept in config/local/notify.yaml."""
    p = Path(root) / "config" / "local" / "notify.yaml"
    data = yaml.safe_load(p.read_text()) if p.is_file() else None
    data = data or {}
    t = (data.get("ntfy") or {}).get("topic")
    if t:
        return t
    t = "".join(secrets.choice(_ALPHABET) for _ in range(24))
    data.setdefault("ntfy", {})["topic"] = t
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(data))
    return t


def title(deal: Any, source: str | None = None, city: str | None = None) -> str:
    """`T1 ★ +$640 RTX 4090 24GB · OLX Kraków` (★ only when deal.star; the product's display name, not its key)."""
    net = Decimal(deal.cons_net_usd).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    star = " ★" if deal.star else ""
    where = " ".join(x for x in (_pretty_source(source), city) if x)
    t = f"T1{star} +${net} {_clean(display_name(deal.key)) or 'deal'}"
    where = _clean(where)
    return f"{t} · {where}" if where else t


def _safe_url(url: str) -> str:
    """ASCII-only, header-safe URL: non-ASCII, spaces and control characters are percent-encoded; the comma
    too, since it separates fields inside an ntfy Actions header."""
    return quote(url or "", safe=":/?&=#%@+~!$()*[]")


def header_url(url: str | None) -> str:
    """The header-safe form of an absolute http(s) URL, or "" for anything else (empty, relative, javascript:,
    ftp:, a scheme without a host). An empty URL must never reach a header: ntfy answers 400 to `Actions: view,
    Card, ` (final review C1)."""
    u = (url or "").strip()
    try:
        parts = urlsplit(u)
    except ValueError:
        return ""
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        return ""
    return _safe_url(u)


def ntfy_headers(title_text: str, *, priority: str | None = None, tags: str | None = None, click: str = "",
                 card: str = "") -> dict:
    """THE ntfy header builder, used by real pushes, retried pushes and `send_test` alike. Click / Actions only
    when the URL is a valid http(s) URL; Priority / Tags only when given."""
    h = {"Title": _clean(title_text) or "Deal Hunter"}
    if priority:
        h["Priority"] = str(priority)
    if tags:
        h["Tags"] = str(tags)
    click_u, card_u = header_url(click) or header_url(card), header_url(card)
    if click_u:
        h["Click"] = click_u
    if card_u:
        h["Actions"] = f"view, Card, {card_u}"
    return h


def _clean(x: str | None) -> str:
    """Drop control characters (incl. CR/LF, which would break headers)."""
    return "".join(ch for ch in (x or "") if ch.isprintable()).strip()


def _pretty_source(s: str | None) -> str | None:
    if not s:
        return None
    return {"olx": "OLX", "ebay_pl": "eBay PL", "ebay_de": "eBay DE", "allegro": "Allegro"}.get(s, s.capitalize())


def bw_secret(ref: str) -> str | None:
    """Fetch `item#field` through the bw-secret helper; None when helper/item is missing."""
    try:
        r = subprocess.run(["bw-secret", ref], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def _send_smtp(creds: dict, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, creds["user"], creds["to"]
    msg.set_content(body)
    with smtplib.SMTP(creds["host"], int(creds["port"]), timeout=30) as s:
        s.starttls()
        s.login(creds["user"], creds["password"])
        s.send_message(msg)


def health_once(store, source: str, kind: str, detail: str) -> None:
    """Health event, at most one per (source, kind) per 10 minutes."""
    if store is None:
        return
    recent = store.conn.execute(
        "select 1 from health where source=? and kind=? and at > datetime('now','-10 minutes')",
        (source, kind)).fetchone()
    if not recent:
        store.add_health(source, kind, detail[:500])


def _health(store, kind: str, detail: str) -> None:
    health_once(store, "notify", kind, detail)


def _ntfy_cfg(cfg) -> tuple[str, str]:
    n = cfg.notify.ntfy
    return n.server.rstrip("/"), n.tier1_priority


async def _ntfy(server: str, tp: str, headers: dict, body: str, client: httpx.AsyncClient | None) -> None:
    async def go(c):
        r = await c.post(f"{server}/{tp}", content=body.encode(), headers=headers)
        r.raise_for_status()
    if client is not None:
        await go(client)
    else:
        async with httpx.AsyncClient(timeout=15) as c:
            await go(c)


def _card_link(headers: dict) -> str:
    act = str(headers.get("Actions") or "")
    return act.split(",", 2)[2].strip() if act.count(",") >= 2 else ""


async def _post(server: str, tp: str, payload: dict, client) -> None:
    """POST one payload to ntfy (non-ASCII title as RFC 2047, which ntfy accepts)."""
    headers = dict(payload["headers"])
    if not headers["Title"].isascii():
        headers["Title"] = "=?UTF-8?B?" + base64.b64encode(headers["Title"].encode()).decode() + "?="
    await _ntfy(server, tp, headers, payload["body"], client)


async def _deliver(payload: dict, store, cfg, root: Path, client, smtp_send, secret_fn) -> str | None:
    """None on success, else the last error text."""
    server, _ = _ntfy_cfg(cfg)
    payload = _current(payload, store)
    try:
        await _post(server, topic(root), payload, client)
        return None
    except Exception as e:  # network, HTTP error, anything: fall back
        _health(store, "ntfy_failed", repr(e))
        err = f"ntfy: {e!r}"
    try:
        if not cfg.notify.email_fallback.enabled:
            raise RuntimeError("email fallback disabled")
        raw = await asyncio.to_thread(secret_fn, "dh-mail#smtp")  # subprocess: keep the loop free
        if not raw:
            _health(store, "email_disabled", "bw-secret dh-mail#smtp unavailable")
            return err + "; email disabled"
        links = "\n".join(x for x in (payload["headers"].get("Click"), _card_link(payload["headers"])) if x)
        await asyncio.to_thread(smtp_send, json.loads(raw), payload["headers"]["Title"],
                                payload["body"] + ("\n" + links if links else ""))
        return None
    except Exception as e:
        _health(store, "email_failed", repr(e))
        return err + f"; email: {e!r}"


def _snapshot(deal) -> dict:
    """The deal fields a push shows, as plain JSON (queued with the push: a retry rebuilds the payload from these
    with the CURRENT code, never re-sends headers an older build made)."""
    try:
        ev = json.loads(getattr(deal, "evidence_json", None) or "{}")
        ev = ev if isinstance(ev, dict) else {}
    except ValueError:
        ev = {}
    lst, est, ni = ev.get("listing") or {}, ev.get("estimate") or {}, ev.get("net_inputs") or {}
    g = lambda k: None if getattr(deal, k, None) is None else str(getattr(deal, k))  # noqa: E731
    return {"id": getattr(deal, "id", None), "tier": getattr(deal, "tier", None), "star": bool(getattr(deal, "star", False)),
            "key": getattr(deal, "key", None), "cons_net_usd": g("cons_net_usd"), "exp_net_usd": g("exp_net_usd"),
            "confidence": getattr(deal, "confidence", None), "buy_price": lst.get("price"),
            "buy_currency": lst.get("currency"), "buy_pln": ev.get("buy_pln"), "exit_p25_pln": est.get("p25_pln"),
            "exit_median_pln": est.get("median_pln"), "exit_channel": ni.get("exit_channel")}


def _n0(x) -> str:
    return str(Decimal(str(x)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _body(d: SimpleNamespace, ttl: str) -> str:
    """Spec §12 item 5: buy price, exit, net, confidence (the links are the Click / Actions headers)."""
    name = _clean(display_name(d.key)) or "deal"
    lines = [ttl, name]
    if d.buy_price is not None:
        ccy = (d.buy_currency or "PLN").upper()
        buy = f"Buy {_n0(d.buy_price)} {ccy}"
        if ccy != "PLN" and d.buy_pln is not None:
            buy += f" ({_n0(d.buy_pln)} PLN)"
        lines.append(buy)
    if d.exit_p25_pln is not None and d.exit_median_pln is not None:
        ch = f" on {_clean(str(d.exit_channel))}" if d.exit_channel else ""
        lines.append(f"Sell: exit {_n0(d.exit_p25_pln)}-{_n0(d.exit_median_pln)} PLN (P25-median){ch}")
    exp = f", expected ${_n0(d.exp_net_usd)}" if d.exp_net_usd is not None else ""
    lines.append(f"Conservative net ${_n0(d.cons_net_usd)}{exp}, confidence {d.confidence}")
    return "\n".join(lines)


def compose(params: dict, *, store=None) -> dict:
    """Payload (headers + body) from the queued inputs. Called for every delivery, first try and retries."""
    d = SimpleNamespace(**{**_snapshot(None), **(params.get("deal") or {})})
    source, city = params.get("source"), params.get("city")
    try:
        ttl = title(d, source, city)
        body = _body(d, ttl)
    except Exception as e:  # a bad deal field must not lose the push: fall back to a plain title
        _health(store, "title_error", repr(e))
        ttl = _clean(f"T{d.tier if d.tier is not None else '?'} deal {d.id if d.id is not None else '?'}")
        body = ttl
    headers = ntfy_headers(ttl, priority=params.get("prio"), tags="moneybag",
                           click=params.get("listing_url") or "", card=params.get("card_url") or "")
    return {"headers": headers, "body": body, "params": params}


def build_payload(deal, card_url: str, prio: str, *, store=None, listing_url: str = "", source: str | None = None,
                  city: str | None = None) -> dict:
    params = {"deal": _snapshot(deal), "card_url": card_url or "", "listing_url": listing_url or "",
              "source": source, "city": city, "prio": prio}
    return compose(params, store=store)


def _current(payload: dict, store=None) -> dict:
    """The payload as the CURRENT code builds it: rebuilt from its queued inputs; a legacy entry (headers only, from
    an older build) has its headers passed through the header builder again (bad Click / Actions dropped)."""
    if isinstance(payload.get("params"), dict):
        return compose(payload["params"], store=store)
    h = dict(payload.get("headers") or {})
    act = str(h.get("Actions") or "")
    card = act.split(",", 2)[2].strip() if act.count(",") >= 2 else ""
    headers = ntfy_headers(str(h.get("Title") or ""), priority=h.get("Priority"), tags=h.get("Tags"),
                           click=str(h.get("Click") or ""), card=card)
    return {"headers": headers, "body": str(payload.get("body") or headers["Title"])}


def queue_push(deal, card_url: str, *, store, cfg=None, root: Path | None = None, listing_url: str = "",
               source: str | None = None, city: str | None = None, reason: str = "queued",
               now: datetime | None = None, not_before: datetime | None = None) -> None:
    """Put a tier-1 push straight into the retry queue (no network, no await): used when the sender was cancelled
    mid-flight (SIGTERM), so a reserved push is delivered by the next run's retry instead of being lost."""
    try:
        _, prio = _ntfy_cfg(cfg or config.load(Path(root or ".")))
    except Exception:  # noqa: BLE001 - a broken config must not lose the push either
        prio = "urgent"
    payload = build_payload(deal, card_url, prio, store=store, listing_url=listing_url, source=source, city=city)
    _enqueue(store, payload, now or datetime.now(timezone.utc), reason, not_before)


def _ensure_pending(store) -> None:
    store.ensure_table(PENDING_SQL)
    if "not_before" not in {r["name"] for r in store.conn.execute("pragma table_info(pending_pushes)")}:
        store.ensure_table("alter table pending_pushes add column not_before text")


def _enqueue(store, payload: dict, now: datetime, err: str, not_before: datetime | None = None) -> None:
    _ensure_pending(store)
    store.conn.execute("insert into pending_pushes (payload, created_at, last_error, not_before) values (?,?,?,?)",
                       (json.dumps(payload), now.isoformat(), err, not_before.isoformat() if not_before else None))
    store.conn.commit()


async def push_tier1(deal, card_url: str, *, store=None, cfg=None, root: Path | None = None,
                     listing_url: str = "", source: str | None = None, city: str | None = None,
                     client: httpx.AsyncClient | None = None,
                     smtp_send: Callable = _send_smtp, secret_fn: Callable = bw_secret,
                     now: datetime | None = None) -> bool:
    """Send a tier-1 push. True if delivered by ntfy or email; otherwise queued and False. Never raises."""
    root = Path(root or ".")
    try:
        cfg = cfg or config.load(root)
        _, prio = _ntfy_cfg(cfg)
    except Exception as e:
        _health(store, "config_error", repr(e))
        return False
    payload = build_payload(deal, card_url, prio, store=store, listing_url=listing_url, source=source, city=city)
    when = now or datetime.now(timezone.utc)
    qe = quiet_until(cfg, when)
    if qe is not None and store is not None:  # quiet hours: queued, delivered by the first retry after the window
        _enqueue(store, payload, when, f"quiet hours until {qe.isoformat()}", qe)
        return False
    err = await _deliver(payload, store, cfg, root, client, smtp_send, secret_fn)
    if err is None:
        return True
    if store is not None:
        _enqueue(store, payload, when, err)
    return False


async def retry_pending(store, cfg, root: Path, *, client=None, smtp_send: Callable = _send_smtp,
                        secret_fn: Callable = bw_secret, now: datetime | None = None) -> int:
    """Retry queued pushes (called every round). Drops entries older than 6 h. Returns number delivered."""
    _ensure_pending(store)
    now = now or datetime.now(timezone.utc)
    done = 0
    for r in store.conn.execute("select * from pending_pushes order by id").fetchall():
        nb = datetime.fromisoformat(r["not_before"]) if r["not_before"] else None
        if nb is not None and now < nb:
            continue  # still inside the quiet window
        if now - (nb or datetime.fromisoformat(r["created_at"])) > RETRY_WINDOW:  # the 6 h run from the window's end
            store.conn.execute("delete from pending_pushes where id=?", (r["id"],))
            store.conn.commit()  # never keep a write transaction open across the next delivery (network I/O)
            _health(store, "push_expired", f"pending push {r['id']} dropped after 6h")
            continue
        err = await _deliver(json.loads(r["payload"]), store, cfg, root, client, smtp_send, secret_fn)
        if err is None:
            store.conn.execute("delete from pending_pushes where id=?", (r["id"],))
            done += 1
        else:
            store.conn.execute("update pending_pushes set attempts=attempts+1, last_error=? where id=?",
                               (err, r["id"]))
        store.conn.commit()
    store.conn.commit()
    return done


async def send_test(cfg, root: Path, client=None) -> None:
    """A test notification through the SAME header builder and POST path as a real push (no priority, no links)."""
    server, _ = _ntfy_cfg(cfg)
    payload = {"headers": ntfy_headers("Deal Hunter test", tags="white_check_mark"), "body": "Deal Hunter notifier test"}
    await _post(server, topic(root), payload, client)
