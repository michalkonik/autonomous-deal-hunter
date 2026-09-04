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
    m = _FM.match(text)
    if not m:
        return {}, text
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return {}, text
    return (fm if isinstance(fm, dict) else {}), m.group(2)


def parse_outcome(text: str) -> dict | None:
    """Normalised outcome dict from a card's front matter, or None when absent/invalid."""
    fm, _ = split_front_matter(text)
    o = fm.get("outcome")
    if not isinstance(o, dict):
        return None
    status = str(o.get("status", "open")).strip().lower()
    if status not in STATUSES:
        return None
    price = o.get("price_pln")
    try:
        price = None if price in (None, "") else str(Decimal(str(price)))
    except InvalidOperation:
        return None
    d = o.get("date")
    d = None if d in (None, "") else (d.isoformat() if isinstance(d, (date, datetime)) else str(d))
    return {"status": status, "price_pln": price, "date": d, "note": str(o.get("note") or "").strip()}


def _ohash(o: dict) -> str:
    return hashlib.sha256(json.dumps(o, sort_keys=True).encode()).hexdigest()


def import_marks(store, log_dir: Path) -> int:
    """Import edited outcome blocks as marks. Returns number imported. Idempotent via mark_imports."""
    store.ensure_table(MARK_IMPORTS_SQL)
    n = 0
    for md in sorted((log_dir / "deals").glob("*/*.md")):
        deal_id = _deal_id(md)
        if deal_id is None:
            continue
        o = parse_outcome(md.read_text())
        if o is None:
            continue
        row = store.conn.execute("select hash from mark_imports where deal_id=?", (deal_id,)).fetchone()
        h = _ohash(o)
        if (row is None and o == DEFAULT_OUTCOME) or (row is not None and row["hash"] == h):
            continue
        note = o["note"] + (f" [date {o['date']}]" if o["date"] else "")
        # mark first, then the hash, in one transaction: a crash in between cannot lose or duplicate it
        with store.conn:
            store.conn.execute("insert into marks (deal_id, status, price_pln, note, source) values (?,?,?,?,?)",
                               (deal_id, o["status"], o["price_pln"], note.strip(), "github"))
            store.conn.execute("insert or replace into mark_imports (deal_id, hash, outcome) values (?,?,?)",
                               (deal_id, h, json.dumps(o)))
        n += 1
    return n


def _deal_id(md: Path) -> int | None:
    m = re.search(r"(\d+)$", md.stem)
    if m:
        return int(m.group(1))
    fm, _ = split_front_matter(md.read_text())
    return int(fm["deal_id"]) if str(fm.get("deal_id", "")).isdigit() else None


# ---------- cards ----------

def scan_cards(store, cards_dir: Path) -> list[CardFile]:
    out = []
    for js in sorted(Path(cards_dir).glob("*/*.json")):
        md = js.with_suffix(".md")
        if not md.is_file():
            continue
        try:
            deal_id = int(json.loads(js.read_text())["deal_id"])
        except (ValueError, KeyError, TypeError):
            continue
        d = store.get_deal(deal_id)
        if d is None:
            continue
        out.append(CardFile(md, js, deal_id, d.tier, d.star, d.cons_net_usd, d.created_at))
    return out


def _card_dest(log_dir: Path, c: CardFile) -> Path:
    return log_dir / "deals" / c.created_at.astimezone(timezone.utc).strftime("%Y-%m-%d") / c.path_md.name


def _with_outcome(text: str, o: dict) -> str:
    fm, body = split_front_matter(text)
    fm["outcome"] = o
    return "---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).rstrip() + "\n---\n" + body


def write_cards(store, log_dir: Path, cards: list[CardFile]) -> int:
    """Copy new/changed cards into the clone, keeping any outcome already imported (never clobber an edit)."""
    store.ensure_table(MARK_IMPORTS_SQL)
    changed = 0
    for c in cards:
        dest = _card_dest(log_dir, c)
        text = c.path_md.read_text()
        row = store.conn.execute("select outcome from mark_imports where deal_id=?", (c.deal_id,)).fetchone()
        if row is not None:
            text = _with_outcome(text, json.loads(row["outcome"]))
        elif dest.is_file() and (cur := parse_outcome(dest.read_text())) not in (None, DEFAULT_OUTCOME):
            text = _with_outcome(text, cur)  # unimported edit still on disk: keep it for the next import
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.is_file() or dest.read_text() != text:
            dest.write_text(text)
            changed += 1
        jdest = dest.with_suffix(".json")
        data = c.path_json.read_text()
        if not jdest.is_file() or jdest.read_text() != data:
            jdest.write_text(data)
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

