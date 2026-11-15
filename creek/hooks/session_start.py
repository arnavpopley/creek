"""SessionStart: inject the protocol and the state block (§7.1)."""

from __future__ import annotations

from .. import inject, ledger
from . import context_output, env_branch, locate


def run(payload: dict) -> dict | None:
    cdir, session = locate(payload)
    source = payload.get("source") or "startup"
    bid, _ = env_branch()
    with ledger.transaction(cdir, session) as tx:
        st = tx.state
        br = st["branches"].get(bid) if bid else None
        if br and br["kind"] == "fork" and br["status"] == "open" and br["session"] in (None, session):
            if (st["focus"].get(session) or {}).get("branch") != bid:
                tx.emit({"type": "focus", "node": br["node"], "branch": bid})
        else:
            bid = None
        block, shown = inject.render_state(st, session, source=source, branch=bid)
        text = inject.PROTOCOL + ("\n\n" + block if block else "")
        tx.emit({
            "type": "inject",
            "hook": "session-start",
            "records": shown,
            "chars": len(text),
            "source": source,
            "transcript": payload.get("transcript_path"),
        })
    return context_output("SessionStart", text)
