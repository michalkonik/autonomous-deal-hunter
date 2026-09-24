"""Bazoš (cz / sk / at).

robots.txt disallows search (`/search.php`, `*hledat=`, `*cenaod=`, `*order=` ...) on every Bazoš domain, so the
search form is NEVER used. Category listing pages (`https://pc.bazos.cz/graficka/`, `/graficka/20/` ...) are
allowed, so the adapter reads the newest ads of the category that matches the query and filters them locally
against the query words. Only the first `pages` pages (20 ads each, newest first) are read.
"""
from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal

from selectolax.lexbor import LexborHTMLParser

from ..models import Listing
from .base import (Adapter, AdapterError, Query, abs_url, in_range, kinds_for, local_dt, matches_query, norm,
                   price_of, seller_hash)

CARD = "div.inzeraty"
ID_RE = re.compile(r"/(?:inzerat|anzeige)/(\d+)/")
DATE_RE = re.compile(r"\[(\d{1,2})\.(\d{1,2})\.\s*(\d{4})\]")
RATING_RE = re.compile(r"odeslatakci\('rating','(\d+)'")

COUNTRIES = {
    "cz": dict(currency="CZK", default="pc", tz="Europe/Prague", sections=dict(
        gpu="graficka", laptop="notebook", cpu="procesor", ram="pamet", disk="hdd", monitor="monitor",
        console="playstation", tablet="tablet")),
    "sk": dict(currency="EUR", default="pc", tz="Europe/Bratislava", sections=dict(
        gpu="graficka", laptop="notebook", cpu="procesor", ram="pamet", disk="hdd", monitor="monitor",
        console="playstation", tablet="tablet")),
    "at": dict(currency="EUR", default="computer", tz="Europe/Vienna", sections=dict(
        gpu="grafikkarten", laptop="notebooks", cpu="prozessoren", ram="speicher", disk="festplatten",
        monitor="lcd", console="konsolen", tablet="tablet")),
}


def sections_for(country: str, q: Query) -> list[str]:
    cfg = COUNTRIES[country]
    return [cfg["sections"][k] for k in kinds_for(q) if k in cfg["sections"]] or [cfg["default"]]


class Bazos(Adapter):
    def __init__(self, country: str = "cz", pages: int = 2):
        if country not in COUNTRIES:
            raise ValueError(country)
        self.country = country
        self.name = f"bazos_{country}"
        self.domain = f"pc.bazos.{country}"
        self.currency = COUNTRIES[country]["currency"]
        self.pages = pages
        self.tz = COUNTRIES[country]["tz"]
        self.gone_markers = ("inzerát již není", "inzerát byl smazán", "inzerát už nie je", "inzerát bol zmazaný",
                             "anzeige ist nicht mehr", "anzeige wurde gelöscht")

    def page_urls(self, q: Query) -> list[str]:
        urls = []
        for sec in sections_for(self.country, q):
            for p in range(self.pages):
                urls.append(f"https://{self.domain}/{sec}/" + (f"{20 * p}/" if p else ""))
        return urls

    async def search(self, query: Query, fetcher) -> list[Listing]:
        out: dict[str, Listing] = {}
        errors = 0
        for url in self.page_urls(query):
            res = await self._get(fetcher, url)
            if res.status != 200:
                errors += 1
                continue
            for l in parse_search(res.text, self, query):
                out.setdefault(l.source_id, l)
        if errors and not out and errors == len(self.page_urls(query)):
            raise AdapterError(f"{self.name}: every category page failed")
        return list(out.values())

    def parse_detail(self, listing: Listing, html: str) -> Listing:
        tree = LexborHTMLParser(html)
        d = tree.css_first(".popisdetail")
        if d is not None:
            listing.description = norm(d.text(separator="\n"))
        photos = []
        for im in tree.css(".carousel-cell-image"):
            src = im.attributes.get("src") or im.attributes.get("data-flickity-lazyload")
            if src:
                photos.append(src)
        if photos:
            listing.photos = list(dict.fromkeys(photos))
        m = RATING_RE.search(html)
        if m:
            listing.seller_hash = seller_hash(listing.source, m.group(1))
        loc = tree.css_first(".listadvlevo")
        if loc is not None:
            m = re.search(r"(?:Lokalita|Lokalita|Ort|Standort)\s*:\s*(.+?)(?:Vid[ěe]lo|Videlo|Gesehen|Cena|Preis|$)",
                          norm(loc.text(separator=" ")))
            if m:
                listing.city = norm(m.group(1))
        return listing

    def detail_price(self, html: str) -> Decimal | None:
        loc = LexborHTMLParser(html).css_first(".listadvlevo")
        if loc is None:
            return None
        m = re.search(r"(?:Cena|Cena|Preis)\s*:\s*(.+)$", norm(loc.text(separator=" ")))
        pr = price_of(m.group(1) if m else "", self.currency)
        return pr[0] if pr else None


def parse_search(html: str, adapter: Bazos, q: Query | None = None) -> list[Listing]:
    tree = LexborHTMLParser(html)
    out: list[Listing] = []
    for card in tree.css(CARD):
        a = card.css_first("h2.nadpis a")
        if a is None:
            continue
        href = a.attributes.get("href") or ""
        m = ID_RE.search(href)
        if not m:
            continue
        pn = card.css_first(".inzeratycena")
        pr = price_of(pn.text() if pn is not None else "", adapter.currency)  # "Dohodou", "V textu" -> None
        if pr is None:
            continue
        dn = card.css_first(".velikost10")
        dm = DATE_RE.search(dn.text()) if dn is not None else None
        posted = None
        if dm:
            try:
                posted = local_dt(datetime(int(dm.group(3)), int(dm.group(2)), int(dm.group(1))), adapter.tz)
            except ValueError:
                pass
        loc = card.css_first(".inzeratylok")
        city = norm(loc.text(separator=" ")) if loc is not None else None
        rm = RATING_RE.search(card.html)
        desc = card.css_first(".popis")
        img = card.css_first("img.obrazek")
        l = adapter.make(source_id=m.group(1), url=abs_url(f"https://{adapter.domain}", href), title=norm(a.text()),
                         description=norm(desc.text()) if desc is not None else "", price=pr[0], currency=pr[1],
                         city=city, seller_hash=seller_hash(adapter.name, rm.group(1)) if rm else None,
                         photos=[img.attributes["src"]] if img is not None and img.attributes.get("src") else [],
                         posted_at=posted)
        if (q is None or (in_range(l.price, l.currency, q) and matches_query(l, q))):
            out.append(l)
    return out
