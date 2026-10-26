"""Itemised playbook, typed deltas and versions (spec §9.1, §9.3; research §4.3).

The playbook is a list of items (`playbook/items.yaml`, loaded by dh/playbook_io.py). The improvement loop never
rewrites it: it proposes ONE typed delta (add / update / remove of one item) at a time, and code merges it.

- Items are immutable values; `Playbook.apply(delta)` returns a NEW playbook and never touches the old one.
- Every delta carries its inverse, computed by code from the playbook it applies to (never trusted from the LLM),
  so a rollback is `apply(delta.inverse)`.
- Caps: <= 400 items, a prompt item <= 1500 characters, short values (query, alias, scam keyword) <= 120.
- Sentinel items (`sentinel: true`, the control-group queries and the reflection prompts) are immutable: no delta
  may add, update or remove one. They are defined by a human in the file.
- Versions live in the store table `playbook_versions` (full YAML snapshot + the delta + a unified diff). The ACTIVE
  playbook is the latest version with status `promoted`; with no versions it is the file.

Write scope (frozen layer by construction, research §1 rule 4): the only file this package writes is
`<root>/playbook/items.yaml` (activation) and the same name inside a private temporary directory (materialised
challengers for evaluation). `_guarded_write` refuses everything else, including every path in
`dh.config.FROZEN_PATHS`.
"""
from __future__ import annotations

import copy
import difflib
import hashlib
import json
import os
import re
import tempfile
import weakref
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from .. import config as dhconfig
from ..playbook_io import load_items

ROOT = Path(__file__).resolve().parents[2]

KINDS = ("query", "alias", "scam_kw", "prompt", "cadence")
OPS = ("add", "update", "remove")
MAX_ITEMS = 400
MAX_PROMPT_CHARS = 1500
MAX_SHORT_CHARS = 120
# prompt items a delta may touch (spec §9.1: extraction and triage prompts); every other prompt is human-owned
EDITABLE_PROMPT_TARGETS = frozenset({"llm.extract", "llm.triage"})
# fixed consumers per kind (query and cadence name sources instead)
KIND_TARGETS = {"scam_kw": frozenset({"risk"}), "alias": frozenset({"match"})}
PROMPT_GUARDS = ("<listing>", "untrusted")  # every prompt keeps its prompt-injection guard
CADENCE_MIN, CADENCE_MAX = 10, 1440  # minutes between searches of one source
ID_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z0-9_.-]{1,60}$")
SOURCE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
ALIAS_SIDE = re.compile(r"^[a-z0-9][a-z0-9 .+/-]{0,39}$")
ALIAS_SEP = "=>"
CORE = ("id", "kind", "target", "provenance", "helpful", "harmful", "sentinel")
EXTRA_KEYS = frozenset({"min_pln", "max_pln", "category", "note"})

VERSIONS_COLUMNS = ("status text", "parent integer", "delta text", "diff text", "sha text", "proposal_id integer",
                    "decided_at text", "note text")
# Human edits made to playbook/items.yaml at install time, before any version existed. They did not go through the
# loop, so the first baseline says so (every later human edit is detected and stored as an "owner edit" baseline).
INSTALL_NOTE = ("baseline at install, with two owner-approved manual edits: prompt.extract trimmed from 1667 to "
                "1500 chars to fit the prompt cap (manual trim at install; same rules, tighter wording); the broad "
                "scam keyword 'pudelko po' replaced by the narrow phrases 'sprzedam/samo/tylko pudelko po' (an "
                "original box sold WITH the card is normal; golden case g-box-included-pl-verified)")
COUNTERS_SQL = """create table if not exists playbook_counters (
  item_id text primary key, helpful integer not null default 0, harmful integer not null default 0)"""
# a version's life: shadow -> promoted | rejected; promoted -> failed_in_prod (rolled back)
STATUSES = ("baseline", "candidate", "shadow", "promoted", "rejected", "failed_in_prod", "aborted")


def prompt_sha(pb: "Playbook", item_id: str = "prompt.extract") -> str | None:
    it = pb.get(item_id)
    return None if it is None else hashlib.sha256(str(it.value).encode()).hexdigest()[:16]


