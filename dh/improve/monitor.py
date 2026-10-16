"""Watched-hours clock and the per-round counts the CUSUMs watch (spec §9.2, §9.4).

Watched hours = the UNION of the ledger's observation windows (dh start .. dh stop, per source). Summing the
windows per source (ledger.watched_hours) counts one hour once per source; every stop rule here uses the union, so
"48 watched hours" is 48 hours the agent was really running.

Per round the controller records two counts in `improve_rounds`:
- champion_n: deals (tier 1-4, near-misses included for a denser signal) the live round saved from listings that
  a non-sentinel arm returned;
- sentinel_n: listings returned by the sentinel arms and evaluated in the round, judged OFFLINE by the pinned
  sentinel playbook, with tier 1-4. The sentinel never changes, so a drop there is the market, not the playbook.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .. import ledger
from .playbook import Playbook

ROUNDS_SQL = """create table if not exists improve_rounds (
  round_id integer primary key, watched_h real not null, version integer, champion_n integer not null,
  sentinel_n integer not null, candidates integer not null default 0, at text)"""


def _t(s: str | None) -> datetime | None:
    return None if not s else datetime.fromisoformat(s)


def watched_hours(store, until: datetime | None = None) -> float:
    """Union of all observation windows (open windows end at their last observation), in hours."""
    ledger.migrate(store)
    spans = []
    for w in store.conn.execute("select opened_at, closed_at, last_obs_at from windows"):
        a = _t(w["opened_at"])
        b = _t(w["closed_at"]) or _t(w["last_obs_at"]) or a
        if until is not None:
            b = min(b, until)
        if b > a:
            spans.append((a, b))
    spans.sort()
    total, cur_a, cur_b = 0.0, None, None
    for a, b in spans:
        if cur_b is None or a > cur_b:
            if cur_b is not None:
                total += (cur_b - cur_a).total_seconds()
            cur_a, cur_b = a, b
        else:
            cur_b = max(cur_b, b)
    if cur_b is not None:
        total += (cur_b - cur_a).total_seconds()
    return total / 3600


def migrate(store) -> None:
    store.ensure_table(ROUNDS_SQL)


def record(store, round_id: int, watched_h: float, version: int | None, champion_n: int, sentinel_n: int,
           candidates: int = 0) -> None:
    migrate(store)
    store.conn.execute("insert or replace into improve_rounds (round_id, watched_h, version, champion_n, sentinel_n, "
                       "candidates, at) values (?,?,?,?,?,?,?)",
                       (round_id, float(watched_h), version, int(champion_n), int(sentinel_n), int(candidates),
                        datetime.now(timezone.utc).isoformat()))
    store.conn.commit()


def counts(store, *, version: int | None = None, last: int = 200, before_round: int | None = None) -> tuple[list[int], list[int]]:
    """(champion counts, sentinel counts) of the last `last` recorded rounds (optionally of one version)."""
    migrate(store)
    sql, args = "select champion_n, sentinel_n from improve_rounds where 1=1", []
    if version is not None:
        sql += " and version=?"
        args.append(version)
    if before_round is not None:
        sql += " and round_id<?"
        args.append(before_round)
    rows = store.conn.execute(sql + " order by round_id desc limit ?", (*args, int(last))).fetchall()
    rows.reverse()
    return [r["champion_n"] for r in rows], [r["sentinel_n"] for r in rows]


def sentinel_arm_ids(pb: Playbook, sources: list[str]) -> set[str]:
    """Arm ids of the sentinel query items (dh/scheduler.build_arms naming)."""
    out = set()
    for it in pb.items:
        if it.kind != "query" or not it.sentinel:
            continue
        text = " ".join(str(it.value).lower().split())
        t = it.target.strip().lower()
        for src in (sources if t in ("all", "*") else [x.strip() for x in t.split(",") if x.strip()]):
            out.add(ledger.arm_id(src, text))
    return out


def round_window(store, round_id: int) -> tuple[str, str] | None:
    r = store.conn.execute("select started_at, ended_at from rounds where id=?", (round_id,)).fetchone()
    if r is None or not r["started_at"]:
        return None
    return r["started_at"], r["ended_at"] or r["started_at"]


def evaluated_in_round(store, round_id: int) -> list[int]:
    """Listings the round (re)evaluated: the candidate stream shadow mode scores."""
    w = round_window(store, round_id)
    if w is None:
        return []
    try:
        rows = store.conn.execute("select listing_id from evaluations where at >= ? and at <= ? order by listing_id", w)
    except Exception:  # noqa: BLE001 - no evaluations table yet (no round ever ran)
        return []
    return [r["listing_id"] for r in rows]


def arms_of(store, lids: list[int]) -> dict[int, set[str]]:
    ledger.migrate(store)
    out: dict[int, set[str]] = {lid: set() for lid in lids}
    for lid in lids:
        for r in store.conn.execute("select arm from listing_arms where listing_id=?", (lid,)):
            out[lid].add(r["arm"])
    return out


async def round_counts(store, round_id: int, sentinel_pb: Playbook, sources: list[str], *, cfg=None,
                       now: datetime | None = None) -> tuple[int, int, list[int]]:
    """(champion_n, sentinel_n, evaluated listing ids) for one finished round (see module doc)."""
    from .replay import judge_listings
    w = round_window(store, round_id)
    if w is None:
        return 0, 0, []
    lids = evaluated_in_round(store, round_id)
    sent_arms = sentinel_arm_ids(sentinel_pb, sources)
    arms = arms_of(store, lids)
    sent = [lid for lid in lids if arms[lid] & sent_arms]
    champ_lids = {lid for lid in lids if arms[lid] - sent_arms}
    champion_n = 0
    for r in store.conn.execute("select listing_ref, tier from deals where created_at >= ? and created_at <= ?", w):
        if r["tier"] in (1, 2, 3, 4) and r["listing_ref"] in champ_lids:
            champion_n += 1
    outs = await judge_listings(sentinel_pb, store, sent, now=now or _t(w[1]), cfg=cfg) if sent else {}
    sentinel_n = sum(1 for o in outs.values() if o.tier in (1, 2, 3, 4))
    return champion_n, sentinel_n, lids


def default_sources(cfg=None) -> list[str]:
    """Enabled source names (for 'all' sentinel targets); the sentinel queries name their sources explicitly."""
    try:
        from ..sources import registry
        return list(registry(cfg, Path(cfg._root)).keys()) if cfg is not None else []
    except Exception:  # noqa: BLE001 - registry needs config; explicit targets work without it
        return []
