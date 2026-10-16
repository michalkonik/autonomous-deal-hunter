"""Verified labels from the owner's marks (spec §9.5). FROZEN: part of the measurement layer (config.FROZEN_PATHS).

Only verified outcomes are labels: the owner's Bought / Sold marks (the deal was real) and Skipped marks whose
reason names a wrongness (scam, box, wrong model, broken, wanted ad...). A skip for time, distance or "not
interested" says nothing about the judgment and is not a label. The agent's own opinion is never a label.
"""
from __future__ import annotations

import re

# a Skipped reason that says the DEAL was wrong (not that the owner had no time); PL / EN / DE wording
FP_REASON = re.compile(
    r"scam|oszust|fake|podr[oó]b|fa[lł]szyw|box|pud[eł]|karton|ovp|wrong|z[lł]y model|z[lł]a karta|nie ten|"
    r"inny model|mismatch|bad match|niezgodn|broken|uszkodz|defekt|for parts|na cz[eę][sś]ci|wanted|kupi|bait|"
    r"not a deal|false|nieprawd|accessor|akcesori|stripped|bez rdzenia|no die|price wrong|overvalued|zawy[zż]on",
    re.I)


def is_false_positive(status: str | None, note: str | None) -> bool:
    return (status or "") == "skipped" and bool(FP_REASON.search(note or ""))


def is_true_positive(status: str | None) -> bool:
    return (status or "") in ("bought", "sold")


def known_outcomes(store) -> tuple[set[int], set[int]]:
    """(listing ids whose deal the owner marked wrong, listing ids the owner bought or sold)."""
    fp: set[int] = set()
    tp: set[int] = set()
    for r in store.conn.execute("select d.listing_ref lid, m.status, m.note from marks m "
                                "join deals d on d.id = m.deal_id order by m.id"):
        if is_true_positive(r["status"]):
            tp.add(r["lid"])
            fp.discard(r["lid"])
        elif is_false_positive(r["status"], r["note"]) and r["lid"] not in tp:
            fp.add(r["lid"])
    return fp, tp
