"""UserPromptSubmit: `::` commands, brief splitting, change notes (§7.2)."""

from __future__ import annotations

from .. import actions, commands, inject, ledger, parse
from . import context_output, locate


def run(payload: dict) -> dict | None:
    cdir, session = locate(payload)
    prompt = payload.get("prompt") or ""
    cmd = parse.parse_command(prompt)
    if cmd and cmd[0] == "map":
        return _map(cdir, session)
    if cmd:
        return {"decision": "block", "reason": commands.handle(cdir, session, cmd[0], cmd[1], payload)}

    titles = parse.split_brief(prompt)
    parts: list[str] = []
    with ledger.transaction(cdir, session) as tx:
        st = tx.state
        tx.emit({"type": "turn", "preview": _preview(prompt), "mode": payload.get("permission_mode")})
        if titles:
            created = []
            brief_id = f"p{st['turn'].get(session, 0) + 1}-{st['seq']}"
            for i, title in enumerate(titles, 1):
                same = actions.find_node(st, title)
                if same and st["nodes"][same]["status"] == "open":
                    continue  # the same brief pasted again
                created.append(actions.create_node(tx, title, "brief", brief_id=brief_id, order=i))
            if created:
                span = f"{created[0]}–{created[-1]}" if len(created) > 1 else created[0]
                parts.append(f"Creek: created {span} from this brief, in order.")
        notes = st["pending_notes"].get(session) or []
        if notes:
            text, used = inject.render_notes(notes)
            if text:
                parts.append(text)
            tx.emit({"type": "notes.clear", "count": used})
        pulls = st.get("pending_pulls", {}).get(session) or []
        if pulls:
            parts.append("Creek: records you asked for (::pull) —\n" + "\n\n".join(pulls))
            tx.emit({"type": "pulls.clear", "count": len(pulls)})
        if parts:
            text = "\n\n".join(parts)
            tx.emit({"type": "inject", "hook": "user-prompt-submit", "records": [], "chars": len(text)})
    return context_output("UserPromptSubmit", text) if parts else None


def _map(cdir, session: str) -> dict:
    """::map — inside Orca, show the view in the chat; elsewhere, write it and say where."""
    from .. import mapview

    state = ledger.load_state(cdir)
    out = mapview.write(cdir, state)
    line = mapview.write_orca(cdir, state)
    if not line:
        return {"decision": "block", "reason": f"Creek: wrote {out}. Open it in a browser, or run `creek map --open`."}
    # This one needs Claude: only its reply can carry the line that shows the view.
    text = f"The user typed ::map. Creek has written its view. Reply with exactly this line and nothing else (no creek: markers):\n{line}"
    with ledger.transaction(cdir, session) as tx:
        tx.emit({"type": "turn", "preview": "::map"})
        tx.emit({"type": "inject", "hook": "user-prompt-submit", "records": [], "chars": len(text)})
    return context_output("UserPromptSubmit", text)


def _preview(prompt: str) -> str:
    """The start of a prompt (up to 4 lines, 240 chars), for the graph and sub-chats. Stays in the local ledger."""
    lines = [line.rstrip() for line in prompt.strip().splitlines() if line.strip()][:4]
    return "\n".join(lines)[:240]