def door_of(kind: str) -> str:
    """Which evaluation path proves a change of this item kind (research §4.2):
    judgment = alias / scam keyword (offline gates, then shadow, then probation);
    llm = prompt (gates cannot evaluate it without live LLM calls: owner review only, then probation);
    arm = query / cadence (online only: the bandit's trial budget under probation)."""
    return {"alias": "judgment", "scam_kw": "judgment", "prompt": "llm"}.get(kind, "arm")


class PlaybookError(ValueError):
    """A delta or a playbook breaks the schema, a cap or the sentinel rule."""


# --------------------------------------------------------------------------------------------- items


@dataclass(frozen=True)
class Item:
    id: str
    kind: Literal["query", "alias", "scam_kw", "prompt", "cadence"]
    target: str
    value: Any
    provenance: str = "seed"
    helpful: int = 0
    harmful: int = 0
    sentinel: bool = False
    extra: dict = field(default_factory=dict, compare=True, hash=False)  # min_pln, max_pln, category, note

    @classmethod
    def from_dict(cls, d: dict) -> "Item":
        missing = [k for k in ("id", "kind", "target", "value") if k not in d]
        if missing:
            raise PlaybookError(f"item {d.get('id')!r} missing {missing}")
        extra = {k: v for k, v in d.items() if k not in CORE and k != "value"}
        return cls(id=str(d["id"]), kind=d["kind"], target=str(d["target"]), value=d["value"],
                   provenance=str(d.get("provenance", "seed")), helpful=int(d.get("helpful", 0) or 0),
                   harmful=int(d.get("harmful", 0) or 0), sentinel=bool(d.get("sentinel", False)), extra=dict(extra))

    def to_dict(self) -> dict:
        d = {"id": self.id, "kind": self.kind, "target": self.target, "provenance": self.provenance,
             "helpful": self.helpful, "harmful": self.harmful, "sentinel": self.sentinel}
        d.update({k: self.extra[k] for k in sorted(self.extra)})
        d["value"] = self.value
        return d

    def content(self) -> dict:
        """What the item DOES (counters and provenance excluded): used to tell real changes from bookkeeping."""
        return {"kind": self.kind, "target": self.target, "value": self.value, "sentinel": self.sentinel,
                **{k: v for k, v in self.extra.items() if k != "note"}}


def alias_pair(value: Any) -> tuple[str, str]:
    """'from => to' -> (from, to), both lowercase ASCII phrases. PlaybookError when malformed."""
    s = str(value)
    if s.count(ALIAS_SEP) != 1:
        raise PlaybookError(f"alias value must be 'from {ALIAS_SEP} to', got {s!r}")
    a, b = (" ".join(x.split()) for x in s.split(ALIAS_SEP))
    for side in (a, b):
        if not ALIAS_SIDE.match(side):
            raise PlaybookError(f"alias side {side!r} must be 1-40 chars of [a-z0-9 .+/-]")
    if a == b:
        raise PlaybookError("alias maps a phrase to itself")
    return a, b


