"""Entities derived from the event log.

State is kept as plain dicts so state.json loads and dumps without a
conversion step on every hook. These dataclasses define each entity's
fields and defaults; `ledger.fold` builds entries with `new(...)`.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

SCHEMA = 1

NODE_STATUSES = ("open", "resolved", "tabled", "abandoned")
PR_ACTIONS = ("opened", "merged", "draft", "closed")

PREFIX = {"node": "n", "branch": "b", "record": "r", "question": "q", "conflict": "c"}


@dataclass
class Node:
    title: str
    origin: str = "user"  # brief | model | user
    status: str = "open"
    brief_id: str | None = None
    order: int | None = None
    reason: str | None = None
    branches: list = field(default_factory=list)
    records: list = field(default_factory=list)
    questions: list = field(default_factory=list)
    revisits: int = 0
    focused: bool = False
    unrecorded: bool = False
    edits: dict = field(default_factory=dict)  # file -> count
    prs: list = field(default_factory=list)
    session: str | None = None
    ts: str | None = None
    turn: int = 0


@dataclass
class Branch:
    node: str
    kind: str = "inline"  # inline | fork
    status: str = "open"  # open | merged | tabled
    session: str | None = None
    parent_session: str | None = None
    record: str | None = None
    records: list = field(default_factory=list)
    reason: str | None = None
    edits: dict = field(default_factory=dict)
    prs: list = field(default_factory=list)
    ts: str | None = None


@dataclass
class Record:
    node: str
    decision: str
    branch: str | None = None
    rule: str | None = None
    affects: list = field(default_factory=list)
    tags: list = field(default_factory=list)
    supersedes: list = field(default_factory=list)
    superseded_by: str | None = None
    active: bool = True
    terms: list = field(default_factory=list)
    files: list = field(default_factory=list)  # normalised file paths from affects
    affects_norm: list = field(default_factory=list)  # "file" or "file#ident"
    prs: list = field(default_factory=list)
    question: str | None = None
    source_msg: dict | None = None
    session: str | None = None
    ts: str | None = None
    turn: int = 0


@dataclass
class Question:
    text: str
    norm: str
    node: str | None = None
    asks: int = 1
    status: str = "open"  # open | answered
    answer: str | None = None
    record: str | None = None
    told: list = field(default_factory=list)  # sessions told it is queued
    history: list = field(default_factory=list)  # [{ts, session, turn}]
    ts: str | None = None


@dataclass
class Conflict:
    records: list
    overlap: list
    status: str = "open"  # open | ok | superseded
    ts: str | None = None


def new(entity) -> dict:
    return asdict(entity)


def empty_state() -> dict:
    return {
        "schema": SCHEMA,
        "offset": 0,
        "seq": 0,
        "next": {p: 1 for p in PREFIX.values()},
        "nodes": {},
        "branches": {},
        "records": {},
        "questions": {},
        "conflicts": {},
        "prs": {},
        "focus": {},
        "index": {"file": {}, "term": {}},
        "injected": {},
        "turn": {},
        "pending_notes": {},
        "pending_pulls": {},
        "sessions": {},
        "cost": {},
        "errors": 0,
        "compacts": 0,
        "last_ts": None,
    }


_REF = re.compile(r"^\s*([nbrqc])?(\d+)\s*$", re.I)


def ref(text: str | None, kind: str = "node") -> str | None:
    """'13', 'n13' or 'N13' -> 'n13' (for kind='node'). None if not an id."""
    if text is None:
        return None
    m = _REF.match(str(text))
    if not m:
        return None
    want = PREFIX[kind]
    got = (m.group(1) or want).lower()
    if got != want:
        return None
    return f"{want}{int(m.group(2))}"


def id_num(eid: str) -> int:
    try:
        return int(eid[1:])
    except (ValueError, IndexError):
        return 0
