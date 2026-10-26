"""Verified labels from the owner's marks (spec §9.5). FROZEN: part of the measurement layer (config.FROZEN_PATHS).

Only verified outcomes are labels: the owner's Bought / Sold marks (the deal was real) and Skipped marks whose
reason names a wrongness (scam, box, wrong model, broken, wanted ad...). A skip for time, distance or "not
interested" says nothing about the judgment and is not a label. The agent's own opinion is never a label.
"""
from __future__ import annotations

import re

# a Skipped reason that says the DEAL was wrong (not that the owner had no time); PL / EN / DE wording
_WORDS = (  # stems match at a word start; short words must stand alone ("box" never inside "xbox")
    r"scam\w*", r"oszust\w*", r"fake", r"podr[oó]b\w*", r"fa[lł]szyw\w*", r"box", r"box only", r"pud[eł]+k\w*",
    r"karton\w*", r"ovp", r"wrong", r"z[lł]y model", r"z[lł]a karta", r"nie ten", r"inny model", r"mismatch",
    r"bad match", r"niezgodn\w*", r"broken", r"uszkodz\w*", r"defekt\w*", r"for parts", r"na cz[eę][sś]ci",
    r"wanted", r"wanted ad", r"kupi[eę]", r"ogłoszenie kupna", r"bait", r"not a deal", r"false", r"nieprawd\w*",
    r"accessor\w*", r"akcesori\w*", r"stripped", r"bez rdzenia", r"no die", r"price wrong", r"overvalued",
    r"zawy[zż]on\w*")
# a Skipped reason that says the DEAL was wrong (not that the owner had no time); PL / EN / DE wording
FP_REASON = re.compile(r"(?<!\w)(?:" + "|".join(_WORDS) + r")(?!\w)", re.I)


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