def item_errors(it: Item) -> list[str]:
    """Schema and per-item caps. Empty list = valid."""
    e: list[str] = []
    if it.kind not in KINDS:
        return [f"{it.id}: unknown kind {it.kind!r}"]
    if not ID_RE.match(it.id):
        e.append(f"{it.id}: id must look like '<kind>.<name>' ([a-z0-9_.-])")
    unknown = set(it.extra) - EXTRA_KEYS
    if unknown:
        e.append(f"{it.id}: unknown fields {sorted(unknown)}")
    if it.helpful < 0 or it.harmful < 0:
        e.append(f"{it.id}: negative counter")
    v = it.value
    if it.kind == "prompt":
        if not isinstance(v, str) or not v.strip():
            e.append(f"{it.id}: prompt value must be non-empty text")
        else:
            if len(v) > MAX_PROMPT_CHARS:
                e.append(f"{it.id}: prompt is {len(v)} chars > cap {MAX_PROMPT_CHARS}")
            missing = [g for g in PROMPT_GUARDS if g not in v]
            if missing:
                e.append(f"{it.id}: prompt lost its injection guard {missing}")
        if not it.target.startswith("llm."):
            e.append(f"{it.id}: prompt target must be llm.<role>")
        return e
    if it.kind == "cadence":
        if not SOURCE_RE.match(it.target):
            e.append(f"{it.id}: cadence target must be one source name")
        if isinstance(v, bool) or not isinstance(v, int) or not CADENCE_MIN <= v <= CADENCE_MAX:
            e.append(f"{it.id}: cadence value must be minutes {CADENCE_MIN}..{CADENCE_MAX}")
        return e
    if not isinstance(v, str) or not v.strip():
        return e + [f"{it.id}: value must be non-empty text"]
    if len(v) > MAX_SHORT_CHARS:
        e.append(f"{it.id}: value is {len(v)} chars > cap {MAX_SHORT_CHARS}")
    if it.kind in KIND_TARGETS and it.target not in KIND_TARGETS[it.kind]:
        e.append(f"{it.id}: {it.kind} target must be one of {sorted(KIND_TARGETS[it.kind])}")
    if it.kind == "alias":
        try:
            alias_pair(v)
        except PlaybookError as err:
            e.append(f"{it.id}: {err}")
    if it.kind == "query":
        t = it.target.strip().lower()
        if t not in ("all", "*") and not all(SOURCE_RE.match(x.strip()) for x in t.split(",") if x.strip()):
            e.append(f"{it.id}: query target must be 'all' or a comma list of source names")
        for k in ("min_pln", "max_pln"):
            if k in it.extra and (isinstance(it.extra[k], bool) or not isinstance(it.extra[k], (int, float))
                                  or it.extra[k] < 0):
                e.append(f"{it.id}: {k} must be a non-negative number")
    return e


# --------------------------------------------------------------------------------------------- deltas


@dataclass
class Delta:
    """One typed change. `inverse` undoes it on the playbook it was made for (filled by Playbook.make_delta)."""
    op: Literal["add", "update", "remove"]
    item: Item
    hypothesis: str = ""
    evidence: list[str] = field(default_factory=list)
    predicted_effect: str = ""
    inverse: "Delta | None" = None
    position: int | None = None  # add only: insert index (None = append); set on the inverse of a remove

    def to_dict(self, with_inverse: bool = True) -> dict:
        d = {"op": self.op, "item": self.item.to_dict(), "hypothesis": self.hypothesis,
             "evidence": list(self.evidence), "predicted_effect": self.predicted_effect, "position": self.position}
        if with_inverse and self.inverse is not None:
            d["inverse"] = self.inverse.to_dict(with_inverse=False)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Delta":
        if d.get("op") not in OPS:
            raise PlaybookError(f"delta op must be one of {OPS}, got {d.get('op')!r}")
        if not isinstance(d.get("item"), dict):
            raise PlaybookError("delta item must be a mapping")
        ev = d.get("evidence") or []
        if not isinstance(ev, list):
            raise PlaybookError("delta evidence must be a list")
        inv = d.get("inverse")
        pos = d.get("position")
        return cls(op=d["op"], item=Item.from_dict(d["item"]), hypothesis=str(d.get("hypothesis") or ""),
                   evidence=[str(x) for x in ev], predicted_effect=str(d.get("predicted_effect") or ""),
                   inverse=cls.from_dict(inv) if isinstance(inv, dict) else None,
                   position=None if pos is None else int(pos))

    def summary(self) -> str:
        v = self.item.value if self.item.kind != "prompt" else f"<{len(str(self.item.value))} chars>"
        return f"{self.op} {self.item.id} ({self.item.kind} -> {self.item.target}): {v!r}"

    def door(self) -> str:
        return door_of(self.item.kind)


# --------------------------------------------------------------------------------------------- playbook

_HEADER = ("# Playbook items (spec §9.1). Written by dh/improve/playbook.py from the ACTIVE version in the store:\n"
           "# edit it only through gated deltas (docs/SELF-IMPROVEMENT.md). Kinds: query (search text; target 'all' or\n"
           "# sources; optional min_pln/max_pln), alias ('from => to' text rewrite before matching; target match),\n"
           "# scam_kw (soft scam phrase, matched without diacritics; target risk), prompt (LLM system prompt; target\n"
           "# llm.<role>), cadence (minutes between searches of a source). sentinel: true = immutable control item.\n")


class _Dumper(yaml.SafeDumper):
    pass


