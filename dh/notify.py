"""Tier-1 notifier: ntfy push -> SMTP email fallback -> pending_pushes queue (retried for up to 6 h)."""
from __future__ import annotations

import base64
import json
import secrets
import smtplib
import string
import subprocess
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Callable

import httpx
import yaml

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
    t = f"T1{star} +${net} {deal.key or 'deal'}"
    return f"{t} · {where}" if where else t


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


def _health(store, kind: str, detail: str) -> None:
    if store is not None:
        store.add_health("notify", kind, detail[:500])


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


async def _deliver(payload: dict, store, cfg, root: Path, client, smtp_send, secret_fn) -> bool:
    server, _ = _ntfy_cfg(cfg)
    headers = dict(payload["headers"])
    if not headers["Title"].isascii():  # ntfy accepts UTF-8 headers as RFC 2047
        headers["Title"] = "=?UTF-8?B?" + base64.b64encode(headers["Title"].encode()).decode() + "?="
    try:
        await _ntfy(server, topic(root), headers, payload["body"], client)
        return True
    except Exception as e:  # network, HTTP error, anything: fall back
        _health(store, "ntfy_failed", repr(e))
    try:
        if not cfg.notify.email_fallback.enabled:
            raise RuntimeError("email fallback disabled")
        raw = secret_fn("dh-mail#smtp")
        if not raw:
            _health(store, "email_disabled", "bw-secret dh-mail#smtp unavailable")
            return False
        smtp_send(json.loads(raw), payload["headers"]["Title"], payload["body"] + "\n" + payload["headers"]["Click"])
        return True
    except Exception as e:
        _health(store, "email_failed", repr(e))
        return False


async def push_tier1(deal, card_url: str, *, store=None, cfg=None, root: Path | None = None,
                     listing_url: str = "", source: str | None = None, city: str | None = None,
                     client: httpx.AsyncClient | None = None,
                     smtp_send: Callable = _send_smtp, secret_fn: Callable = bw_secret,
                     now: datetime | None = None) -> bool:
    """Send a tier-1 push. True if delivered by ntfy or email; otherwise queued and False. Never raises."""
    root = Path(root or ".")
    _, prio = _ntfy_cfg(cfg)
    ttl = title(deal, source, city)
    payload = {
        "headers": {"Title": ttl, "Priority": prio, "Tags": "moneybag", "Click": listing_url or card_url,
                    "Actions": f"view, Card, {card_url}"},
        "body": f"{ttl}\nConservative net ${Decimal(deal.cons_net_usd):.0f}, confidence {deal.confidence}",
    }
    if await _deliver(payload, store, cfg, root, client, smtp_send, secret_fn):
        return True
    if store is not None:
        store.ensure_table(PENDING_SQL)
        store.conn.execute("insert into pending_pushes (payload, created_at) values (?,?)",
                           (json.dumps(payload), (now or datetime.now(timezone.utc)).isoformat()))
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
        if await _deliver(json.loads(r["payload"]), store, cfg, root, client, smtp_send, secret_fn):
            store.conn.execute("delete from pending_pushes where id=?", (r["id"],))
            done += 1
        else:
            store.conn.execute("update pending_pushes set attempts=attempts+1 where id=?", (r["id"],))
    store.conn.commit()
    return done


async def send_test(cfg, root: Path, client=None) -> None:
    server, _ = _ntfy_cfg(cfg)
    await _ntfy(server, topic(root), {"Title": "Deal Hunter test", "Tags": "white_check_mark"},
                "Deal Hunter notifier test", client)
