"""Direct-to-Poland shipping assessment per source (owner request 2026-11-07).

`ship_pl(listing)` returns 1 when the listing can be ordered straight to Poland, 0 when the source says
it cannot (pickup-only), None when the source does not say (unknown, rendered "?"). The rules lean on
what each adapter already proves, verified 2026-11-07 against live sources:

- Polish marketplaces and the Polish deal aggregator: the seller is in Poland, ordering is domestic.
- refurbed.pl: the Polish store's price includes delivery to Poland (checked 2026-11-12).
- taniekomputery.pl: a Polish refurbished shop; the offer is bought with delivery in Poland.
- asgoodasnew.de: the shipping page prices Poland at 10,90 EUR and names DHL (checked 2026-11-13).
- amso.pl: a Polish shop in Kraków; the offer is bought with delivery in Poland.
- luxtrade.pl: a Polish shop for refurbished phones; the offer is bought with delivery in Poland.
- device.store: a Polish shop in Bydgoszcz; the storefront delivers to Poland (checked 2026-11-13).
- gebrauchte-technik.de: the shipping page lists Poland at 9,99 EUR (checked 2026-11-13).
- eBay (any market, incl. insights): the search itself is filtered by `deliveryCountry: PL`
  (dh/sources/ebay.py SHIP_TO), so every eBay listing we see ships to Poland.
- kleinanzeigen / marktplaats / 2ememain / tori: adapters parse a `ships` flag, but that only proves
  domestic shipping, not Poland. The scheduler prices them as a pickup from home, not as a cheap
  EU parcel. A pickup-only ad on any source is a clear 0.
- bazos, hardverapro, hotukdeals and the rest: no structured shipping info — unknown.
"""

from .tiering import PL_SOURCES

_SHIPS_TO_PL = frozenset(PL_SOURCES | {"pepper_pl", "refurbed", "taniekomputery", "asgoodasnew", "amso",
                                        "luxtrade", "devicestore", "gebrauchtetechnik"})
_CLASSIFIED_DOMESTIC_SHIP = frozenset({"kleinanzeigen", "marktplaats", "2ememain", "tori"})


def inbound_pln(origin: str, table: dict, overrides: dict | None = None):
    """Gross PLN to get the parcel to Poland. A per-country override wins over the PL / EU / non-EU table.

    Overseas GPUs are not a 200 PLN European envelope. The override lives in valuation.yaml so the frozen
    shipping table stays untouched."""
    from decimal import Decimal

    from . import economics
    code = economics.norm_country(origin or "PL")
    over = overrides or {}
    if code in over:
        return Decimal(str(over[code]))
    key = "PL" if code == "PL" else ("EU" if code in economics.EU else "non_EU")
    return Decimal(str((table or {}).get(key, 0)))


def domestic_classified(source: str) -> bool:
    """True when this source's ships flag means domestic delivery, not a shipment to Poland."""
    return str(source or "") in _CLASSIFIED_DOMESTIC_SHIP


def ship_pl(listing) -> int | None:
    """1 ships to PL / 0 does not / None unknown, from the listing and what its source can prove."""
    source = str(getattr(listing, "source", "") or "")
    if getattr(listing, "pickup_only", False):
        return 0
    if source in _SHIPS_TO_PL:
        return 1
    if source.startswith("ebay"):
        return 1
    if source in _CLASSIFIED_DOMESTIC_SHIP:
        return None
    return None


def easy_poland(source, country, ship_pl_flag) -> bool:
    """Easy to get into Poland: a Polish offer (including pickup), or a listing that ships to Poland.

    Pickup on a Polish marketplace is easy even though `ship_pl` is 0 (pickup is checked first). A foreign
    pickup that does not ship here is not. This ranks deals inside a tier. It does not change the tier."""
    if str(country or "").upper() == "PL":
        return True
    if str(source or "") in _SHIPS_TO_PL:
        return True
    return ship_pl_flag == 1
