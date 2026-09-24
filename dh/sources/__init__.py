"""Marketplace source adapters. `registry()` returns the enabled ones, keyed by source name."""
from __future__ import annotations

from pathlib import Path

from .base import Adapter, AdapterError, Query, ROOT

__all__ = ["Adapter", "AdapterError", "Query", "registry", "ADAPTER_NAMES", "DROPPED"]

# Sources evaluated and dropped because robots.txt forbids polite access (see task-8-report.md).
DROPPED = {"sbazar": "robots.txt: `User-agent: *  Disallow: /` (only Googlebot-style agents are allowed)"}


def _build() -> dict[str, Adapter]:
    from .allegro_lokalnie import AllegroLokalnie
    from .bazos import Bazos
    from .hardverapro import Hardverapro
    from .kleinanzeigen import Kleinanzeigen
    from .marktplaats import Marktplaats
    from .sprzedajemy import Sprzedajemy

    adapters: list[Adapter] = [AllegroLokalnie(), Sprzedajemy(), Kleinanzeigen(), Hardverapro(), Marktplaats(),
                               Bazos("cz"), Bazos("sk"), Bazos("at")]
    return {a.name: a for a in adapters}


ADAPTER_NAMES = ["allegro_lokalnie", "sprzedajemy", "kleinanzeigen", "hardverapro", "marktplaats",
                 "bazos_cz", "bazos_sk", "bazos_at"]


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
    for name, ad in _build().items():
        entry = conf.get(name) or conf.get(name.split("_")[0]) if name.startswith("bazos") else conf.get(name)
        if isinstance(entry, dict) and entry.get("enabled") is False:
            continue
        if isinstance(entry, dict) and name.startswith("bazos") and "pages" in entry:
            ad.pages = int(entry["pages"])
        out[name] = ad
    return out
