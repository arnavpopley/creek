"""PreToolUse on edits: show matching records (§7.3). Never blocks or changes permissions."""

from __future__ import annotations

from .. import guard, inject, ledger
from . import context_output, locate


def run(payload: dict) -> dict | None:
    cdir, session = locate(payload)
    state = ledger.fresh_state(cdir)
    if not state or not state["records"]:
        return None
    hits = guard.check(state, payload.get("tool_input") or {}, session)
    if not hits:
        return None
    text = inject.render_guard(hits)
    with ledger.transaction(cdir, session) as tx:
        tx.emit({"type": "inject", "hook": "pre-tool-use", "records": [rid for rid, _ in hits], "chars": len(text)})
    return context_output("PreToolUse", text)
