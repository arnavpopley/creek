"""Producer operations shared by hooks, `::` commands, and replay.

Each takes an open `ledger.Tx`, so reads and the events they lead to happen
under one lock.
"""

from __future__ import annotations

import re

from . import terms
from .model import id_num, ref


def find_node(state: dict, text: str | None) -> str | None:
    """'13', 'n13', or an exact (case-insensitive) title -> node id."""
    if not text:
        return None
    nid = ref(text, "node")
    if nid:
        return nid if nid in state["nodes"] else None
    want = text.strip().lower()
    for k, n in state["nodes"].items():
        if n["title"].strip().lower() == want:
            return k
    return None


def _title_words(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    # Crude singular so "shortcut" finds "shortcuts".
    return {w[:-1] if len(w) > 3 and w.endswith("s") else w for w in words if len(w) >= 3 and w not in terms.STOP}


def match_node(state: dict, text: str | None, threshold: float = 0.6) -> str | None:
    """An exact id or title, else the node whose title contains most of `text`'s words.

    The model often names a node by a short paraphrase ("h shortcut" for
    "Add keyboard shortcuts: …"); matching it keeps the work on one node
    instead of starting a duplicate.
    """
    nid = find_node(state, text)
    if nid or not text or ref(text, "node"):
        return nid
    want = _title_words(text)
    if not want:
        return None
    best, best_key = None, (0.0, 0)
    for k, n in state["nodes"].items():
        if n["status"] == "abandoned":
            continue
        hit = len(want & _title_words(n["title"])) / len(want)
        if hit >= threshold and (hit, id_num(k)) > best_key:
            best, best_key = k, (hit, id_num(k))
    return best


def focus_of(state: dict, session: str) -> tuple[str | None, str | None]:
    f = state["focus"].get(session) or {}
    return f.get("node"), f.get("branch")


def open_branch(state: dict, session: str, node: str) -> str | None:
    """The open branch on `node` this session is working in, if any."""
    fnode, fbranch = focus_of(state, session)
    if fbranch and fnode == node and state["branches"].get(fbranch, {}).get("status") == "open":
        return fbranch
    return None


def create_node(tx, title: str, origin: str, **extra) -> str:
    return tx.emit({"type": "node.create", "title": title, "origin": origin, **extra})["node"]


def start_on(tx, node_text: str) -> str | None:
    """`creek: on <node>`: focus it, opening an inline branch if it was already settled."""
    st = tx.state
    nid = match_node(st, node_text)
    if nid is None:
        if ref(node_text, "node"):
            tx.emit({"type": "error", "where": "marker", "msg": f"creek: on {node_text}: no such node"})
            return None
        nid = create_node(tx, node_text.strip()[:200], "model")
    fnode, fbranch = focus_of(st, tx.session)
    if fnode == nid and (fbranch is None or st["branches"][fbranch]["status"] == "open"):
        return nid
    if st["nodes"][nid]["records"] and not open_branch(st, tx.session, nid):
        bid = tx.emit({"type": "branch.open", "node": nid, "kind": "inline", "parent_session": tx.session})["branch"]
        tx.emit({"type": "focus", "node": nid, "branch": bid})
    else:
        tx.emit({"type": "focus", "node": nid})
    return nid


def add_record(tx, node: str, decision: str, rule=None, affects=None, tags=None, supersedes=None, branch=None, **extra) -> str:
    from . import conflicts

    ev = tx.emit({
        "type": "record.add",
        "node": node,
        "branch": branch,
        "decision": decision,
        "rule": rule,
        "affects": list(affects or []),
        "tags": list(tags or []),
        "supersedes": list(supersedes or []),
        **extra,
    })
    conflicts.check(tx, ev["record"])
    return ev["record"]


def resolve(tx, marker: dict, source_msg: dict | None = None, env_branch: str | None = None) -> str:
    st = tx.state
    nid = match_node(st, marker.get("node"))
    if nid is None and marker.get("node") and not ref(marker["node"], "node"):
        # A title we haven't seen: treat it as a new point.
        nid = create_node(tx, marker["node"][:200], "model")
    fnode, fbranch = focus_of(st, tx.session)
    if nid is None:
        nid = fnode
    if nid is None:
        nid = create_node(tx, marker["decision"][:60], "model")
    branch = None
    if env_branch and st["branches"].get(env_branch, {}).get("node") == nid:
        branch = env_branch
    elif fbranch and fnode == nid and st["branches"].get(fbranch, {}).get("status") == "open":
        branch = fbranch
    return add_record(
        tx,
        nid,
        marker["decision"],
        rule=marker.get("rule"),
        affects=marker.get("affects"),
        tags=marker.get("tags"),
        supersedes=[r for r in marker.get("supersedes") or [] if r in st["records"]],
        branch=branch,
        source_msg=source_msg,
    )


def ask(tx, text: str) -> str:
    fnode, _ = focus_of(tx.state, tx.session)
    return tx.emit({"type": "question.ask", "text": text, "norm": terms.normalize_question(text), "node": fnode})["question"]


def apply_markers(tx, markers: list[dict], source_msg: dict | None = None, env_branch: str | None = None) -> list[str]:
    """Apply parsed markers in order. Returns the ids they created or touched."""
    out = []
    for m in markers:
        kind = m["kind"]
        if kind == "on":
            nid = start_on(tx, m["node"])
        elif kind == "node":
            # A "new" point whose words all appear in an existing node is more work on that node.
            existing = match_node(tx.state, m["title"], threshold=1.0)
            nid = start_on(tx, existing) if existing else create_node(tx, m["title"], "model")
        elif kind == "resolve":
            nid = resolve(tx, m, source_msg=source_msg, env_branch=env_branch)
        elif kind == "ask":
            nid = ask(tx, m["text"])
        else:
            nid = None
        if nid:
            out.append(nid)
    return out


def flag_unrecorded(tx) -> list[str]:
    """Nodes with a merged PR and no record since: mark them for the status line."""
    flagged = []
    for nid, n in tx.state["nodes"].items():
        merged = n.get("pr_merged_seq")
        if merged and merged > n.get("record_seq", 0) and not n.get("unrecorded"):
            tx.emit({"type": "node.flag", "node": nid, "flag": "unrecorded"})
            flagged.append(nid)
    return flagged
