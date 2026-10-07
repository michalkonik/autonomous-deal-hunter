"""Capture trimmed live fixtures for a source adapter, politely (Fetcher: robots.txt, pacing, backoff).

    python tests/fixtures/capture.py <source> <query> [--detail-url URL]

Writes tests/fixtures/<source>/search.html (first search/category page) and detail.html (first parsed
listing, or --detail-url). Scripts, styles, SVGs and comments are stripped (≤ 200 KB per file); the few
inline-script facts the adapters read are preserved. Never run against sites whose robots.txt forbids it:
the Fetcher refuses those URLs.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from selectolax.lexbor import LexborHTMLParser  # noqa: E402

from dh.config import load  # noqa: E402
from dh.fetch import Fetcher  # noqa: E402
from dh.sources import registry  # noqa: E402
from dh.sources.base import Query  # noqa: E402
from dh.store import Store  # noqa: E402

LIMIT = 150_000
KEEP_INLINE = [r'"posterid":"\d+"', r'"Verkaeufer":"\w+"', r'"versand":"\w+"', r'"Angebotstyp":"\w+"']

KEEP_ATTRS = {"class", "id", "href", "src", "title", "datetime", "content", "itemprop", "data-adid", "data-href",
              "data-uadid", "data-card-analytics-click", "data-retina-url", "data-flickity-lazyload",
              "data-mlc-seller-details", "data-mlc-param", "onclick", "name", "property", "type"}
# search pages: keep only the result cards (wrapped in `shell`); detail pages: keep only these regions
SEARCH = {  # source -> (card selector, shell open, shell close, max cards)
    "allegro_lokalnie": ("article.mlc-itembox__container", "", "", 30),
    "sprzedajemy": ("article.element", "", "", 30),
    "kleinanzeigen": ("#srchrslt-adtable > li", '<ul id="srchrslt-adtable">', "</ul>", 30),
    "hardverapro": ("li.media[data-uadid]", "<ul>", "</ul>", 40),
    "bazos_cz": ("div.inzeraty", "", "", 20), "bazos_sk": ("div.inzeraty", "", "", 20),
    "bazos_at": ("div.inzeraty", "", "", 20),
}
DETAIL_REGIONS = {
    "allegro_lokalnie": ["meta[itemprop=price]", ".mlc-offer__description", "tr[data-mlc-param]",
                         ".mlc-offer__gallery", "[data-mlc-seller-details]", ".mlc-delivery-options__row", "title"],
}


def _clean(node) -> str:
    for n in node.css("script,style,svg,noscript,link,iframe,template"):
        if n.tag == "script" and n.attributes.get("type") == "application/ld+json":
            continue
        n.decompose()
    for n in node.css("*"):
        for k in list(n.attributes):
            if k not in KEEP_ATTRS:
                del n.attrs[k]
    return re.sub(r"\s+", " ", node.html)


def trim(html: str, source: str, limit: int = LIMIT, kind: str = "search") -> str:
    inline = [m.group(0) for rx in KEEP_INLINE for m in [re.search(rx, html)] if m]
    html = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    tree = LexborHTMLParser(html)
    if kind == "search" and source in SEARCH:
        sel, op, cl, mx = SEARCH[source]
        cards = tree.css(sel)[:mx]
        parts = [_clean(c) for c in cards]
        out = op + "".join(parts) + cl
        while len(out) > limit and len(parts) > 12:
            parts.pop()
            out = op + "".join(parts) + cl
        return f"<html><body>{out}</body></html>"
    if kind == "detail" and source in DETAIL_REGIONS:
        parts = []
        for sel in DETAIL_REGIONS[source]:
            for n in tree.css(sel):
                parts.append(_clean(n) if n.tag != "meta" else n.html)
        return "<html><head>" + "".join(p for p in parts if p.startswith(("<meta", "<title"))) + "</head><body>" + \
            "".join(p for p in parts if not p.startswith(("<meta", "<title"))) + "</body></html>"
    out = _clean(tree.root)
    if inline:
        out = out.replace("</body>", f"<script>{' '.join(inline)}</script></body>")
    return out


def trim_marktplaats(html: str, limit: int = LIMIT) -> str:
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return trim(html, "marktplaats", limit)
    listings = json.loads(m.group(1))["props"]["pageProps"]["searchRequestAndResponse"]["listings"]
    for it in listings:  # keep what the adapter reads
        for k in ("pictures", "extendedAttributes", "traits", "verticals"):
            it.pop(k, None)
    while True:
        blob = json.dumps({"props": {"pageProps": {"searchRequestAndResponse": {"listings": listings}}}},
                          ensure_ascii=False)
        if len(blob) <= limit or len(listings) <= 12:
            break
        listings = listings[:-1]
    return f'<html><body><script id="__NEXT_DATA__" type="application/json">{blob}</script></body></html>'


async def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("source")
    ap.add_argument("query")
    ap.add_argument("--detail-url")
    ap.add_argument("--detail-only", action="store_true", help="skip the search page (needs --detail-url)")
    a = ap.parse_args(argv)
    cfg = load(ROOT)
    ad = registry(cfg, ROOT)[a.source]
    mod = importlib.import_module(ad.__module__)
    tmp = Path(tempfile.mkdtemp(prefix="dh-capture-"))
    f = Fetcher(cfg, Store(tmp / "c.db"), tmp)
    out = ROOT / "tests" / "fixtures" / a.source
    out.mkdir(parents=True, exist_ok=True)
    q = Query(text=a.query)
    url = ad.page_urls(q)[0] if hasattr(ad, "page_urls") else ad.search_url(q)
    if a.detail_only:
        r2 = await f.get(a.detail_url, kind="html")
        print("detail", r2.status, a.detail_url)
        if r2.status == 200:
            d = trim(r2.text, a.source, kind="detail")
            (out / "detail.html").write_text(d)
            print("  detail.html", len(d), "bytes")
        await f.aclose()
        return 0 if r2.status == 200 else 1
    res = await f.get(url, kind="html")
    print("search", res.status, "disallowed" if res.disallowed else "", "blocked" if res.blocked else "", url)
    if res.status != 200:
        return 1
    body = trim_marktplaats(res.text) if a.source == "marktplaats" else trim(res.text, a.source, kind="search")
    (out / "search.html").write_text(body)
    print("  search.html", len(body), "bytes")
    parse = mod.parse_search
    listings = parse(body, ad) if a.source.startswith("bazos") else parse(body)
    print("  parsed", len(listings), "listings with price > 0")
    durl = a.detail_url or (listings[0].url if listings else None)
    if durl:
        r2 = await f.get(durl, kind="html")
        print("detail", r2.status, durl)
        if r2.status == 200:
            d = trim(r2.text, a.source, kind="detail")
            (out / "detail.html").write_text(d)
            print("  detail.html", len(d), "bytes")
    await f.aclose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
