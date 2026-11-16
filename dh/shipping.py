"""Direct-to-Poland shipping assessment per source (owner request 2026-11-07).

`ship_pl(listing)` returns 1 when the listing can be ordered straight to Poland, 0 when the source says
it cannot (pickup-only), None when the source does not say (unknown, rendered "?"). The rules lean on
what each adapter already proves, verified 2026-11-07 against live sources:

- Polish marketplaces and the Polish deal aggregator: the seller is in Poland, ordering is domestic.
- eBay (any market, incl. insights): the search itself is filtered by `deliveryCountry: PL`
  (dh/sources/ebay.py SHIP_TO), so every eBay listing we see ships to Poland.
- kleinanzeigen / marktplaats / 2ememain / tori: adapters parse a `ships` flag, but that only proves
  domestic shipping, not Poland — left unknown; a pickup-only ad is a clear 0.
- bazos, hardverapro, hotukdeals and the rest: no structured shipping info — unknown.
"""

from .tiering import PL_SOURCES

_SHIPS_TO_PL = frozenset(PL_SOURCES | {"pepper_pl"})
_CLASSIFIED_DOMESTIC_SHIP = frozenset({"kleinanzeigen", "marktplaats", "2ememain", "tori"})


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
