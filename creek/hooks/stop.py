"""Stop: read `creek:` markers from the final reply (§7.5). Never blocks."""

from __future__ import annotations

import re

from .. import actions, ledger, parse, transcript
from . import env_branch, locate


def run(payload: dict) -> None:
    cdir, session = locate(payload)
    text = payload.get("last_assistant_message")
    if text is None:
        text = transcript.last_assistant_text(payload.get("transcript_path")) or ""
    markers, errors = parse.parse_markers(text)
    if not markers and not errors:
        cached = ledger.read_cached_state(cdir)
        if cached is not None and not _maybe_unrecorded(cached) and session not in (cached.get("sub_route") or {}):
            return None
    bid, _ = env_branch()
    source = {"transcript": payload.get("transcript_path"), "offset": transcript.size(payload.get("transcript_path"))}
    with ledger.transaction(cdir, session) as tx:
        source["turn"] = tx.state["turn"].get(session, 0)
        for err in errors:
            tx.emit({"type": "error", "where": "marker", "msg": err})
        actions.apply_markers(tx, markers, source_msg=source, env_branch=bid)
        actions.flag_unrecorded(tx)
        _file_reply(tx, session, text)
    return None


_SECTION = re.compile(r"^\s*(?:#{1,4}\s*|\*\*\s*)?(\d{1,2})\s*(?:[·.):\-–—])\s*", re.M)


def _file_reply(tx, session: str, text: str) -> None:
    """Put Claude's reply into the sub-chat this turn was for, or split a brief reply by point."""
    route = (tx.state.get("sub_route") or {}).get(session)
    if not route:
        return
    body = "\n".join(line for line in (text or "").splitlines() if not line.strip().lower().startswith(("creek:", "::orca-visual"))).strip()
    if route == "brief":
        for nid, section in _sections(tx.state, body):
            tx.emit({"type": "sub.msg", "node": nid, "who": "claude", "text": section[:1500]})
    elif route in tx.state["nodes"] and body:
        tx.emit({"type": "sub.msg", "node": route, "who": "claude", "text": body[:1500]})
    tx.emit({"type": "sub.route", "node": None})


def _sections(state: dict, body: str) -> list[tuple[str, str]]:
    """'**1 · Header** …' blocks of a reply, matched to points that exist."""
    marks = [(m.start(), int(m.group(1))) for m in _SECTION.finditer(body)]
    out = []
    for i, (start, num) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(body)
        nid = f"n{num}"
        chunk = body[start:end].strip()
        # Drop the heading line itself; keep what was said about the point.
        lines = chunk.splitlines()
        rest = "\n".join(lines[1:]).strip() or re.sub(r"^\W*\d+\W*", "", lines[0]).strip("* ")
        if nid in state["nodes"] and rest:
            out.append((nid, rest))
    return out


def _maybe_unrecorded(state: dict) -> bool:
    return any(
        n.get("pr_merged_seq") and n["pr_merged_seq"] > n.get("record_seq", 0) and not n.get("unrecorded")
        for n in state["nodes"].values()
    )
