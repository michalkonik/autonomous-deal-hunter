"""Polite per-domain fetcher: plain httpx, honest UA, robots.txt, backoff, gzip snapshots.

No impersonation, no proxies, no challenge solving. A block (403/429 or challenge page)
backs the domain off (60 s doubling, cap 1 h); 3 consecutive blocks disable it for the session.
"""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

DEFAULT_UA = "DealHunter/0.1 (personal price research; +https://github.com/michalkonik)"
CHALLENGE_MARKERS = ("cf-chl", "just a moment", "datadome", "captcha-delivery", "_incapsula_")
ROBOTS_FAIL_TTL_S = 300


@dataclass
class FetchResult:
    status: int
    text: str
    url: str
    from_cache: bool = False
    raw_path: Path | None = None
    blocked: bool = False
    disallowed: bool = False


def is_challenge(status: int, text: str) -> bool:
    low = text.lower()
    if status == 200 and len(low) > 50_000:  # real content pages may merely embed a vendor script
        return False
    if any(m in low for m in CHALLENGE_MARKERS):
        return True
    return status == 403 and "akamai" in low


def _fetch_cfg(cfg: Any) -> Any:
    try:
        return cfg.sources.fetch
    except (AttributeError, KeyError):
        pass
    try:
        return cfg["fetch"]
    except (KeyError, TypeError):
        return cfg


def _opt(c: Any, name: str, default: Any) -> Any:
    try:
        return c.get(name, default)
    except AttributeError:
        return getattr(c, name, default)


class _Dom:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.last_request: float | None = None
        self.blocked_until = 0.0
        self.consecutive_blocks = 0
        self.disabled = False


class Fetcher:
    def __init__(self, cfg, store, snapshot_dir, *, clock: Callable[[], float] | None = None,
                 sleep: Callable[[float], Awaitable[None]] | None = None,
                 http: httpx.AsyncClient | None = None):
        c = _fetch_cfg(cfg)
        self.min_interval = float(_opt(c, "min_interval_s", 4))
        self.jitter = float(_opt(c, "jitter_s", 2))
        self.backoff_start = float(_opt(c, "backoff_start_s", 60))
        self.backoff_cap = float(_opt(c, "backoff_cap_s", 3600))
        self.disable_after = int(_opt(c, "disable_after_blocks", 3))
        self.robots_ttl = float(_opt(c, "robots_ttl_s", 86400))
        self.user_agent = str(_opt(c, "user_agent", DEFAULT_UA))
        timeout = float(_opt(c, "timeout_s", 30))
        self.store = store
        self.snapshot_dir = Path(snapshot_dir)
        self.clock = clock or time.monotonic
        self.sleep = sleep or asyncio.sleep
        self.http = http or httpx.AsyncClient(timeout=timeout, follow_redirects=True)
        self._doms: dict[str, _Dom] = {}
        self._robots: dict[str, tuple[float, RobotFileParser | None, bool]] = {}

    # --- state -------------------------------------------------------------
    def _dom(self, domain: str) -> _Dom:
        return self._doms.setdefault(domain, _Dom())

    def blocked(self, domain: str) -> bool:
        d = self._dom(domain)
        return d.disabled or self.clock() < d.blocked_until

    def disabled(self, domain: str) -> bool:
        return self._dom(domain).disabled

    async def aclose(self) -> None:
        await self.http.aclose()

    def _health(self, domain: str, kind: str, detail: str) -> None:
        self.store.add_health(domain, kind, detail)

    def _register_block(self, domain: str, why: str) -> None:
        d = self._dom(domain)
        d.consecutive_blocks += 1
        delay = min(self.backoff_start * 2 ** (d.consecutive_blocks - 1), self.backoff_cap)
        d.blocked_until = self.clock() + delay
        self._health(domain, "blocked", f"{why}; backoff {delay:.0f}s; consecutive={d.consecutive_blocks}")
        if d.consecutive_blocks >= self.disable_after and not d.disabled:
            d.disabled = True
            self._health(domain, "disabled", f"disabled for session after {d.consecutive_blocks} consecutive blocks")

    # --- pacing ------------------------------------------------------------
    async def _pace(self, d: _Dom) -> None:
        """Caller holds d.lock."""
        if d.last_request is not None:
            wait = d.last_request + self.min_interval + random.uniform(0, self.jitter) - self.clock()
            if wait > 0:
                await self.sleep(wait)
        d.last_request = self.clock()

    async def _request(self, domain: str, url: str, headers: dict | None) -> httpx.Response:
        h = {"User-Agent": self.user_agent}
        h.update(headers or {})
        d = self._dom(domain)
        async with d.lock:
            await self._pace(d)
            try:
                return await self.http.get(url, headers=h)
            finally:
                d.last_request = self.clock()

    # --- robots ------------------------------------------------------------
    async def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        domain = parts.netloc.lower()
        now = self.clock()
        cached = self._robots.get(domain)
        if cached is None or now >= cached[0]:
            cached = await self._load_robots(parts.scheme or "https", domain)
            self._robots[domain] = cached
        _, rp, ok = cached
        if rp is None:
            return ok
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        return rp.can_fetch(self.user_agent, path)

    async def _load_robots(self, scheme: str, domain: str):
        now = self.clock()
        try:
            r = await self._request(domain, f"{scheme}://{domain}/robots.txt", None)
        except httpx.HTTPError as e:
            self._health(domain, "robots_error", f"{type(e).__name__}: {e}")
            return (now + ROBOTS_FAIL_TTL_S, None, False)
        if r.status_code == 404:
            return (now + self.robots_ttl, None, True)
        if r.status_code == 200:
            rp = RobotFileParser()
            rp.parse(r.text.splitlines())
            return (now + self.robots_ttl, rp, True)
        self._health(domain, "robots_error", f"robots.txt status {r.status_code}; treating as disallowed")
        return (now + ROBOTS_FAIL_TTL_S, None, False)

    # --- get ---------------------------------------------------------------
    def _snapshot(self, domain: str, url: str, text: str) -> Path:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        sha = hashlib.sha1(url.encode()).hexdigest()
        p = self.snapshot_dir / "snapshots" / day / domain.replace(":", "_") / f"{sha}.gz"
        p.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(p, "wt", encoding="utf-8") as f:
            f.write(text)
        return p

    async def get(self, url: str, *, kind: Literal["html", "json"], headers: dict | None = None) -> FetchResult:
        domain = urlsplit(url).netloc.lower()
        d = self._dom(domain)
        if self.blocked(domain):
            return FetchResult(0, "", url, blocked=True)
        if not await self.allowed(url):
            self._health(domain, "robots_disallow", url)
            return FetchResult(0, "", url, disallowed=True)
        h = dict(headers or {})
        h.setdefault("Accept", "application/json" if kind == "json" else "text/html,application/xhtml+xml")
        try:
            r = await self._request(domain, url, h)
        except httpx.HTTPError as e:
            self._health(domain, "fetch_error", f"{type(e).__name__}: {e}")
            return FetchResult(0, "", url)
        text = r.text
        if r.status_code in (403, 429) or is_challenge(r.status_code, text):
            self._register_block(domain, f"status {r.status_code}" + (" challenge page" if is_challenge(r.status_code, text) else ""))
            return FetchResult(r.status_code, text, str(r.url), blocked=True)
        d.consecutive_blocks = 0
        path = self._snapshot(domain, url, text)
        return FetchResult(r.status_code, text, str(r.url), raw_path=path)
