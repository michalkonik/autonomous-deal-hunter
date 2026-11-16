"""Shadow mode (spec §9.3 step 3.5; research §4.2): the challenger scores the SAME candidate stream as the champion,
every round, and never acts. Only disagreements matter, and only once a verified outcome labels them.

- `score_round` judges the round's evaluated listings with both playbooks (offline judge, the same current cached
  LLM output on both sides) and stores every disagreement in `shadow_results` (metric 'disagreement'), plus one
  'round' row with the number of listings scored. Prompt changes never come here (owner review only).
- Labels are TRUTHS, never winners, and come only from verified outcomes (spec §9.5):
  * the owner's marks on the deal of that listing (Bought / Sold -> 'deal'; Skipped for a wrongness -> 'not_deal');
  * the owner's adjudication of a disagreement (`label`, from the digest);
  * the clears-at ledger, one-sided: a listing that one side called a deal and that stayed listed, unsold, for
    UNSOLD_DAYS at the same price was not a deal (mis-valued, defective or a scam: all mean 'not_deal'). A
    disappearance is NOT used: it cannot tell a sale from a removed scam.
  Code then decides which side was right. The agent's own opinion never labels anything.
- `decide` applies the frozen single-look rule (gates.shadow_verdict) and the Twyman audit flag.
"""
from __future__ import annotations

import json
import weakref
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from evals.labels import is_false_positive, is_true_positive

from ..verify import load_listing
from . import gates
from .playbook import Playbook
from .replay import Judge, Outcome, cached_extraction, verified_listings

COLUMNS = ("round_id integer", "listing_id integer", "kind text", "champion text", "challenger text",
           "truth text", "label_source text", "labelled_at text", "winner text")
UNSOLD_DAYS = 7


@dataclass
class Disagreement:
    id: int | None
    proposal_id: int
    round_id: int | None
    listing_id: int
    kind: str  # key | exclusion | deal | tier
    champion: Outcome
    challenger: Outcome
    truth: str | None = None
    winner: str | None = None  # challenger | champion | tie


_MIGRATED: "weakref.WeakSet" = weakref.WeakSet()


def migrate(store) -> None:
    """Once per store object (a step's transaction is never committed by a schema check)."""
    if store in _MIGRATED:
        return
    have = {r["name"] for r in store.conn.execute("pragma table_info(shadow_results)")}
    for col in COLUMNS:
        if col.split()[0] not in have:
            store.ensure_table(f"alter table shadow_results add column {col}")
    _MIGRATED.add(store)


def kind_of(a: Outcome, b: Outcome) -> str | None:
    if a.key != b.key:
        return "key"
    if a.excluded != b.excluded:
        return "exclusion"
    if a.deal != b.deal:
        return "deal"
    if a.tier != b.tier:
        return "tier"
    return None


async def score_round(champion: Playbook, challenger: Playbook, round_candidates: list[int], *, store,
                      proposal_id: int, round_id: int | None = None, cfg=None,
                      now: datetime | None = None, commit: bool = True) -> list[Disagreement]:
    """Score the round's candidates (listing ids) with both playbooks; store and return the disagreements.
    Judgment changes only (alias, scam keyword): both sides use the same current cached LLM output. A prompt change
    never reaches shadow (owner review only, gates.evaluate -> 'owner_review')."""
    migrate(store)
    now = now or datetime.now(timezone.utc)
    jc = Judge(champion, store, cfg=cfg, now=now)
    jh = Judge(challenger, store, cfg=cfg, now=now)
    verified = verified_listings(store)
    out: list[Disagreement] = []
    scored = 0
    for lid in round_candidates:
        l = load_listing(store, lid)
        if l is None:
            continue
        scored += 1
        cached = cached_extraction(store, lid, l.title)
        v = 0.0 if lid in verified else None
        a = await jc.judge(l, lid=lid, cached=cached, verified_age_s=v)
        b = await jh.judge(l, lid=lid, cached=cached, verified_age_s=v)
        k = kind_of(a, b)
        if k is None:
            continue
        cur = store.conn.execute(
            "insert into shadow_results (proposal_id, metric, value, round_id, listing_id, kind, champion, challenger) "
            "values (?,?,?,?,?,?,?,?)", (proposal_id, "disagreement", 1.0, round_id, lid, k,
                                         json.dumps(a.to_dict()), json.dumps(b.to_dict())))
        out.append(Disagreement(cur.lastrowid, proposal_id, round_id, lid, k, a, b))
    store.conn.execute("insert into shadow_results (proposal_id, metric, value, round_id) values (?,?,?,?)",
                       (proposal_id, "round", float(scored), round_id))
    if commit:
        store.conn.commit()
    return out


def _right(o: Outcome, truth: str) -> bool:
    if truth == "deal":
        return o.deal
    if truth == "not_deal":
        return not o.deal
    if truth.startswith("key:"):
        k = truth[4:] or None
        return o.key == (None if k in (None, "none", "null") else k)
    raise ValueError(f"unknown truth {truth!r}")


