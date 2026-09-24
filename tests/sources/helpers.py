from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

FIX = Path(__file__).resolve().parents[1] / "fixtures"


def fixture_text(source: str, name: str) -> str:
    return (FIX / source / name).read_text()


@dataclass
class Res:
    status: int = 200
    text: str = ""
    url: str = ""
    blocked: bool = False
    disallowed: bool = False


class FakeFetcher:
    """Records requested URLs; replies from a {url_substring: Res} map (first match) or a default."""

    def __init__(self, routes: dict[str, Res] | None = None, default: Res | None = None):
        self.routes = routes or {}
        self.default = default or Res(404, "", "")
        self.urls: list[str] = []

    async def get(self, url, *, kind="html", headers=None):
        self.urls.append(url)
        for k, r in self.routes.items():
            if k in url:
                return Res(r.status, r.text, r.url or url, r.blocked, r.disallowed)
        d = self.default
        return Res(d.status, d.text, d.url or url, d.blocked, d.disallowed)


def check_listings(ls, currency: str, n: int = 10):
    assert len(ls) >= n
    ids = set()
    for l in ls:
        assert l.source_id and l.url.startswith("https://") and l.title.strip()
        assert l.price > Decimal(0) and l.currency == currency
        assert l.seen_at.tzinfo is not None
        ids.add(l.source_id)
    assert len(ids) == len(ls), "duplicate ids"


def sub_first(html: str, pattern: str, repl: str) -> str:
    out, n = re.subn(pattern, repl, html, count=1, flags=re.S)
    assert n == 1, f"pattern did not match: {pattern}"
    return out
