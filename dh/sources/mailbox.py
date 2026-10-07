"""Alert mailbox: platform saved-search e-mails (IMAP over SSL) as a listing source.

This is the legitimate channel for platforms that block polite polling (Allegro.pl, OLX): the user creates saved
searches on the platform, the platform e-mails the new offers, and we parse the mail. For Allegro, OLX and eBay NO
detail page is ever fetched (only the e-mail data plus the link); Kleinanzeigen (whose adapter is allowed by
robots.txt) is enriched through the polite Fetcher when one is passed. Failure to enrich never drops the listing.

Credentials: `bw-secret dh-mail#imap` = JSON {"host","port","user","password","folder"}. Missing or malformed
-> the source is disabled and ONE health event is recorded. Mails are fetched with BODY.PEEK and flagged \\Seen only
after they were parsed; mails from unknown senders are left untouched.

The alert formats here are built from each platform's typical structure (see tests/fixtures/mailbox/README.md,
SYNTHETIC): re-check them against real mails once they exist.
"""
from __future__ import annotations

import asyncio
import email
import hashlib
import imaplib
import json
import logging
import re
from datetime import timezone
from decimal import Decimal
from email import policy
from email.message import EmailMessage
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from selectolax.lexbor import LexborHTMLParser

from ..models import Listing
from .base import Adapter, AdapterError, Query, filter_query, norm, now_utc, price_of

log = logging.getLogger(__name__)

SECRET_REF = "dh-mail#imap"
_AMOUNT = r"(?:\d{1,3}(?:[.\s\xa0 ]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)"
_CCY = r"(?:zł|zl|PLN|€|EUR|Kč|CZK|Ft|HUF|£|GBP|\$|USD)"
_PRICE_AFTER = re.compile(rf"({_AMOUNT})\s*({_CCY})(?![A-Za-zł])", re.I)
_PRICE_BEFORE = re.compile(rf"({_CCY})\s*({_AMOUNT})", re.I)


class Platform:
    def __init__(self, source: str, country: str, currency: str, link_rx: str, id_rx: str, domains: tuple[str, ...]):
        self.source, self.country, self.currency = source, country, currency
        self.link, self.id, self.domains = re.compile(link_rx), re.compile(id_rx), domains


PLATFORMS = {
    "allegro": Platform("allegro", "PL", "PLN", r"^https://(?:www\.)?allegro\.pl/oferta/", r"-(\d{8,})$",
                        ("allegro.pl",)),
    "olx": Platform("olx", "PL", "PLN", r"^https://(?:www\.)?olx\.pl/d/oferta/", r"-ID([A-Za-z0-9]+)\.html$",
                    ("olx.pl",)),
    "kleinanzeigen": Platform("kleinanzeigen", "DE", "EUR", r"^https://(?:www\.)?kleinanzeigen\.de/s-anzeige/",
                              r"/(\d{6,})-\d+-\d+$", ("kleinanzeigen.de",)),
    "ebay": Platform("ebay_de", "DE", "EUR", r"^https://(?:www\.)?ebay\.(?:de|pl)/itm/", r"/itm/(?:[^/]*/)?(\d{9,})$",
                     ("ebay.de", "ebay.pl")),
}
#: platforms for which a detail fetch is never made (blocked, or API-only by policy)
NO_FETCH = {"allegro", "olx", "ebay"}


def platform_of(sender: str) -> str | None:
    dom = parseaddr(sender)[1].rsplit("@", 1)[-1].lower()
    for name, p in PLATFORMS.items():
        if any(dom == d or dom.endswith("." + d) for d in p.domains):
            return name
    return None


def _html_of(msg: EmailMessage) -> str:
    part = msg.get_body(preferencelist=("html",))
    if part is not None:
        return part.get_content()
    part = msg.get_body(preferencelist=("plain",))
    return part.get_content() if part is not None else ""


def _price_in(text: str, default_ccy: str) -> tuple[Decimal, str] | None:
    m = _PRICE_AFTER.search(text)
    chunk = f"{m.group(1)} {m.group(2)}" if m else None
    if chunk is None:
        m = _PRICE_BEFORE.search(text)
        chunk = f"{m.group(2)} {m.group(1)}" if m else None
    return price_of(chunk, default_ccy) if chunk else None


