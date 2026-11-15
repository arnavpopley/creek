"""Stop: read `creek:` markers from the final reply (§7.5). Never blocks."""

from __future__ import annotations

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
        if cached is not None and not _maybe_unrecorded(cached):
            return None
    bid, _ = env_branch()
    source = {"transcript": payload.get("transcript_path"), "offset": transcript.size(payload.get("transcript_path"))}
    with ledger.transaction(cdir, session) as tx:
        source["turn"] = tx.state["turn"].get(session, 0)
        for err in errors:
            tx.emit({"type": "error", "where": "marker", "msg": err})
        actions.apply_markers(tx, markers, source_msg=source, env_branch=bid)
        actions.flag_unrecorded(tx)
    return None


def _maybe_unrecorded(state: dict) -> bool:
    return any(
        n.get("pr_merged_seq") and n["pr_merged_seq"] > n.get("record_seq", 0) and not n.get("unrecorded")
        for n in state["nodes"].values()
    )
