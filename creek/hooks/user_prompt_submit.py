"""UserPromptSubmit: `::` commands, brief splitting, change notes (§7.2)."""

from __future__ import annotations

import re

from .. import actions, commands, inject, ledger, parse
from . import context_output, locate

# "3: make it smaller" goes to point 3's sub-chat.
_ROUTE = re.compile(r"^\s*@?(\d{1,3})\s*:\s*(\S.*)$", re.S)


def run(payload: dict) -> dict | None:
    cdir, session = locate(payload)
    prompt = payload.get("prompt") or ""
    cmd = parse.parse_command(prompt)
    if cmd and cmd[0] == "map":
        return _map(cdir, session)
    if cmd:
        return {"decision": "block", "reason": commands.handle(cdir, session, cmd[0], cmd[1], payload)}

    titles = parse.split_brief(prompt)
    route = None if titles else _ROUTE.match(prompt)
    parts: list[str] = []
    with ledger.transaction(cdir, session) as tx:
        st = tx.state
        tx.emit({"type": "turn", "preview": _preview(prompt), "mode": payload.get("permission_mode")})
        focus = None
        if route and f"n{int(route.group(1))}" in st["nodes"]:
            focus = _route_to(tx, f"n{int(route.group(1))}", route.group(2).strip(), parts)
        created = []
        if titles:
            brief_id = f"p{st['turn'].get(session, 0) + 1}-{st['seq']}"
            for i, title in enumerate(titles, 1):
                same = actions.find_node(st, title)
                if same and st["nodes"][same]["status"] == "open":
                    continue  # the same brief pasted again
                created.append(actions.create_node(tx, title, "brief", brief_id=brief_id, order=i))
            if created:
                span = f"{created[0]}–{created[-1]}" if len(created) > 1 else created[0]
                parts.append(f"Creek: created {span} from this brief, in order.")
                tx.emit({"type": "sub.route", "node": "brief"})
        from .. import panes

        if created and panes.host():
            # Each point gets its own pane and its own Claude; this chat stays the overview.
            panes.launch_background(tx.cdir, session, created, _preview(prompt) if len(prompt) > 1200 else prompt.strip())
            tx.emit({"type": "sub.route", "node": None})
            parts[:] = [p for p in parts if not p.startswith("Creek: created")]
            parts.append(
                f"Creek: opened a pane for each point ({created[0]}–{created[-1]}); a separate Claude in each pane works on "
                f"its point, and the user types to it there. Reply here in one short line acknowledging that. Don't work "
                f"on the points in this chat."
            )
            created = []
        if created or focus:
            line = _show_view(tx, session, prompt if created else None, focus, fresh=bool(created))
            if line:
                parts.append(f"Your reply must start with this exact line, before anything else (it draws the user's sub-chats):\n{line}")
        if created and not any(p.startswith("Creek: opened a pane") for p in parts):
            parts.append(
                ("After that line, answer" if parts[-1].startswith("Your reply must start") else "Answer")
                + " point by point under headings like **1 · title**, so each answer lands in its point's sub-chat."
            )
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


def _route_to(tx, nid: str, text: str, parts: list[str]) -> str:
    """Address this turn to one point's sub-chat: focus it (reopening it if settled) and say so."""
    st = tx.state
    actions.start_on(tx, nid)
    from ..sub import _open_questions

    for qid in _open_questions(st, nid)[:1]:
        tx.emit({"type": "question.answer", "question": qid, "answer": text[:300]})
    tx.emit({"type": "sub.msg", "node": nid, "who": "you", "text": text})
    tx.emit({"type": "sub.route", "node": nid})
    num = nid[1:]
    parts.append(
        f"Creek: this message is for sub-chat {num} ({st['nodes'][nid]['title'][:80]}). Work only on point {num} and "
        f"leave the others alone. When it's settled, end with `creek: resolve {num} | decision: …`."
    )
    return nid


def _show_view(tx, session: str, brief: str | None, focus: str | None, fresh: bool) -> str | None:
    """Write the in-chat sub-chat view (Orca only) and return the line that shows it."""
    from .. import sub, subview

    st = tx.state
    nodes = sub._brief_nodes(st)
    if not nodes:
        return None
    preview = _preview(brief) if brief else sub._prompt_preview(tx.cdir)
    data, still_open = subview.build(st, session, preview, nodes, focus=focus, fresh=fresh)
    line = subview.write(data)
    if line:
        tx.emit({"type": "view.shown", "open": still_open})
    return line
