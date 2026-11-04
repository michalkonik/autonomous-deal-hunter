"""SQLite store (WAL). Decimals are stored as TEXT, datetimes as ISO-8601 TEXT, lists as JSON."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from .models import Deal, Extraction, Listing

SCHEMA = """
create table if not exists listings (
  id integer primary key,
  source text not null, source_id text not null, url text, title text, description text,
  price text, currency text, country text, city text, lat real, lon real,
  seller_hash text, seller_age_days integer, seller_ratings integer, seller_score real,
  ships integer, pickup_only integer, is_business integer, photos text,
  posted_at text, first_seen_at text, last_seen_at text, raw_path text,
  contact_in_text integer not null default 0,
  unique (source, source_id)
);
create table if not exists observations (
  id integer primary key, listing_id integer not null references listings(id),
  price text not null, seen_at text not null
);
create index if not exists idx_obs_listing on observations(listing_id, seen_at);
create table if not exists windows (
  id integer primary key, source text not null, opened_at text not null, closed_at text
);
create table if not exists extractions (
  listing_id integer primary key references listings(id),
  key text, category text, certainty real, condition text, defects text, box_only integer,
  for_parts integer, wanted_ad integer, bundle_items text, method text
);
create table if not exists deals (
  id integer primary key, listing_ref integer not null, key text, tier integer, star integer,
  effort text, confidence text, cons_net_pln text, exp_net_pln text, cons_net_usd text,
  exp_net_usd text, capital_usd text, breakdown_json text, evidence_json text, reasons text,
  created_at text, verified_at text, status text
);
create table if not exists marks (
  id integer primary key, deal_id integer not null, status text, price_pln text, note text,
  source text, created_at text default current_timestamp
);
create table if not exists comps (
  id integer primary key, key text not null, condition text, channel text, price_pln text,
  sold_at text, kind text, ref text
);
create index if not exists idx_comps on comps(key, condition, channel, sold_at);
create table if not exists rounds (
  id integer primary key, started_at text, ended_at text, source text,
  listings_seen integer default 0, deals_found integer default 0, notes text
);
create table if not exists arm_stats (
  arm text primary key, pulls integer default 0, reward real default 0
);
create table if not exists costs (
  id integer primary key, round_id integer, kind text, amount_pln text, units real
);
create table if not exists health (
  id integer primary key, source text, kind text, detail text, at text default current_timestamp
);
create table if not exists fx (
  date text not null, currency text not null, rate_pln text not null, effective_date text,
  primary key (date, currency)
);
create table if not exists playbook_versions (
  id integer primary key, version text, content text, created_at text default current_timestamp
);
create table if not exists proposals (
  id integer primary key, kind text, payload text, status text, created_at text default current_timestamp
);
create table if not exists shadow_results (
  id integer primary key, proposal_id integer, metric text, value real, created_at text default current_timestamp
);
"""

_DEAL_FILTERS = {"tier", "status", "key", "star", "effort", "confidence", "listing_ref"}
_DEC = ("cons_net_pln", "exp_net_pln", "cons_net_usd", "exp_net_usd", "capital_usd")


def _iso(t: datetime | str | None) -> str | None:
    """Normalise to timezone-aware UTC ISO-8601. Strings are parsed; naive values are rejected."""
    if t is None:
        return None
    dt = datetime.fromisoformat(t) if isinstance(t, str) else t
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"naive datetime not allowed: {t!r}")
    return dt.astimezone(timezone.utc).isoformat()


class Store:
    def __init__(self, path: Path | str):
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("pragma journal_mode=wal")
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        if "contact_in_text" not in {r["name"] for r in self.conn.execute("pragma table_info(listings)")}:
            self.ensure_table("alter table listings add column contact_in_text integer not null default 0")
        have = {r["name"] for r in self.conn.execute("pragma table_info(extractions)")}
        for col in ("prompt_sha text", "extracted_at text"):  # which extraction prompt produced an LLM result, when
            if col.split()[0] not in have:
                self.ensure_table(f"alter table extractions add column {col}")

    def ensure_table(self, sql: str) -> None:
        self.conn.execute(sql)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # listings
    def upsert_listing(self, l: Listing) -> int:
        seen = _iso(l.seen_at)
        self.conn.execute(
            """insert into listings (source, source_id, url, title, description, price, currency, country,
              city, lat, lon, seller_hash, seller_age_days, seller_ratings, seller_score, ships, pickup_only,
              is_business, photos, posted_at, first_seen_at, last_seen_at, raw_path, contact_in_text)
              values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
              on conflict(source, source_id) do update set
              url=excluded.url, title=excluded.title, description=excluded.description, price=excluded.price,
              currency=excluded.currency, country=excluded.country, city=excluded.city, lat=excluded.lat,
              lon=excluded.lon, seller_hash=excluded.seller_hash, seller_age_days=excluded.seller_age_days,
              seller_ratings=excluded.seller_ratings, seller_score=excluded.seller_score, ships=excluded.ships,
              pickup_only=excluded.pickup_only, is_business=excluded.is_business, photos=excluded.photos,
              posted_at=coalesce(excluded.posted_at, posted_at),
              last_seen_at=max(coalesce(last_seen_at,''), excluded.last_seen_at),
              raw_path=coalesce(excluded.raw_path, raw_path),
              contact_in_text=max(contact_in_text, excluded.contact_in_text)""",
            (l.source, l.source_id, l.url, l.title, l.description, str(l.price), l.currency, l.country, l.city,
             l.lat, l.lon, l.seller_hash, l.seller_age_days, l.seller_ratings, l.seller_score,
             None if l.ships is None else int(l.ships), int(l.pickup_only),
             None if l.is_business is None else int(l.is_business), json.dumps(l.photos),
             _iso(l.posted_at), seen, seen, l.raw_path, int(bool(l.contact_in_text))),
        )
        self.conn.commit()
        return self.conn.execute("select id from listings where source=? and source_id=?",
                                 (l.source, l.source_id)).fetchone()["id"]

    def record_observation(self, listing_id: int, price: Decimal, seen_at: datetime) -> None:
        self.conn.execute("insert into observations (listing_id, price, seen_at) values (?,?,?)",
                          (listing_id, str(price), _iso(seen_at)))
        self.conn.execute("update listings set last_seen_at=max(coalesce(last_seen_at,''), ?) where id=?",
                          (_iso(seen_at), listing_id))
        self.conn.commit()

    # observation windows
    def open_window(self, source: str, ts: datetime) -> int:
        cur = self.conn.execute("insert into windows (source, opened_at) values (?,?)", (source, _iso(ts)))
        self.conn.commit()
        return cur.lastrowid

    def close_window(self, window_id: int, ts: datetime) -> None:
        self.conn.execute("update windows set closed_at=? where id=?", (_iso(ts), window_id))
        self.conn.commit()

    def open_windows(self) -> list[sqlite3.Row]:
        return self.conn.execute("select * from windows where closed_at is null order by id").fetchall()

    def listings_missing_since(self, source: str, ts: datetime) -> list[int]:
        rows = self.conn.execute(
            "select id from listings where source=? and last_seen_at < ? order by id", (source, _iso(ts)))
        return [r["id"] for r in rows]

    # extractions
    def save_extraction(self, listing_id: int, e: Extraction) -> None:
        """Upsert. An LLM result carries the extraction prompt's sha in its notes ("prompt_sha:<sha>", set by
        dh.extract); it is stored with the time. Re-saving a cached LLM result without that note keeps the stored
        sha; a rules result clears it."""
        sha = next((n.split(":", 1)[1] for n in e.notes if n.startswith("prompt_sha:")), None)
        at = datetime.now(timezone.utc).isoformat() if sha else None
        self.conn.execute(
            """insert into extractions (listing_id, key, category, certainty, condition, defects,
               box_only, for_parts, wanted_ad, bundle_items, method, prompt_sha, extracted_at)
               values (?,?,?,?,?,?,?,?,?,?,?,?,?)
               on conflict(listing_id) do update set key=excluded.key, category=excluded.category,
               certainty=excluded.certainty, condition=excluded.condition, defects=excluded.defects,
               box_only=excluded.box_only, for_parts=excluded.for_parts, wanted_ad=excluded.wanted_ad,
               bundle_items=excluded.bundle_items, method=excluded.method,
               prompt_sha=case when excluded.method='llm' then coalesce(excluded.prompt_sha, extractions.prompt_sha) end,
               extracted_at=case when excluded.method='llm'
                                 then coalesce(excluded.extracted_at, extractions.extracted_at) end""",
            (listing_id, e.key, e.category, e.certainty, e.condition, json.dumps(e.defects), int(e.box_only),
             int(e.for_parts), int(e.wanted_ad), json.dumps(e.bundle_items), e.method, sha, at))
        self.conn.commit()

    # deals
    def save_deal(self, d: Deal) -> int:
        cur = self.conn.execute(
            """insert into deals (listing_ref, key, tier, star, effort, confidence, cons_net_pln, exp_net_pln,
               cons_net_usd, exp_net_usd, capital_usd, breakdown_json, evidence_json, reasons, created_at,
               verified_at, status) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (d.listing_ref, d.key, d.tier, int(d.star), d.effort, d.confidence, str(d.cons_net_pln),
             str(d.exp_net_pln), str(d.cons_net_usd), str(d.exp_net_usd), str(d.capital_usd), d.breakdown_json,
             d.evidence_json, json.dumps(d.reasons), _iso(d.created_at), _iso(d.verified_at), d.status))
        self.conn.commit()
        return cur.lastrowid

    @staticmethod
    def _deal(r: sqlite3.Row) -> Deal:
        kw = {k: r[k] for k in r.keys()}
        for k in _DEC:
            kw[k] = Decimal(kw[k])
        kw["star"] = bool(kw["star"])
        kw["reasons"] = json.loads(kw["reasons"])
        kw["created_at"] = datetime.fromisoformat(kw["created_at"])
        if kw["verified_at"]:
            kw["verified_at"] = datetime.fromisoformat(kw["verified_at"])
        return Deal(**kw)

    def deals(self, filters: dict | None = None) -> list[Deal]:
        filters = filters or {}
        bad = set(filters) - _DEAL_FILTERS
        if bad:
            raise ValueError(f"unknown deal filter(s): {sorted(bad)}")
        where = " and ".join(f"{k}=?" for k in filters)
        args = [int(v) if isinstance(v, bool) else v for v in filters.values()]
        sql = "select * from deals" + (f" where {where}" if where else "") + " order by id"
        return [self._deal(r) for r in self.conn.execute(sql, args)]

    def get_deal(self, deal_id: int) -> Deal | None:
        r = self.conn.execute("select * from deals where id=?", (deal_id,)).fetchone()
        return self._deal(r) if r else None

    def add_mark(self, deal_id: int, status: str, price_pln: Decimal | None, note: str, source: str) -> None:
        self.conn.execute("insert into marks (deal_id, status, price_pln, note, source) values (?,?,?,?,?)",
                          (deal_id, status, None if price_pln is None else str(price_pln), note, source))
        self.conn.commit()

    # comps
    def add_comp(self, key: str, condition: str, channel: str, price_pln: Decimal, sold_at: datetime,
                 kind: str, ref: str) -> None:
        self.conn.execute("insert into comps (key, condition, channel, price_pln, sold_at, kind, ref) values (?,?,?,?,?,?,?)",
                          (key, condition, channel, str(price_pln), _iso(sold_at), kind, ref))
        self.conn.commit()

    def comps(self, key: str, condition: str | None = None, channel: str | None = None,
              since: datetime | None = None) -> list[sqlite3.Row]:
        sql, args = "select * from comps where key=?", [key]
        for col, v in (("condition", condition), ("channel", channel)):
            if v is not None:
                sql += f" and {col}=?"
                args.append(v)
        if since is not None:
            sql += " and sold_at >= ?"
            args.append(_iso(since))
        return self.conn.execute(sql + " order by sold_at", args).fetchall()

    # bookkeeping
    def log_round(self, started_at: datetime, ended_at: datetime | None = None, source: str | None = None,
                  listings_seen: int = 0, deals_found: int = 0, notes: str = "") -> int:
        cur = self.conn.execute(
            "insert into rounds (started_at, ended_at, source, listings_seen, deals_found, notes) values (?,?,?,?,?,?)",
            (_iso(started_at), _iso(ended_at), source, listings_seen, deals_found, notes))
        self.conn.commit()
        return cur.lastrowid

    def add_cost(self, round_id: int, kind: str, amount_pln: Decimal, units: float) -> None:
        self.conn.execute("insert into costs (round_id, kind, amount_pln, units) values (?,?,?,?)",
                          (round_id, kind, str(amount_pln), units))
        self.conn.commit()

    def add_health(self, source: str, kind: str, detail: str) -> None:
        self.conn.execute("insert into health (source, kind, detail) values (?,?,?)", (source, kind, detail))
        self.conn.commit()