async def _git(cwd: Path, *args: str, timeout: float = 120) -> tuple[int, str]:
    try:
        p = await asyncio.create_subprocess_exec("git", *GIT_ID, *args, cwd=str(cwd),
                                                 stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                                                 env=_env())
        out, _ = await asyncio.wait_for(p.communicate(), timeout)
        return p.returncode, out.decode(errors="replace").strip()
    except asyncio.TimeoutError:
        p.kill()
        return 124, "timeout"
    except OSError as e:
        return 127, repr(e)


def _env() -> dict:
    import os
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0"}


async def _ensure_clone(log_dir: Path, remote: str | None, branch: str, store) -> bool:
    if (log_dir / ".git").is_dir():
        return True
    if not remote:
        store.add_health("publish", "no_clone", f"{log_dir} missing and publish.remote not set")
        return False
    log_dir.parent.mkdir(parents=True, exist_ok=True)
    rc, out = await _git(log_dir.parent, "clone", "--branch", branch, remote, str(log_dir))
    if rc != 0:
        store.add_health("publish", "clone_failed", out[:500])
        return False
    return True


async def sync(store, cfg, root: Path | None = None) -> dict:
    """One publish cycle. Never raises on git/network failure."""
    root = Path(root or ".")
    pc = cfg.publish
    branch = pc.get("branch", "log")
    log_dir = Path(str(pc.log_dir)).expanduser()
    cards_dir = Path(str(pc.cards_dir))
    cards_dir = cards_dir if cards_dir.is_absolute() else root / cards_dir
    res = {"pulled": False, "marks": 0, "cards": 0, "committed": False, "pushed": False}
    store.ensure_table(MARK_IMPORTS_SQL)
    if not await _ensure_clone(log_dir, pc.get("remote"), branch, store):
        return res
    if (await _git(log_dir, "symbolic-ref", "-q", "HEAD"))[1] != f"refs/heads/{branch}":
        await _git(log_dir, "checkout", "-q", "-B", branch)
    rc, out = await _git(log_dir, "pull", "--rebase", "--autostash", "origin", branch)
    res["pulled"] = rc == 0
    if rc != 0:
        await _git(log_dir, "rebase", "--abort")
        store.add_health("publish", "pull_failed", out[:500])
    res["marks"] = import_marks(store, log_dir)
    cards = scan_cards(store, cards_dir)
    res["cards"] = write_cards(store, log_dir, cards)
    # reports cover every card in the clone, not just ones still in the local cards dir
    known = {c.deal_id: c for c in cards}
    for js in (log_dir / "deals").glob("*/*.json"):
        try:
            did = int(json.loads(js.read_text())["deal_id"])
        except (ValueError, KeyError, TypeError):
            continue
        if did not in known and (d := store.get_deal(did)) is not None:
            known[did] = CardFile(js.with_suffix(".md"), js, did, d.tier, d.star, d.cons_net_usd, d.created_at)
    write_reports(store, log_dir, list(known.values()))
    await _git(log_dir, "add", "-A")
    rc, _ = await _git(log_dir, "diff", "--cached", "--quiet")
    if rc != 0:
        rc, out = await _git(log_dir, "commit", "-q", "-m", f"deal-hunter sync {datetime.now(timezone.utc):%Y-%m-%d %H:%M}")
        res["committed"] = rc == 0
        if rc != 0:
            store.add_health("publish", "commit_failed", out[:500])
    # push if anything is ahead of the remote (also covers commits left over from a failed earlier push)
    rc, ahead = await _git(log_dir, "rev-list", "--count", f"origin/{branch}..HEAD")
    if rc != 0 or ahead.strip() != "0":
        rc, out = await _git(log_dir, "push", "origin", branch)
        res["pushed"] = rc == 0
        if rc != 0:
            store.add_health("publish", "push_failed", out[:500])
    return res


async def run_loop(store, cfg, root: Path, stop: asyncio.Event) -> None:
    interval = int(cfg.publish.interval_min) * 60
    while not stop.is_set():
        try:
            await sync(store, cfg, root)
        except Exception as e:  # the round must never die because of the publisher
            store.add_health("publish", "sync_error", repr(e)[:500])
        try:
            await asyncio.wait_for(stop.wait(), interval)
        except asyncio.TimeoutError:
            pass