def winner_of(champion: Outcome, challenger: Outcome, truth: str) -> str:
    a, b = _right(champion, truth), _right(challenger, truth)
    return "challenger" if b and not a else "champion" if a and not b else "tie"


def label(store, disagreement_id: int, truth: str, source: str = "owner", commit: bool = True) -> str:
    """Record a verified truth ('deal' | 'not_deal' | 'key:<key or none>') for one disagreement; returns the winner."""
    migrate(store)
    r = store.conn.execute("select champion, challenger from shadow_results where id=? and metric='disagreement'",
                           (disagreement_id,)).fetchone()
    if r is None:
        raise KeyError(disagreement_id)
    w = winner_of(Outcome.from_dict(json.loads(r["champion"])), Outcome.from_dict(json.loads(r["challenger"])), truth)
    store.conn.execute("update shadow_results set truth=?, label_source=?, labelled_at=?, winner=? where id=?",
                       (truth, source, datetime.now(timezone.utc).isoformat(), w, disagreement_id))
    if commit:
        store.conn.commit()
    return w


def pending(store, proposal_id: int | None = None) -> list[Disagreement]:
    """Unlabelled disagreements (the owner's adjudication digest)."""
    migrate(store)
    sql = "select * from shadow_results where metric='disagreement' and truth is null"
    args: tuple = ()
    if proposal_id is not None:
        sql += " and proposal_id=?"
        args = (proposal_id,)
    return [Disagreement(r["id"], r["proposal_id"], r["round_id"], r["listing_id"], r["kind"],
                         Outcome.from_dict(json.loads(r["champion"])), Outcome.from_dict(json.loads(r["challenger"])))
            for r in store.conn.execute(sql + " order by id", args)]


def auto_label(store, proposal_id: int, now: datetime | None = None, commit: bool = True) -> int:
    """Label pending disagreements from verified outcomes (module doc). Returns how many were labelled."""
    now = now or datetime.now(timezone.utc)
    n = 0
    for d in pending(store, proposal_id):
        truth, source = None, None
        for m in store.conn.execute("select m.status, m.note from marks m join deals x on x.id=m.deal_id "
                                    "where x.listing_ref=? order by m.id", (d.listing_id,)):
            if is_true_positive(m["status"]):
                truth, source = "deal", "owner_mark"
            elif is_false_positive(m["status"], m["note"]) and truth is None:
                truth, source = "not_deal", "owner_mark"
        if truth is None and d.kind != "key" and (d.champion.deal or d.challenger.deal):
            l = store.conn.execute("select first_seen_at, last_seen_at, price from listings where id=?",
                                   (d.listing_id,)).fetchone()
            if l is not None and l["first_seen_at"] and l["last_seen_at"]:
                span = datetime.fromisoformat(l["last_seen_at"]) - datetime.fromisoformat(l["first_seen_at"])
                prices = {r["price"] for r in store.conn.execute(
                    "select price from observations where listing_id=?", (d.listing_id,))}
                if span >= timedelta(days=UNSOLD_DAYS) and len(prices) <= 1:
                    truth, source = "not_deal", "ledger_unsold"
        if truth is not None:
            label(store, d.id, truth, source, commit=commit)
            n += 1
    return n


def tally(store, proposal_id: int) -> dict:
    migrate(store)
    w = {r["winner"]: r["n"] for r in store.conn.execute(
        "select winner, count(*) n from shadow_results where proposal_id=? and metric='disagreement' "
        "and winner is not null group by winner", (proposal_id,))}
    total = store.conn.execute("select count(*) from shadow_results where proposal_id=? and metric='disagreement'",
                               (proposal_id,)).fetchone()[0]
    r = store.conn.execute("select count(*) n, coalesce(sum(value),0) s from shadow_results where proposal_id=? "
                           "and metric='round'", (proposal_id,)).fetchone()
    return {"wins": w.get("challenger", 0), "losses": w.get("champion", 0), "ties": w.get("tie", 0),
            "disagreements": total, "unlabelled": total - sum(w.values()), "rounds": r["n"], "scored": int(r["s"])}


def decide(store, proposal_id: int, watched_hours: float) -> tuple[str, dict]:
    """'promote' | 'reject' | 'wait' (gates.shadow_verdict on labelled disagreements) plus the tally."""
    t = tally(store, proposal_id)
    verdict, d = gates.shadow_verdict(t["wins"], t["losses"], t["rounds"], watched_hours)
    d.update({k: t[k] for k in ("ties", "disagreements", "unlabelled", "scored")})
    if t["scored"] and t["disagreements"] / t["scored"] > gates.SHADOW_TWYMAN_RATE:
        d["twyman"] = (f"{t['disagreements']} of {t['scored']} judgments changed: audit before trusting "
                       "(a logging bug or a definition change is likelier than a huge real gain)")
        if verdict == "promote":
            verdict = "reject"
            d["why"] = "twyman: a one-item delta should not change this many judgments"
    return verdict, d


def candidates_from_round(store, round_id: int) -> list[int]:
    from .monitor import evaluated_in_round
    return evaluated_in_round(store, round_id)