def _str_rep(dumper: yaml.SafeDumper, s: str):
    if "\n" in s:
        return dumper.represent_scalar("tag:yaml.org,2002:str", s, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", s)


_Dumper.add_representer(str, _str_rep)


class Playbook:
    """An ordered, immutable list of items, optionally tied to a stored version id."""

    def __init__(self, items: list[Item], version: int | None = None):
        self._items = tuple(items)
        self.version = version

    # ------------------------------------------------------------ access
    @property
    def items(self) -> list[Item]:
        return list(self._items)

    def ids(self) -> list[str]:
        return [i.id for i in self._items]

    def get(self, item_id: str) -> Item | None:
        return next((i for i in self._items if i.id == item_id), None)

    def of_kind(self, kind: str) -> list[Item]:
        return [i for i in self._items if i.kind == kind]

    def as_dicts(self) -> list[dict]:
        """The shape dh/playbook_io.load_items returns (what the pipeline consumes)."""
        return [copy.deepcopy(i.to_dict()) for i in self._items]

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Playbook) and self._items == other._items

    def __hash__(self) -> int:
        return hash(self.sha())

    def __len__(self) -> int:
        return len(self._items)

    # ------------------------------------------------------------ load
    @classmethod
    def from_file(cls, root: Path = ROOT) -> "Playbook":
        try:
            return cls([Item.from_dict(d) for d in load_items(Path(root))])
        except ValueError as e:
            raise PlaybookError(str(e)) from None

    @classmethod
    def from_yaml(cls, text: str, version: int | None = None) -> "Playbook":
        raw = yaml.safe_load(text) or []
        if not isinstance(raw, list):
            raise PlaybookError("playbook YAML must be a list")
        return cls([Item.from_dict(d) for d in raw], version)

    @classmethod
    def load(cls, version: int | None = None, *, store=None, root: Path = ROOT) -> "Playbook":
        """`version` from the store; None = the ACTIVE playbook (latest promoted version, or the file if none)."""
        if store is not None:
            migrate(store)
            if version is None:
                version = active_version(store)
            if version is not None:
                r = store.conn.execute("select id, content from playbook_versions where id=?", (version,)).fetchone()
                if r is None:
                    raise PlaybookError(f"no playbook version {version}")
                return cls.from_yaml(r["content"], r["id"])
        elif version is not None:
            raise PlaybookError("loading a version needs the store")
        return cls.from_file(root)

    # ------------------------------------------------------------ validation
    def errors(self) -> list[str]:
        e: list[str] = []
        if len(self._items) > MAX_ITEMS:
            e.append(f"{len(self._items)} items > cap {MAX_ITEMS}")
        seen: set[str] = set()
        for it in self._items:
            if it.id in seen:
                e.append(f"duplicate id {it.id}")
            seen.add(it.id)
            e += item_errors(it)
        return e

    def check(self) -> "Playbook":
        e = self.errors()
        if e:
            raise PlaybookError("; ".join(e[:10]))
        return self

    # ------------------------------------------------------------ deltas
    def make_delta(self, op: str, item: Item, *, hypothesis: str = "", evidence: list[str] | None = None,
                   predicted_effect: str = "") -> Delta:
        """A delta for THIS playbook with its inverse filled in by code."""
        d = Delta(op=op, item=item, hypothesis=hypothesis, evidence=list(evidence or []),
                  predicted_effect=predicted_effect)
        d.inverse = self._inverse(d)
        return d

    def _inverse(self, d: Delta) -> Delta:
        if d.op == "add":
            return Delta("remove", d.item, hypothesis=f"undo: {d.hypothesis}"[:500])
        cur = self.get(d.item.id)
        if cur is None:
            raise PlaybookError(f"{d.op}: no item {d.item.id!r}")
        if d.op == "update":
            return Delta("update", cur, hypothesis=f"undo: {d.hypothesis}"[:500])
        return Delta("add", cur, hypothesis=f"undo: {d.hypothesis}"[:500], position=self.ids().index(cur.id))

    def apply(self, delta: Delta) -> "Playbook":
        """A new playbook with the delta merged. Raises PlaybookError on a sentinel item, a missing/duplicate id,
        a schema error or a broken cap. The receiving playbook is never modified."""
        if delta.op not in OPS:
            raise PlaybookError(f"unknown op {delta.op!r}")
        it = delta.item
        cur = self.get(it.id)
        items = list(self._items)
        if delta.op == "add":
            if cur is not None:
                raise PlaybookError(f"add: item {it.id!r} exists")
            if it.sentinel:
                raise PlaybookError(f"add: sentinel items are human-defined, not added by deltas ({it.id})")
            pos = len(items) if delta.position is None else max(0, min(int(delta.position), len(items)))
            items.insert(pos, it)
        else:
            if cur is None:
                raise PlaybookError(f"{delta.op}: no item {it.id!r}")
            if cur.sentinel:
                raise PlaybookError(f"{delta.op}: sentinel item {it.id!r} is immutable")
            if delta.op == "update":
                if it.sentinel:
                    raise PlaybookError(f"update: an item cannot be made sentinel ({it.id})")
                if it.kind != cur.kind:
                    raise PlaybookError(f"update: kind of {it.id!r} cannot change ({cur.kind} -> {it.kind})")
                items[items.index(cur)] = it
            else:
                items.remove(cur)
        out = Playbook(items)
        errs = out.errors()
        if errs:
            raise PlaybookError("; ".join(errs[:10]))
        return out

    # ------------------------------------------------------------ serialisation
    def to_yaml(self) -> str:
        body = yaml.dump([i.to_dict() for i in self._items], Dumper=_Dumper, sort_keys=False, allow_unicode=True,
                         width=120, default_flow_style=False)
        return _HEADER + body

    def sha(self) -> str:
        return hashlib.sha256(json.dumps([i.to_dict() for i in self._items], sort_keys=True,
                                         ensure_ascii=False, default=str).encode()).hexdigest()

    def diff(self, other: "Playbook", a: str = "champion", b: str = "challenger") -> str:
        return "\n".join(difflib.unified_diff(self.to_yaml().splitlines(), other.to_yaml().splitlines(), a, b,
                                              lineterm="", n=2))

    # ------------------------------------------------------------ store / files
    def save_version(self, store, *, status: str = "candidate", parent: int | None = None, delta: Delta | None = None,
                     proposal_id: int | None = None, note: str | None = None, commit: bool = True) -> int:
        """Store a full snapshot (+ delta + diff against `parent`) and return the version id. The snapshot is
        validated first; nothing is written to disk (activation is separate, see `activate`)."""
        if status not in STATUSES:
            raise PlaybookError(f"unknown version status {status!r}")
        self.check()
        migrate(store)
        prev = Playbook.load(parent, store=store) if parent is not None else None
        diff = prev.diff(self, f"v{parent}", "new") if prev is not None else ""
        n = store.conn.execute("select count(*) from playbook_versions").fetchone()[0] + 1
        cur = store.conn.execute(
            "insert into playbook_versions (version, content, status, parent, delta, diff, sha, proposal_id, note) "
            "values (?,?,?,?,?,?,?,?,?)",
            (f"v{n}", self.to_yaml(), status, parent, json.dumps(delta.to_dict()) if delta else None, diff,
             self.sha(), proposal_id, note))
        if commit:
            store.conn.commit()
        self.version = cur.lastrowid
        return cur.lastrowid

    def activate(self, root: Path = ROOT) -> Path:
        """Write this playbook to <root>/playbook/items.yaml (atomic replace) so the pipeline uses it."""
        self.check()
        root = Path(root).resolve()
        return _guarded_write(root / "playbook" / "items.yaml", self.to_yaml(), allowed_root=root)

    def materialise(self, directory: Path) -> Path:
        """Write this playbook into a private evaluation root `<directory>/playbook/items.yaml`; returns the root.
        `directory` must be inside the system temp dir (challengers are evaluated in a fresh workspace, R3)."""
        d = Path(directory).resolve()
        tmp = Path(tempfile.gettempdir()).resolve()
        if tmp not in d.parents:
            raise PlaybookError(f"materialise only into a temp directory, not {d}")
        (d / "playbook").mkdir(parents=True, exist_ok=True)
        _guarded_write(d / "playbook" / "items.yaml", self.to_yaml(), allowed_root=d)
        return d


