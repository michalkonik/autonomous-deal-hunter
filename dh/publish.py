"""Log publisher: sync deal cards, INDEX.md, daily/weekly summaries to the private log repo (branch `log`)
and import `outcome:` edits made in the GitHub app as marks (source `github`).

Failure policy (Review Focus 5): git/network failures never raise. They become health events; local state
(imported marks, local commits) is kept and the next cycle pushes again. Marks are written to the DB
before anything can fail.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import yaml

from .cards import PUBLIC_COMP_KEYS, PUBLIC_KEYS, PUBLIC_LISTING_KEYS
from .notify import health_once

MARK_IMPORTS_SQL = """create table if not exists mark_imports (
  deal_id integer primary key, hash text not null, outcome text not null
)"""
STATUSES = {"open", "bought", "skipped", "sold"}
DEFAULT_OUTCOME = {"status": "open", "price_pln": None, "date": None, "note": ""}
_FM = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.S)
GIT_ID = ["-c", "user.name=deal-hunter", "-c", "user.email=michal.konik.job@gmail.com"]


@dataclass
class CardFile:
    path_md: Path
    path_json: Path
    deal_id: int
    tier: int
    star: bool
    cons_net_usd: Decimal
    created_at: datetime


# ---------- outcome blocks ----------

def split_front_matter(text: str) -> tuple[dict, str]:
    """(front matter, body). Raises yaml.YAMLError on broken YAML (callers must not overwrite such a file)."""
    m = _FM.match(text)
    if not m:
        return {}, text
    fm = yaml.safe_load(m.group(1)) or {}
    return (fm if isinstance(fm, dict) else {}), m.group(2)


def parse_outcome(text: str) -> dict | None:
    """Normalised outcome dict, None when there is no outcome block.
    Raises ValueError/yaml.YAMLError on a broken or invalid block."""
    fm, _ = split_front_matter(text)
    o = fm.get("outcome")
    if o is None:
        return None
    if not isinstance(o, dict):
        raise ValueError("outcome is not a mapping")
    status = str(o.get("status", "open")).strip().lower()
    if status not in STATUSES:
        raise ValueError(f"invalid outcome status {status!r}")
    price = o.get("price_pln")
    if price in (None, ""):
        price = None
    else:
        try:
            dec = Decimal(str(price))
        except InvalidOperation:
            raise ValueError(f"invalid price_pln {price!r}") from None
        if not dec.is_finite():
            raise ValueError(f"non-finite price_pln {price!r}")
        price = str(dec)
    d = o.get("date")
    d = None if d in (None, "") else (d.isoformat() if isinstance(d, (date, datetime)) else str(d))
    return {"status": status, "price_pln": price, "date": d, "note": str(o.get("note") or "").strip()}


def _ohash(o: dict) -> str:
    return hashlib.sha256(json.dumps(o, sort_keys=True).encode()).hexdigest()


def _deal_id(name: str, text: str) -> int | None:
    """Front-matter deal_id is authoritative; trailing digits of the file stem are the fallback."""
    fm, _ = split_front_matter(text)
    v = fm.get("deal_id")
    if isinstance(v, int) and not isinstance(v, bool):
        return v
    if isinstance(v, str) and v.isdigit():
        return int(v)
    m = re.search(r"(\d+)$", Path(name).stem)
    return int(m.group(1)) if m else None


def import_texts(store, items) -> tuple[int, set[int]]:
    """items: iterable of (label, text). Imports changed outcome blocks as marks via store.add_mark.
    Returns (imported, deal_ids of unparseable cards). A poison file never stops the import."""
    store.ensure_table(MARK_IMPORTS_SQL)
    n, bad = 0, set()
    for label, text in items:
        did = None
        try:
            did = _deal_id(label, text)
            if did is None:
                continue
            o = parse_outcome(text)
            if o is None:
                continue
            row = store.conn.execute("select hash from mark_imports where deal_id=?", (did,)).fetchone()
            h = _ohash(o)
            if (row is None and o == DEFAULT_OUTCOME) or (row is not None and row["hash"] == h):
                continue
            note = (o["note"] + (f" [date {o['date']}]" if o["date"] else "")).strip()
            # mark first, hash second: a crash in between can duplicate a mark on retry but never lose one
            store.add_mark(did, o["status"], None if o["price_pln"] is None else Decimal(o["price_pln"]),
                           note, "github")
            store.conn.execute("insert or replace into mark_imports (deal_id, hash, outcome) values (?,?,?)",
                               (did, h, json.dumps(o)))
            store.conn.commit()
            n += 1
        except Exception as e:
            if did is not None:
                bad.add(did)
            health_once(store, "publish", "bad_card", f"{label}: {e!r}")
    return n, bad


def import_marks(store, log_dir: Path) -> tuple[int, set[int]]:
    def items():
        for md in sorted((log_dir / "deals").glob("*/*.md")):
            try:
                yield md.as_posix(), md.read_text()
            except OSError as e:
                health_once(store, "publish", "bad_card", f"{md}: {e!r}")
    return import_texts(store, items())


# ---------- cards ----------

# Allowlist of card JSON keys: owned by dh/cards.py (spec section 12 card content). Anything else is dropped;
# no seller data.
CARD_KEYS = PUBLIC_KEYS
COMP_KEYS = PUBLIC_COMP_KEYS
LISTING_KEYS = PUBLIC_LISTING_KEYS


def _strip_seller(x):
    if isinstance(x, dict):
        return {k: _strip_seller(v) for k, v in x.items() if not str(k).lower().startswith("seller")}
    if isinstance(x, list):
        return [_strip_seller(v) for v in x]
    return x


def sanitize_json(text: str) -> str:
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("card json is not an object")
    out = {k: v for k, v in data.items() if k in CARD_KEYS}
    if isinstance(out.get("comps"), list):
        out["comps"] = [{k: v for k, v in c.items() if k in COMP_KEYS} for c in out["comps"] if isinstance(c, dict)]
    if isinstance(out.get("listing"), dict):
        out["listing"] = {k: v for k, v in out["listing"].items() if k in LISTING_KEYS}
    return json.dumps(_strip_seller(out), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _is_seller_line(line: str) -> bool:
    return line.lstrip(" \t-*>_#").lower().startswith("seller")


def sanitize_md(text: str) -> str:
    m = _FM.match(text)
    if m:
        fm = yaml.safe_load(m.group(1)) or {}
        head = m.group(1)
        if isinstance(fm, dict) and any(str(k).lower().startswith("seller") for k in fm):
            head = yaml.safe_dump({k: v for k, v in fm.items() if not str(k).lower().startswith("seller")},
                                  sort_keys=False, allow_unicode=True).rstrip()
        body = "\n".join(l for l in m.group(2).split("\n") if not _is_seller_line(l))
        return f"---\n{head}\n---\n{body}"
    return "\n".join(l for l in text.split("\n") if not _is_seller_line(l))


def scan_cards(store, cards_dir: Path) -> list[CardFile]:
    out = []
    for js in sorted(Path(cards_dir).glob("*/*.json")):
        try:
            md = js.with_suffix(".md")
            if not md.is_file():
                continue
            deal_id = _deal_id(md.name, md.read_text())
            if deal_id is None:
                deal_id = int(json.loads(js.read_text())["deal_id"])
            d = store.get_deal(deal_id)
            if d is None:
                continue
            out.append(CardFile(md, js, deal_id, d.tier, d.star, d.cons_net_usd, d.created_at))
        except Exception as e:
            health_once(store, "publish", "bad_card", f"{js}: {e!r}")
    return out


def _card_dest(log_dir: Path, c: CardFile) -> Path:
    return log_dir / "deals" / c.created_at.astimezone(timezone.utc).strftime("%Y-%m-%d") / c.path_md.name


def _with_outcome(text: str, o: dict) -> str:
    fm, body = split_front_matter(text)
    fm["outcome"] = o
    return "---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).rstrip() + "\n---\n" + body


def write_cards(store, log_dir: Path, cards: list[CardFile], protect: set[int] = frozenset()) -> int:
    """Copy new/changed cards into the clone (sanitised), keeping any outcome already imported.
    Cards in `protect` (broken user edit) are left untouched this cycle."""
    store.ensure_table(MARK_IMPORTS_SQL)
    changed = 0
    for c in cards:
        if c.deal_id in protect:
            continue
        try:
            dest = _card_dest(log_dir, c)
            text = sanitize_md(c.path_md.read_text())
            row = store.conn.execute("select outcome from mark_imports where deal_id=?", (c.deal_id,)).fetchone()
            if row is not None:
                text = _with_outcome(text, json.loads(row["outcome"]))
            elif dest.is_file() and (cur := parse_outcome(dest.read_text())) not in (None, DEFAULT_OUTCOME):
                text = _with_outcome(text, cur)  # unimported edit still on disk: keep it for the next import
            data = sanitize_json(c.path_json.read_text())
            dest.parent.mkdir(parents=True, exist_ok=True)
            if not dest.is_file() or dest.read_text() != text:
                dest.write_text(text)
                changed += 1
            jdest = dest.with_suffix(".json")
            if not jdest.is_file() or jdest.read_text() != data:
                jdest.write_text(data)
        except Exception as e:
            health_once(store, "publish", "bad_card", f"{c.path_md}: {e!r}")
    return changed


def index_order(cards: list[CardFile]) -> list[CardFile]:
    """Star and tier 1 first, then by tier, then conservative net (desc)."""
    return sorted(cards, key=lambda c: (not (c.star or c.tier == 1), c.tier, -c.cons_net_usd, c.deal_id))


def _line(c: CardFile, log_dir: Path) -> str:
    rel = _card_dest(log_dir, c).relative_to(log_dir).as_posix()
    star = "★ " if c.star else ""
    return f"- {star}T{c.tier} +${c.cons_net_usd:.0f} · [{c.path_md.stem}]({rel}) · {c.created_at:%Y-%m-%d}"


def write_reports(store, log_dir: Path, cards: list[CardFile]) -> None:
    ordered = index_order(cards)
    (log_dir / "INDEX.md").write_text(
        "# Deal Hunter index\n\nStar and tier-1 first, then by tier and conservative net.\n\n"
        + ("\n".join(_line(c, log_dir) for c in ordered) or "_no deals yet_") + "\n")
    days: dict[str, list[CardFile]] = {}
    weeks: dict[str, list[CardFile]] = {}
    for c in cards:
        d = c.created_at.astimezone(timezone.utc)
        days.setdefault(d.strftime("%Y-%m-%d"), []).append(c)
        y, w, _ = d.isocalendar()
        weeks.setdefault(f"{y}-W{w:02d}", []).append(c)
    for sub, groups in (("daily", days), ("weekly", weeks)):
        (log_dir / sub).mkdir(exist_ok=True)
        for name, cs in groups.items():
            by_tier = {t: sum(1 for c in cs if c.tier == t) for t in sorted({c.tier for c in cs})}
            tiers = ", ".join(f"T{t}: {n}" for t, n in by_tier.items())
            marks = _mark_counts(store, [c.deal_id for c in cs])
            mk = ", ".join(f"{k}: {v}" for k, v in sorted(marks.items())) or "none"
            body = (f"# {'Day' if sub == 'daily' else 'Week'} {name}\n\nDeals: {len(cs)} ({tiers})\n\n"
                    f"Marks: {mk}\n\n" + "\n".join(_line(c, log_dir) for c in index_order(cs)) + "\n")
            (log_dir / sub / f"{name}.md").write_text(body)


def _mark_counts(store, ids: list[int]) -> dict[str, int]:
    if not ids:
        return {}
    q = ",".join("?" * len(ids))
    # latest mark per deal
    rows = store.conn.execute(
        f"select status, count(*) n from marks m where deal_id in ({q}) and id=(select max(id) from marks "
        f"where deal_id=m.deal_id) group by status", ids).fetchall()
    return {r["status"]: r["n"] for r in rows}


# ---------- git ----------

BRANCH = "log"  # hard-coded: the log repo's only branch; never push anything else


async def _git(cwd: Path, *args: str, timeout: float = 120) -> tuple[int, str]:
    p = None
    try:
        p = await asyncio.create_subprocess_exec("git", *GIT_ID, *args, cwd=str(cwd),
                                                 stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                                                 env=_env())
        out, _ = await asyncio.wait_for(p.communicate(), timeout)
        return p.returncode, out.decode(errors="replace").strip()
    except asyncio.TimeoutError:
        p.kill()
        await p.wait()
        return 124, "timeout"
    except OSError as e:
        return 127, repr(e)


def _env() -> dict:
    import os
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0"}


async def _ensure_clone(log_dir: Path, remote: str | None, store) -> bool:
    if (log_dir / ".git").is_dir():
        return True
    if not remote:
        health_once(store, "publish", "no_clone", f"{log_dir} missing and publish.remote not set")
        return False
    log_dir.parent.mkdir(parents=True, exist_ok=True)
    rc, out = await _git(log_dir.parent, "clone", "--branch", BRANCH, "--", remote, str(log_dir))
    if rc != 0:
        health_once(store, "publish", "clone_failed", out[:500])
        return False
    return True


async def _origin_items(log_dir: Path):
    """(label, text) of every deal card on origin/log, read from the git tree (no checkout)."""
    rc, names = await _git(log_dir, "ls-tree", "-r", "--name-only", f"origin/{BRANCH}", "--", "deals")
    if rc != 0:
        return []
    items = []
    for n in names.splitlines():
        if n.endswith(".md"):
            rc, text = await _git(log_dir, "show", f"origin/{BRANCH}:{n}")
            if rc == 0:
                items.append((n, text))
    return items


async def sync(store, cfg, root: Path | None = None) -> dict:
    """One publish cycle. Never raises on git/network failure."""
    root = Path(root or ".")
    pc = cfg.publish
    log_dir = Path(str(pc.log_dir)).expanduser()
    cards_dir = Path(str(pc.cards_dir))
    cards_dir = cards_dir if cards_dir.is_absolute() else root / cards_dir
    res = {"pulled": False, "marks": 0, "cards": 0, "committed": False, "pushed": False}
    if pc.get("branch", BRANCH) != BRANCH:
        health_once(store, "publish", "bad_branch", f"publish.branch must be {BRANCH!r}, got {pc.get('branch')!r}")
        return res
    store.ensure_table(MARK_IMPORTS_SQL)
    if not await _ensure_clone(log_dir, pc.get("remote"), store):
        return res
    if (await _git(log_dir, "symbolic-ref", "-q", "HEAD"))[1] != f"refs/heads/{BRANCH}":
        await _git(log_dir, "checkout", "-q", "-B", BRANCH)
    rc, out = await _git(log_dir, "pull", "--rebase", "origin", BRANCH)
    res["pulled"] = rc == 0
    bad: set[int] = set()
    if rc != 0:
        await _git(log_dir, "rebase", "--abort")
        health_once(store, "publish", "pull_failed", out[:500])
        # Conflict (or other pull failure) with a reachable remote: take the remote's marks from its tree,
        # then reset to it. Local cards are regenerated below from the card dir + DB.
        frc, fout = await _git(log_dir, "fetch", "origin", BRANCH)
        if frc == 0:
            n, b = import_texts(store, await _origin_items(log_dir))
            res["marks"] += n
            bad |= b
            rrc, rout = await _git(log_dir, "reset", "--hard", f"origin/{BRANCH}")
            if rrc != 0:
                health_once(store, "publish", "reset_failed", rout[:500])
        else:
            health_once(store, "publish", "fetch_failed", fout[:500])
    n, b = import_marks(store, log_dir)
    res["marks"] += n
    bad |= b
    cards = scan_cards(store, cards_dir)
    res["cards"] = write_cards(store, log_dir, cards, protect=bad)
    # reports cover every card in the clone, not just ones still in the local cards dir
    known = {c.deal_id: c for c in cards}
    for js in (log_dir / "deals").glob("*/*.json"):
        try:
            did = int(json.loads(js.read_text())["deal_id"])
            if did not in known and (d := store.get_deal(did)) is not None:
                known[did] = CardFile(js.with_suffix(".md"), js, did, d.tier, d.star, d.cons_net_usd, d.created_at)
        except Exception as e:
            health_once(store, "publish", "bad_card", f"{js}: {e!r}")
    try:
        write_reports(store, log_dir, list(known.values()))
    except Exception as e:
        health_once(store, "publish", "report_failed", repr(e)[:500])
    await _git(log_dir, "add", "-A")
    rc, _ = await _git(log_dir, "diff", "--cached", "--quiet")
    if rc != 0:
        rc, out = await _git(log_dir, "commit", "-q", "-m",
                             f"deal-hunter sync {datetime.now(timezone.utc):%Y-%m-%d %H:%M}")
        res["committed"] = rc == 0
        if rc != 0:
            health_once(store, "publish", "commit_failed", out[:500])
    # push if anything is ahead of the remote (also covers commits left over from a failed earlier push)
    rc, ahead = await _git(log_dir, "rev-list", "--count", f"origin/{BRANCH}..HEAD")
    if rc != 0 or ahead.strip() != "0":
        rc, out = await _git(log_dir, "push", "origin", BRANCH)
        res["pushed"] = rc == 0
        if rc != 0:
            health_once(store, "publish", "push_failed", out[:500])
    return res


async def run_loop(store, cfg, root: Path, stop: asyncio.Event) -> None:
    interval = int(cfg.publish.interval_min) * 60
    while not stop.is_set():
        try:
            await sync(store, cfg, root)
        except Exception as e:  # the round must never die because of the publisher
            health_once(store, "publish", "sync_error", repr(e)[:500])
        try:
            await asyncio.wait_for(stop.wait(), interval)
        except asyncio.TimeoutError:
            pass
