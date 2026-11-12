"""Polite per-domain fetcher: plain httpx, honest UA, robots.txt, backoff, gzip snapshots.

No impersonation, no proxies, no challenge solving.

- Per-domain lock; `min_interval_s` + 0..`jitter_s` between requests (raised to the site's Crawl-delay).
- Block (403/429 or challenge page): backoff 60 s doubling, cap 1 h; 3 consecutive blocks disable the
  domain for the session. 5xx / network errors are soft failures: backoff 60 s doubling, cap 15 min,
  never disable.
- Block state and robots rules are re-checked after acquiring the domain lock, so concurrent callers
  cannot all hit a domain that the first one just found blocked.
- robots.txt: own RFC 9309-style matcher (UA group selection, `*`, `$`, longest match, Allow wins ties).
- Redirects are followed manually (max 5); every hop re-runs robots, pacing and block state for its host.
- `snapshot_dir` is the project var root; snapshots are written to
  `<snapshot_dir>/snapshots/YYYY-MM-DD/<domain>/<sha1(url)>.gz`. Only 2xx/3xx-final bodies that are not
  404/5xx/blocked are stored. `purge_snapshots(older_than_days)` deletes old day directories.
"""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import random
import re
import shutil
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal
from urllib.parse import urljoin, urlsplit

import httpx

DEFAULT_UA = "DealHunter/0.1 (personal price research; +https://github.com/michalkonik)"
CHALLENGE_MARKERS = ("cf-chl", "just a moment", "datadome", "captcha-delivery", "_incapsula_")
ROBOTS_FAIL_TTL_S = 300
SOFT_CAP_S = 900
MAX_REDIRECTS = 5
HEAD_BYTES = 4096


@dataclass
class FetchResult:
    status: int
    text: str
    url: str
    from_cache: bool = False
    raw_path: Path | None = None
    blocked: bool = False
    disallowed: bool = False
    content: bytes = b""  # raw body, filled only by `get_bytes`


def is_challenge(status: int, text: str) -> bool:
    low = text.lower()
    if status == 200 and len(low) > 50_000:
        # big real pages may embed vendor scripts: only trust markers in <title> or the first 4 KB
        m = re.search(r"<title[^>]*>(.*?)</title>", low, re.S)
        hay = low[:HEAD_BYTES] + " " + (m.group(1) if m else "")
    else:
        hay = low
    if any(mk in hay for mk in CHALLENGE_MARKERS):
        return True
    return status == 403 and "akamai" in low


# --- robots.txt matcher ----------------------------------------------------
class RobotsRules:
    def __init__(self, text: str, ua: str = DEFAULT_UA):
        groups: list[dict] = []
        cur: dict | None = None
        seen_rule = False
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            k, v = (x.strip() for x in line.split(":", 1))
            k = k.lower()
            if k == "user-agent":
                if cur is None or seen_rule:
                    cur = {"agents": [], "rules": [], "delay": None}
                    groups.append(cur)
                    seen_rule = False
                cur["agents"].append(v.lower())
            elif k in ("allow", "disallow") and cur is not None:
                seen_rule = True
                if v:  # empty Disallow = allow all; empty Allow = no-op
                    cur["rules"].append((k == "allow", v))
            elif k == "crawl-delay" and cur is not None:
                seen_rule = True
                try:
                    cur["delay"] = max(cur["delay"] or 0.0, float(v))
                except ValueError:
                    pass
        ua_l = ua.lower()
        token = ua_l.split("/")[0].strip()  # product token only, e.g. "dealhunter"
        specific = [g for g in groups if token in g["agents"]]
        sel = specific or [g for g in groups if "*" in g["agents"]]
        self.rules: list[tuple[bool, str, re.Pattern]] = []
        self.crawl_delay = 0.0
        for g in sel:
            for allow, pat in g["rules"]:
                self.rules.append((allow, pat, self._compile(pat)))
            if g["delay"]:
                self.crawl_delay = max(self.crawl_delay, g["delay"])

    @staticmethod
    def _compile(pat: str) -> re.Pattern:
        anchored = pat.endswith("$")
        if anchored:
            pat = pat[:-1]
        rx = ".*".join(re.escape(p) for p in pat.split("*"))
        return re.compile(rx + ("$" if anchored else ""), re.S)

    def allows(self, path_query: str) -> bool:
        best_len, best_allow = -1, True
        for allow, pat, rx in self.rules:
            if rx.match(path_query):
                n = len(pat)
                if n > best_len or (n == best_len and allow):
                    best_len, best_allow = n, allow
        return best_allow