_MATERIALISED: dict[str, tempfile.TemporaryDirectory] = {}


def eval_root(pb: Playbook) -> Path:
    """A cached private evaluation root for this playbook content (one temp dir per distinct sha, per process),
    so per-round shadow scoring does not create a new directory (and new mtime caches) every round."""
    sha = pb.sha()
    td = _MATERIALISED.get(sha)
    if td is None or not Path(td.name).is_dir():
        if len(_MATERIALISED) >= 16:  # bounded: oldest first
            old = next(iter(_MATERIALISED))
            _MATERIALISED.pop(old).cleanup()
        td = tempfile.TemporaryDirectory(prefix="dh-playbook-")
        _MATERIALISED[sha] = td
        pb.materialise(Path(td.name))
    return Path(td.name).resolve()


def _frozen_abs(root: Path) -> list[Path]:
    return [(root / p).resolve() for p in dhconfig.FROZEN_PATHS + dhconfig.LOCAL_FROZEN_PATHS]


def _guarded_write(path: Path, text: str, *, allowed_root: Path) -> Path:
    """THE single write path of dh/improve. Only `<allowed_root>/playbook/items.yaml`; never a frozen path
    (checked against both the allowed root and the repository root)."""
    path = Path(path).resolve()
    allowed = (Path(allowed_root).resolve() / "playbook" / "items.yaml")
    if path != allowed:
        raise PlaybookError(f"refusing to write {path}: the improvement loop may only write {allowed}")
    for base in {Path(allowed_root).resolve(), ROOT.resolve()}:
        for fp in _frozen_abs(base):
            if path == fp or fp in path.parents:
                raise PlaybookError(f"refusing to write frozen path {path}")
    tmp = path.with_name(f".items.yaml.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return path


# --------------------------------------------------------------------------------------------- versions table


_MIGRATED: "weakref.WeakSet" = weakref.WeakSet()


def migrate(store) -> None:
    """Idempotent and done once per store object, so it never commits in the middle of a caller's transaction."""
    if store in _MIGRATED:
        return
    have = {r["name"] for r in store.conn.execute("pragma table_info(playbook_versions)")}
    for col in VERSIONS_COLUMNS:
        if col.split()[0] not in have:
            store.ensure_table(f"alter table playbook_versions add column {col}")
    store.ensure_table(COUNTERS_SQL)
    _MIGRATED.add(store)


def active_version(store) -> int | None:
    """Latest version with status promoted or baseline (None: no versions yet, the file is active)."""
    migrate(store)
    r = store.conn.execute("select id from playbook_versions where status in ('promoted','baseline') "
                           "order by id desc limit 1").fetchone()
    return None if r is None else int(r["id"])


def ensure_baseline(store, root: Path = ROOT, note: str = INSTALL_NOTE) -> int:
    """The active version id; on first use the file is stored as version v1 with status 'baseline' and a note."""
    vid = active_version(store)
    if vid is not None:
        return vid
    return Playbook.from_file(root).save_version(store, status="baseline", note=note)


def set_status(store, version: int, status: str, *, commit: bool = True) -> None:
    if status not in STATUSES:
        raise PlaybookError(f"unknown version status {status!r}")
    migrate(store)
    store.conn.execute("update playbook_versions set status=?, decided_at=datetime('now') where id=?",
                       (status, version))
    if commit:
        store.conn.commit()


def version_sha(store, version: int) -> str | None:
    migrate(store)
    r = store.conn.execute("select sha from playbook_versions where id=?", (version,)).fetchone()
    return None if r is None else r["sha"]


def versions(store) -> list[dict]:
    migrate(store)
    return [dict(r) for r in store.conn.execute(
        "select id, version, status, parent, delta, sha, proposal_id, note, created_at, decided_at "
        "from playbook_versions order by id")]


def bump_counter(store, item_id: str, *, helpful: int = 0, harmful: int = 0) -> None:
    """Outcome counters live beside the versions (they are bookkeeping, not playbook content, so they never
    create a new version). Updated by the harness from verified outcomes only."""
    migrate(store)
    store.conn.execute("insert into playbook_counters (item_id, helpful, harmful) values (?,?,?) on conflict(item_id) "
                       "do update set helpful=helpful+excluded.helpful, harmful=harmful+excluded.harmful",
                       (item_id, int(helpful), int(harmful)))
    store.conn.commit()


def counters(store) -> dict[str, tuple[int, int]]:
    migrate(store)
    return {r["item_id"]: (r["helpful"], r["harmful"]) for r in store.conn.execute("select * from playbook_counters")}
