"""Tier-1 notifier: ntfy push -> SMTP email fallback -> pending_pushes queue (retried for up to 6 h)."""
from __future__ import annotations

import asyncio
import base64
import json
import secrets
import smtplib
import string
import subprocess
from urllib.parse import quote
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Callable

import httpx
import yaml

from . import config

PENDING_SQL = """create table if not exists pending_pushes (
  id integer primary key, payload text not null, created_at text not null,
  attempts integer default 0, last_error text
)"""
RETRY_WINDOW = timedelta(hours=6)
_ALPHABET = string.ascii_lowercase + string.digits


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
    """`T1 ★ +$640 RTX 4090 · OLX Kraków` (★ only when deal.star)."""
    net = Decimal(deal.cons_net_usd).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    star = " ★" if deal.star else ""
    where = " ".join(x for x in (_pretty_source(source), city) if x)
    t = f"T1{star} +${net} {_clean(deal.key) or 'deal'}"
    where = _clean(where)
    return f"{t} · {where}" if where else t


def _safe_url(url: str) -> str:
    """ASCII-only, header-safe URL: non-ASCII, spaces and control characters are percent-encoded; the comma
    too, since it separates fields inside an ntfy Actions header."""
    return quote(url or "", safe=":/?&=#%@+~!$()*[]")


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


async def _deliver(payload: dict, store, cfg, root: Path, client, smtp_send, secret_fn) -> str | None:
    """None on success, else the last error text."""
    server, _ = _ntfy_cfg(cfg)
    headers = dict(payload["headers"])
    if not headers["Title"].isascii():  # ntfy accepts UTF-8 headers as RFC 2047
        headers["Title"] = "=?UTF-8?B?" + base64.b64encode(headers["Title"].encode()).decode() + "?="
    try:
        await _ntfy(server, topic(root), headers, payload["body"], client)
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
        await asyncio.to_thread(smtp_send, json.loads(raw), payload["headers"]["Title"],
                                payload["body"] + "\n" + payload["headers"]["Click"])
        return None
    except Exception as e:
        _health(store, "email_failed", repr(e))
        return err + f"; email: {e!r}"


def build_payload(deal, card_url: str, prio: str, *, store=None, listing_url: str = "", source: str | None = None,
                  city: str | None = None) -> dict:
    try:
        ttl = title(deal, source, city)
        body = f"{ttl}\nConservative net ${Decimal(deal.cons_net_usd):.0f}, confidence {deal.confidence}"
    except Exception as e:  # a bad deal field must not lose the push: fall back to a plain title
        _health(store, "title_error", repr(e))
        ttl = _clean(f"T{getattr(deal, 'tier', '?')} deal {getattr(deal, 'id', '?')}")
        body = ttl
    click, card = _safe_url(listing_url or card_url), _safe_url(card_url)
    return {
        "headers": {"Title": ttl, "Priority": prio, "Tags": "moneybag", "Click": click,
                    "Actions": f"view, Card, {card}"},
        "body": body,
    }


def queue_push(deal, card_url: str, *, store, cfg=None, root: Path | None = None, listing_url: str = "",
               source: str | None = None, city: str | None = None, reason: str = "queued",
               now: datetime | None = None) -> None:
    """Put a tier-1 push straight into the retry queue (no network, no await): used when the sender was cancelled
    mid-flight (SIGTERM), so a reserved push is delivered by the next run's retry instead of being lost."""
    try:
        _, prio = _ntfy_cfg(cfg or config.load(Path(root or ".")))
    except Exception:  # noqa: BLE001 - a broken config must not lose the push either
        prio = "urgent"
    payload = build_payload(deal, card_url, prio, store=store, listing_url=listing_url, source=source, city=city)
    store.ensure_table(PENDING_SQL)
    store.conn.execute("insert into pending_pushes (payload, created_at, last_error) values (?,?,?)",
                       (json.dumps(payload), (now or datetime.now(timezone.utc)).isoformat(), reason))
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
    err = await _deliver(payload, store, cfg, root, client, smtp_send, secret_fn)
    if err is None:
        return True
    if store is not None:
        store.ensure_table(PENDING_SQL)
        store.conn.execute("insert into pending_pushes (payload, created_at, last_error) values (?,?,?)",
                           (json.dumps(payload), (now or datetime.now(timezone.utc)).isoformat(), err))
        store.conn.commit()
    return False


async def retry_pending(store, cfg, root: Path, *, client=None, smtp_send: Callable = _send_smtp,
                        secret_fn: Callable = bw_secret, now: datetime | None = None) -> int:
    """Retry queued pushes (called every round). Drops entries older than 6 h. Returns number delivered."""
    store.ensure_table(PENDING_SQL)
    now = now or datetime.now(timezone.utc)
    done = 0
    for r in store.conn.execute("select * from pending_pushes order by id").fetchall():
        if now - datetime.fromisoformat(r["created_at"]) > RETRY_WINDOW:
            store.conn.execute("delete from pending_pushes where id=?", (r["id"],))
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
    return done


async def send_test(cfg, root: Path, client=None) -> None:
    server, _ = _ntfy_cfg(cfg)
    await _ntfy(server, topic(root), {"Title": "Deal Hunter test", "Tags": "white_check_mark"},
                "Deal Hunter notifier test", client)