@dataclass
class _Robots:
    expires: float
    rules: RobotsRules | None
    ok: bool  # False = could not load -> disallow everything


@dataclass
class _Out:
    kind: str  # response | skipped | disallowed | error
    resp: httpx.Response | None = None
    block: bool = False


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


_SAFE_CROSS_HOST = {"accept", "accept-language"}


def _scope_headers(h: dict) -> dict:
    """Drop credentials (Authorization, Cookie, Proxy-Authorization, *api-key*, *token*) on a cross-host hop."""
    out = {}
    for k, v in h.items():
        kl = k.lower()
        if kl in _SAFE_CROSS_HOST:
            out[k] = v
        elif kl in ("authorization", "cookie", "proxy-authorization") or any(
                x in kl for x in ("api-key", "apikey", "token")):
            continue
        else:
            out[k] = v
    return out


def _pq(url: str) -> str:
    p = urlsplit(url)
    return (p.path or "/") + ("?" + p.query if p.query else "")


class _Dom:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.last_request: float | None = None
        self.blocked_until = 0.0
        self.soft_until = 0.0
        self.consecutive_blocks = 0
        self.soft_failures = 0
        self.disabled = False
        self.crawl_delay = 0.0


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
        self.max_crawl_delay = float(_opt(c, "max_crawl_delay_s", 60))
        timeout = float(_opt(c, "timeout_s", 30))
        self.store = store
        self.snapshot_dir = Path(snapshot_dir)
        self.clock = clock or time.monotonic
        self.sleep = sleep or asyncio.sleep
        self.http = http or httpx.AsyncClient(timeout=timeout)
        self._doms: dict[str, _Dom] = {}
        self._robots: dict[str, _Robots] = {}
        self._robots_locks: dict[str, asyncio.Lock] = {}

    # --- state -------------------------------------------------------------
    def _dom(self, domain: str) -> _Dom:
        return self._doms.setdefault(domain, _Dom())

    def blocked(self, domain: str) -> bool:
        d = self._dom(domain)
        return d.disabled or self.clock() < max(d.blocked_until, d.soft_until)

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

    def _register_soft(self, domain: str, why: str) -> None:
        d = self._dom(domain)
        d.soft_failures += 1
        delay = min(self.backoff_start * 2 ** (d.soft_failures - 1), SOFT_CAP_S)
        d.soft_until = self.clock() + delay
        self._health(domain, "soft_backoff", f"{why}; backoff {delay:.0f}s; failures={d.soft_failures}")

    async def pace(self, domain: str) -> bool:
        """For callers that load a page outside `get` (headless screenshots): wait out the domain's pacing
        interval and count it as a request. False when the domain is blocked or disabled (do not load)."""
        d = self._dom(domain)
        async with d.lock:
            if self.blocked(domain):
                return False
            if d.last_request is not None:
                wait = (d.last_request + max(self.min_interval, d.crawl_delay)
                        + random.uniform(0, self.jitter) - self.clock())
                if wait > 0:
                    await self.sleep(wait)
            d.last_request = self.clock()
            return True

    def report_block(self, domain: str, why: str) -> None:
        """A block (403/429/challenge) seen outside `get`: same backoff and health bookkeeping."""
        self._register_block(domain, why)

    def report_ok(self, domain: str) -> None:
        """A good page load outside `get` (a paced screenshot): resets the block and soft-failure counts exactly as
        a good response in `get` does. A running backoff is not shortened."""
        d = self._dom(domain)
        d.consecutive_blocks = 0
        d.soft_failures = 0

    # --- the one place a request is made ------------------------------------
    async def _locked_request(self, domain: str, url: str, headers: dict | None, *, robots: bool = False) -> _Out:
        d = self._dom(domain)
        h = {"User-Agent": self.user_agent}
        h.update(headers or {})
        async with d.lock:
            # re-check state now that we hold the lock
            if self.blocked(domain):
                return _Out("skipped")
            if not robots:
                ent = self._robots.get(domain)
                if ent is not None and not self._rules_allow(ent, url):
                    return _Out("disallowed")
            if d.last_request is not None:
                interval = max(self.min_interval, d.crawl_delay)
                wait = d.last_request + interval + random.uniform(0, self.jitter) - self.clock()
                if wait > 0:
                    await self.sleep(wait)
            d.last_request = self.clock()
            try:
                r = await self.http.get(url, headers=h, follow_redirects=False)
            except (httpx.HTTPError, httpx.InvalidURL) as e:
                d.last_request = self.clock()
                self._health(domain, "fetch_error", f"{type(e).__name__}: {e}")
                self._register_soft(domain, "network error")
                return _Out("error")
            d.last_request = self.clock()
            text = r.text
            chal = is_challenge(r.status_code, text)
            if r.status_code in (403, 429) or chal:
                self._register_block(domain, f"status {r.status_code}" + (" challenge page" if chal else ""))
                return _Out("response", r, block=True)
            if r.status_code >= 500:
                self._register_soft(domain, f"status {r.status_code}")
            else:
                d.consecutive_blocks = 0
                d.soft_failures = 0
            return _Out("response", r)

    # --- robots ------------------------------------------------------------
    @staticmethod
    def _rules_allow(ent: _Robots, url: str) -> bool:
        if not ent.ok:
            return False
        return ent.rules is None or ent.rules.allows(_pq(url))

    async def _robots_for(self, scheme: str, domain: str) -> _Robots:
        ent = self._robots.get(domain)
        if ent is not None and self.clock() < ent.expires:
            return ent
        lock = self._robots_locks.setdefault(domain, asyncio.Lock())
        async with lock:  # coalesce concurrent loads
            ent = self._robots.get(domain)
            if ent is not None and self.clock() < ent.expires:
                return ent
            ent = await self._load_robots(scheme, domain)
            if ent is None:  # domain blocked right now: do not cache
                return _Robots(self.clock(), None, False)
            self._robots[domain] = ent
            return ent

    async def _load_robots(self, scheme: str, domain: str) -> _Robots | None:
        now = self.clock()
        cur, host = f"{scheme}://{domain}/robots.txt", domain
        out = _Out("error")
        for _ in range(MAX_REDIRECTS + 1):
            out = await self._locked_request(host, cur, None, robots=True)
            r = out.resp
            if out.kind == "skipped":
                return None
            if r is not None and not out.block and 300 <= r.status_code < 400 and r.headers.get("location"):
                cur = urljoin(cur, r.headers["location"])
                host = urlsplit(cur).netloc.lower()
                continue
            break
        r = out.resp
        fail = _Robots(now + ROBOTS_FAIL_TTL_S, None, False)
        if out.kind != "response" or r is None:
            self._health(domain, "robots_error", "robots.txt unreachable; treating as disallowed")
            return fail
        if out.block:
            self._health(domain, "robots_error", f"robots.txt blocked ({r.status_code})")
            return fail
        if r.status_code == 404:
            return _Robots(now + self.robots_ttl, None, True)
        if r.status_code == 200:
            rules = RobotsRules(r.text, self.user_agent)
            self._dom(domain).crawl_delay = min(rules.crawl_delay, self.max_crawl_delay)
            return _Robots(now + self.robots_ttl, rules, True)
        self._health(domain, "robots_error", f"robots.txt status {r.status_code}; treating as disallowed")
        return fail

    async def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        domain = parts.netloc.lower()
        ent = await self._robots_for(parts.scheme or "https", domain)
        return self._rules_allow(ent, url)

    # --- snapshots ---------------------------------------------------------
    def _write_snapshot(self, domain: str, url: str, text: str) -> Path:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        sha = hashlib.sha1(url.encode()).hexdigest()
        p = self.snapshot_dir / "snapshots" / day / domain.replace(":", "_") / f"{sha}.gz"
        p.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(p, "wt", encoding="utf-8") as f:
            f.write(text)
        return p

    def purge_snapshots(self, older_than_days: int, today: date | None = None) -> int:
        """Delete snapshot day-directories older than N days. Returns number of files removed."""
        root = self.snapshot_dir / "snapshots"
        if not root.is_dir():
            return 0
        cutoff = (today or datetime.now(timezone.utc).date()) - timedelta(days=older_than_days)
        removed = 0
        for day_dir in root.iterdir():
            try:
                day = date.fromisoformat(day_dir.name)
            except ValueError:
                continue
            if day_dir.is_dir() and day < cutoff:
                removed += sum(1 for f in day_dir.rglob("*") if f.is_file())
                shutil.rmtree(day_dir)
        return removed

    # --- get ---------------------------------------------------------------
    async def get(self, url: str, *, kind: Literal["html", "json"], headers: dict | None = None) -> FetchResult:
        h = dict(headers or {})
        h.setdefault("Accept", "application/json" if kind == "json" else "text/html,application/xhtml+xml")
        return await self._follow(url, h, binary=False)

    async def get_bytes(self, url: str, *, headers: dict | None = None, max_bytes: int = 5_000_000) -> FetchResult:
        """Same etiquette as `get` (robots.txt, pacing, block backoff, redirects) for a binary body (a photo).
        `content` holds the body (empty when it is larger than `max_bytes`); nothing is written to the snapshot dir."""
        h = dict(headers or {})
        h.setdefault("Accept", "image/*")
        res = await self._follow(url, h, binary=True)
        if len(res.content) > max_bytes:
            res.content = b""
        return res

    async def _follow(self, url: str, h: dict, *, binary: bool) -> FetchResult:
        cur = url
        for _ in range(MAX_REDIRECTS + 1):
            res, nxt = await self._get_hop(cur, h, binary)
            if nxt is None:
                return res
            if urlsplit(nxt).netloc.lower() != urlsplit(cur).netloc.lower():
                h = _scope_headers(h)  # cross-host: never forward credentials
            cur = nxt
        host = urlsplit(cur).netloc.lower()
        self._health(host, "redirect_loop", f"more than {MAX_REDIRECTS} redirects from {url}")
        return FetchResult(0, "", cur)

    async def _get_hop(self, url: str, headers: dict, binary: bool = False) -> tuple[FetchResult, str | None]:
        try:
            parts = urlsplit(url)
            domain = parts.netloc.lower()
        except ValueError:
            return FetchResult(0, "", url), None
        if parts.scheme not in ("http", "https") or not domain:
            self._health(domain or "?", "fetch_error", f"unsupported URL {url}")
            return FetchResult(0, "", url), None
        if self.blocked(domain):
            return FetchResult(0, "", url, blocked=True), None
        ent = await self._robots_for(parts.scheme, domain)
        if self.blocked(domain):
            return FetchResult(0, "", url, blocked=True), None
        if not self._rules_allow(ent, url):
            self._health(domain, "robots_disallow", url)
            return FetchResult(0, "", url, disallowed=True), None
        out = await self._locked_request(domain, url, headers)
        if out.kind == "skipped":
            return FetchResult(0, "", url, blocked=True), None
        if out.kind == "disallowed":
            self._health(domain, "robots_disallow", url)
            return FetchResult(0, "", url, disallowed=True), None
        if out.kind == "error" or out.resp is None:
            return FetchResult(0, "", url), None
        r = out.resp
        if out.block:
            return FetchResult(r.status_code, r.text, url, blocked=True), None
        loc = r.headers.get("location")
        if 300 <= r.status_code < 400 and loc:
            return FetchResult(r.status_code, "", url), urljoin(url, loc)
        if binary:
            return FetchResult(r.status_code, "", url, content=r.content if r.status_code == 200 else b""), None
        text = r.text
        path = None
        if r.status_code < 400 or (r.status_code < 500 and r.status_code != 404):
            try:
                path = await asyncio.to_thread(self._write_snapshot, domain, url, text)
            except OSError as e:
                self._health(domain, "snapshot_error", f"{type(e).__name__}: {e}")
        return FetchResult(r.status_code, text, url, raw_path=path), None