def _container_text(a, title: str, sid_of: Callable[[str], str | None]) -> tuple[str, object]:
    """Text around an offer link: climb until it holds a price, but never into a node holding another offer."""
    node, best = a, a
    mine = sid_of(a.attributes.get("href") or "")
    for _ in range(5):
        parent = node.parent
        if parent is None or parent.tag in ("body", "html", "-undef"):
            break
        others = {sid_of(x.attributes.get("href") or "") for x in parent.css("a[href]")} - {None, mine}
        if others:
            break
        node = best = parent
        if _price_in(node.text(separator=" ").replace(title, " "), "PLN"):
            break
    return best.text(separator=" "), best


def parse_message(msg: EmailMessage, pf_name: str) -> list[Listing]:
    """Pure: one alert e-mail -> listings (price > 0, deduplicated by id)."""
    pf = PLATFORMS[pf_name]
    try:
        posted = parsedate_to_datetime(msg["Date"]).astimezone(timezone.utc) if msg["Date"] else None
    except (TypeError, ValueError):
        posted = None
    tree = LexborHTMLParser(_html_of(msg) or "<html></html>")
    ad = _Maker()

    def clean(href: str) -> str:
        s = urlsplit(href.replace("&amp;", "&"))
        return f"{s.scheme}://{s.netloc}{s.path}"

    def sid_of(href: str) -> str | None:
        u = clean(href)
        if not pf.link.match(u):
            return None
        m = pf.id.search(u)
        return m.group(1) if m else None

    groups: dict[str, list] = {}
    for a in tree.css("a[href]"):
        sid = sid_of(a.attributes.get("href") or "")
        if sid:
            groups.setdefault(sid, []).append(a)
    out: list[Listing] = []
    for sid, anchors in groups.items():
        titles = [norm(a.text(separator=" ")) for a in anchors]
        for a in anchors:  # image-only link: use the alt text
            img = a.css_first("img")
            if img is not None and img.attributes.get("alt"):
                titles.append(norm(img.attributes["alt"]))
        titles = [t for t in titles if t]
        title = max(titles, key=len, default="")
        if not title:
            continue
        text, node = _container_text(anchors[-1], title, sid_of)
        rest = norm(text).replace(title, " ")
        for t in titles:
            rest = rest.replace(t, " ")
        sf = _price_in(rest, pf.currency)
        if sf is None:  # "do negocjacji", "Zu verschenken", no amount: skip
            continue
        src = pf.source
        url = clean(anchors[0].attributes["href"])
        if pf_name == "ebay":
            src, country = ("ebay_pl", "PL") if ".ebay.pl" in url or "//ebay.pl" in url else ("ebay_de", "DE")
        else:
            country = pf.country
        img, probe = None, node
        for _ in range(3):  # the picture may sit in a sibling cell: widen while no other offer comes in
            img = next((i.attributes.get("src") for i in probe.css("img") if i.attributes.get("src")), None)
            par = probe.parent
            if img or par is None or par.tag in ("body", "html", "-undef") or \
                    ({sid_of(x.attributes.get("href") or "") for x in par.css("a[href]")} - {None, sid}):
                break
            probe = par
        out.append(ad.make(source=src, source_id=sid, url=url, title=title, price=sf[0], currency=sf[1],
                           country=country, photos=[img] if img else [], posted_at=posted))
    return out


class _Maker(Adapter):
    name = "mailbox"


