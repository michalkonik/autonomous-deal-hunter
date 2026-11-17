"""Marketplace source adapters. `registry()` returns the enabled ones, keyed by source name."""
from __future__ import annotations

from pathlib import Path

from .base import Adapter, AdapterError, Query, ROOT

__all__ = ["Adapter", "AdapterError", "Query", "registry", "ADAPTER_NAMES", "DROPPED"]

# Sources evaluated and dropped because robots.txt forbids polite access (see task-8-report.md).
DROPPED = {"sbazar": "robots.txt: `User-agent: *  Disallow: /` (only Googlebot-style agents are allowed)",
           "willhaben": "robots.txt header: \"expressively forbidden to use spiders, search robots or other automatic "
                        "methods\" without willhaben.at's permission; keyword search and paging are Disallowed too",
           "olx_ro": "OLX (ro, bg, pt) blocks polite polling like olx.pl: dropped, same mailbox-only rule",
           "olx_bg": "see olx_ro", "olx_pt": "see olx_ro",
           "swappie": "plain GET of the Polish iPhone category returned 403 on 2026-11-13; not worked around",
           "subito": "robots.txt returned 403 (access denied) on 2026-11-12",
           "blocket": "robots.txt says automated crawling is prohibited without written permission (checked 2026-11-12)",
           "backmarket": "plain GET of the laptop category returned 403 (checked 2026-11-12); not worked around",
           "afbshop": "shipping page lists Germany, Austria, Belgium, Italy and Spain, not Poland (checked 2026-11-13)",
           "computeruniverse": "homepage returned 403 on 2026-11-13; not worked around",
           "cex": "the Poland search page had no product cards, and the catalog JSON returned 403 (2026-11-13)",
           "itsco": "ships to Poland, but the catalog card is a bulk from-price and the search URL returned 500 "
                    "(2026-11-13); the one-piece price was not booked",
           "clevertronic": "homepage returned 403 (a challenge page) on 2026-11-13; not worked around",
           "wisetek": "ships to Poland, but the search page and the catalog JSON returned 429 on 2026-11-13; "
                      "not worked around",
           "lapstore": "Poland delivery is only for commercial resellers, not a private address "
                       "(checked 2026-11-13)"}


def _build(conf: dict | None = None) -> dict[str, Adapter]:
    from .allegro import Allegro
    from .allegro_lokalnie import AllegroLokalnie
    from .bazos import Bazos
    from .hardverapro import Hardverapro
    from .kleinanzeigen import Kleinanzeigen
    from .ebay import EbayBrowse
    from .mailbox import Mailbox
    from .marktplaats import Marktplaats
    from .ememain import E2emain
    from .tori import Tori
    from .retail import RetailWatch
    from .rss import RssAdapter
    from .sprzedajemy import Sprzedajemy
    from .refurbed import Refurbed
    from .taniekomputery import Taniekomputery
    from .asgoodasnew import Asgoodasnew
    from .amso import Amso
    from .luxtrade import Luxtrade
    from .devicestore import Devicestore
    from .gebrauchtetechnik import GebrauchteTechnik

    adapters: list[Adapter] = [Allegro(), AllegroLokalnie(), Sprzedajemy(), Refurbed(), Taniekomputery(),
                               Asgoodasnew(), Amso(), Luxtrade(), Devicestore(), GebrauchteTechnik(),
                               Kleinanzeigen(), Hardverapro(), Marktplaats(),
                               E2emain(), Tori(),
                               Bazos("cz"), Bazos("sk"), Bazos("at"),
                               EbayBrowse("EBAY_DE"), EbayBrowse("EBAY_PL"),
                               EbayBrowse("EBAY_GB"), EbayBrowse("EBAY_FR"), EbayBrowse("EBAY_IT"),
                               EbayBrowse("EBAY_ES"), EbayBrowse("EBAY_IE"), EbayBrowse("EBAY_AT"),
                               EbayBrowse("EBAY_CH"), EbayBrowse("EBAY_BE"),
                               RssAdapter("pepper_pl", "https://www.pepper.pl/rss/hot", "PL", "PLN"),
                               RssAdapter("hotukdeals", "https://www.hotukdeals.com/rss/hot", "GB", "GBP"),
                               Mailbox(),
                               RetailWatch((conf or {}).get("watch"), float((conf or {}).get("drop_pct", 0.10)))]
    return {a.name: a for a in adapters}


ADAPTER_NAMES = ["allegro", "allegro_lokalnie", "sprzedajemy", "refurbed", "taniekomputery", "asgoodasnew", "amso",
                 "luxtrade", "devicestore", "gebrauchtetechnik", "kleinanzeigen", "hardverapro", "marktplaats", "2ememain",
                 "bazos_cz", "bazos_sk", "bazos_at", "ebay_de", "ebay_pl", "tori", "ebay_gb", "ebay_fr", "ebay_it",
                 "ebay_es", "ebay_ie", "ebay_at", "ebay_ch", "ebay_be", "pepper_pl", "hotukdeals", "mailbox", "retail"]


def registry(cfg=None, root: Path | None = None) -> dict[str, Adapter]:
    """Adapters enabled in config/sources.yaml (`adapters.<name>.enabled`; default enabled when unlisted)."""
    if cfg is None:
        from ..config import load
        cfg = load(Path(root or ROOT))
    try:
        conf = cfg.sources.adapters.to_dict()
    except (AttributeError, KeyError):
        conf = {}
    out = {}
    try:
        retail_conf = cfg.sources.retail.to_dict()
    except (AttributeError, KeyError):
        retail_conf = {}
    for name, ad in _build(retail_conf).items():
        if name.startswith("bazos"):
            entry = conf.get(name) or conf.get(name.split("_")[0])
        elif name.startswith("ebay_"):
            entry = conf.get(name) or conf.get("ebay")
        else:
            entry = conf.get(name)
        if isinstance(entry, dict) and entry.get("enabled") is False:
            continue
        if isinstance(entry, dict) and name == "mailbox" and entry.get("image_hosts"):
            ad.image_hosts = {k: tuple(v) for k, v in entry["image_hosts"].items()}
        if isinstance(entry, dict) and name.startswith("bazos") and "pages" in entry:
            ad.pages = int(entry["pages"])
        if isinstance(entry, dict) and name == "allegro" and entry.get("user_agent"):
            ad.user_agent = str(entry["user_agent"])
            ad.client.user_agent = ad.user_agent
        out[name] = ad
    return out