class Mailbox(Adapter):
    """Source "mailbox": `poll()` returns the listings of all unseen alert mails; `search()` is `poll()` with the
    query's price bounds (the query text is irrelevant: the saved searches on the platforms define it)."""
    name = "mailbox"
    domain = "imap"
    via_mailbox = True

    def __init__(self, secret_fn: Callable[[str], str | None] | None = None, snapshot_dir: Path | str | None = None,
                 imap_factory: Callable[[str, int], object] | None = None, store=None):
        self._secret_fn = secret_fn
        self.snapshot_dir = Path(snapshot_dir) if snapshot_dir else None
        self._factory = imap_factory or (lambda host, port: imaplib.IMAP4_SSL(host, port, timeout=30))
        self.store = store
        self._creds: dict | None | bool = False
        self._health_done = False

    # ---------------------------------------------------------------- credentials
    def _resolve(self) -> dict | None:
        if self._creds is False:
            fn = self._secret_fn
            if fn is None:
                from ..notify import bw_secret as fn
            self._creds = None
            raw = fn(SECRET_REF)
            if raw:
                try:
                    c = json.loads(raw)
                    if all(c.get(k) for k in ("host", "user", "password")):
                        self._creds = {"host": c["host"], "port": int(c.get("port") or 993), "user": c["user"],
                                       "password": c["password"], "folder": c.get("folder") or "INBOX"}
                except (ValueError, TypeError, AttributeError):
                    self._creds = None
        return self._creds or None

    @property
    def enabled(self) -> bool:
        return self._resolve() is not None

    def _health(self, kind: str, detail: str, fetcher=None) -> None:
        store = self.store or getattr(fetcher, "store", None)
        if store is not None:
            store.add_health(self.name, kind, detail)

    # ---------------------------------------------------------------- IMAP (blocking, run in a thread)
    def _fetch_unseen(self, c: dict) -> list[tuple[bytes, Callable[[], None]]]:
        """[(raw message, mark_seen callable)] for every UNSEEN mail of the folder."""
        imap = self._factory(c["host"], c["port"])
        try:
            imap.login(c["user"], c["password"])
            typ, _ = imap.select(c["folder"])
            if typ != "OK":
                raise AdapterError(f"mailbox: cannot select folder {c['folder']!r}")
            typ, data = imap.search(None, "UNSEEN")
            ids = data[0].split() if typ == "OK" and data and data[0] else []
            out = []
            for i in ids:
                typ, parts = imap.fetch(i, "(BODY.PEEK[])")
                if typ != "OK" or not parts:
                    continue
                raw = next((p[1] for p in parts if isinstance(p, tuple)), None)
                if raw:
                    out.append((raw, i))
            # parse and mark outside the connection loop would need the connection: do both here
            return self._handle(imap, out)
        finally:
            try:
                imap.logout()
            except Exception:  # noqa: BLE001 - best effort close
                pass

    def _handle(self, imap, raws) -> list:
        results = []
        for raw, i in raws:
            msg = email.message_from_bytes(raw, policy=policy.default)
            pf = platform_of(msg["From"] or "")
            if pf is None:
                continue  # not an alert we know: leave it unseen and untouched
            try:
                ls = parse_message(msg, pf)
            except Exception as e:  # noqa: BLE001 - one bad mail must not block the rest
                log.warning("mailbox: cannot parse a %s mail: %s", pf, e)
                continue
            path = self._save_eml(raw)
            for l in ls:
                l.raw_path = path
            imap.store(i, "+FLAGS", "\\Seen")
            results.append((pf, ls))
        return results

    def _save_eml(self, raw: bytes) -> str | None:
        if self.snapshot_dir is None:
            return None
        d = self.snapshot_dir / "mail"
        d.mkdir(parents=True, exist_ok=True)
        p = d / (hashlib.sha256(raw).hexdigest()[:16] + ".eml")
        if not p.exists():
            p.write_bytes(raw)
        return str(p)

    # ---------------------------------------------------------------- public
    async def poll(self, fetcher=None) -> list[Listing]:
        c = self._resolve()
        if c is None:
            if not self._health_done:
                self._health_done = True
                self._health("no_credentials", f"alert mailbox disabled: no credentials (bw-secret {SECRET_REF})", fetcher)
            return []
        try:
            groups = await asyncio.to_thread(self._fetch_unseen, c)
        except AdapterError:
            raise
        except (imaplib.IMAP4.error, OSError) as e:
            raise AdapterError(f"mailbox: IMAP error: {type(e).__name__}") from e
        found: dict[tuple[str, str], Listing] = {}
        for pf, ls in groups:
            for l in ls:
                if fetcher is not None and pf not in NO_FETCH:
                    l = await self._enrich(l, fetcher)
                found.setdefault((l.source, l.source_id), l)
        return list(found.values())

    async def _enrich(self, l: Listing, fetcher) -> Listing:
        if l.source != "kleinanzeigen":
            return l
        from .kleinanzeigen import Kleinanzeigen
        try:
            full = await Kleinanzeigen().detail(l, fetcher)
        except AdapterError as e:
            log.info("mailbox: no detail for %s: %s", l.url, e)
            return l
        full.raw_path = l.raw_path
        return full

    async def search(self, query: Query, fetcher=None, to_pln=None) -> list[Listing]:
        return filter_query(await self.poll(fetcher), Query(text="", min_price_pln=query.min_price_pln,
                                                            max_price_pln=query.max_price_pln),
                            to_pln or self.to_pln)

    def parse_detail(self, listing: Listing, html: str) -> Listing:
        return listing

    async def alive(self, listing: Listing, fetcher):
        raise AdapterError(f"mailbox: {listing.source} listings cannot be re-verified (no detail fetch for mailbox sources)")
