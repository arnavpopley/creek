"""`::` commands typed in chat (§6.1). Each returns the confirmation text."""

from __future__ import annotations

import os

from . import actions, ledger, transcript
from .model import ref

HELP = """Creek commands (never sent to Claude, except ::map inside Orca):
::status                 open nodes, questions, conflicts
::map                    show the board (inline in Orca; otherwise writes .creek/map.html)
::answer q1 <answer>     answer a queued question
::branch 13              open an inline branch on n13
::merge                  close the current branch with its latest record
::table 9 <reason>       table a node and its branch
::drop 9 <reason>        abandon a node
::node <title>           add a node
::ok c1 | ::supersede c1 keep both records, or retire the older one
::pull r7                show Claude the full record and its source next turn"""

PULL_EXCERPT = 1500


def handle(cdir, session: str, name: str, args: str, payload: dict | None = None) -> str:
    fn = _COMMANDS.get(name)
    if fn is None:
        return f"Creek: unknown command ::{name}.\n{HELP}"
    with ledger.transaction(cdir, session) as tx:
        return fn(tx, args, payload or {})


def _status(tx, args, payload):
    from .cli import render_status

    return render_status(tx.state, tx.session)


def _help(tx, args, payload):
    return HELP


def _node_arg(tx, args) -> tuple[str | None, str]:
    first, _, rest = args.strip().partition(" ")
    nid = actions.find_node(tx.state, first)
    return nid, rest.strip()


def _answer(tx, args, payload):
    first, _, answer = args.strip().partition(" ")
    qid = ref(first, "question")
    st = tx.state
    if not qid or qid not in st["questions"]:
        return f"Creek: no question {first or '(none given)'}. Usage: ::answer q1 <answer>"
    answer = answer.strip()
    if not answer:
        return f"Creek: what's the answer? Usage: ::answer {qid} <answer>"
    q = st["questions"][qid]
    if q["status"] != "open":
        return f"Creek: {qid} was already answered ({q.get('answer')})."
    nid = q.get("node") if q.get("node") in st["nodes"] else None
    if nid is None:
        nid = actions.create_node(tx, q["text"][:80], "user")
    rid = actions.add_record(tx, nid, f"{answer} (your answer to: {q['text']})", question=qid)
    tx.emit({"type": "question.answer", "question": qid, "answer": answer, "record": rid})
    return f"Creek: {qid} answered ({answer}); recorded as {rid} on {nid}. Claude sees it with your next message."


def _branch(tx, args, payload):
    nid, _ = _node_arg(tx, args)
    if not nid:
        return "Creek: which node? Usage: ::branch 13"
    bid = tx.emit({"type": "branch.open", "node": nid, "kind": "inline", "parent_session": tx.session})["branch"]
    tx.emit({"type": "focus", "node": nid, "branch": bid})
    return f"Creek: opened {bid} on {nid} ({tx.state['nodes'][nid]['title']}). Edits are tagged to it until the next resolve on {nid} or ::merge."


def _current_branch(tx) -> str | None:
    env_b = os.environ.get("CREEK_BRANCH")
    st = tx.state
    if env_b in st["branches"] and st["branches"][env_b]["status"] == "open":
        return env_b
    _, bid = actions.focus_of(st, tx.session)
    if bid in st["branches"] and st["branches"][bid]["status"] == "open":
        return bid
    return None


def _merge(tx, args, payload):
    st = tx.state
    bid = ref(args.split()[0], "branch") if args.strip() else _current_branch(tx)
    if not bid or bid not in st["branches"]:
        return "Creek: no open branch here. Open one with ::branch <node> or creek fork <node>."
    br = st["branches"][bid]
    if br["status"] != "open":
        return f"Creek: {bid} is already {br['status']}."
    if not br["records"]:
        return (
            f"Creek: {bid} has no record yet. Ask Claude to settle it "
            f"(creek: resolve {br['node'][1:]} | decision: …), or ::table {br['node'][1:]} <reason>."
        )
    rid = br["records"][-1]
    tx.emit({"type": "branch.merge", "branch": bid, "record": rid})
    rec = st["records"][rid]
    msg = f"Creek: merged {bid} into {br['node']} with {rid}: {rec['decision'][:160]}"
    if br["kind"] == "fork":
        msg += "\nThis fork is done; the main session gets a one-line note on its next message. You can close this session now."
    return msg


def _table(tx, args, payload, status="tabled"):
    nid, reason = _node_arg(tx, args)
    if not nid:
        return f"Creek: which node? Usage: ::{'table' if status == 'tabled' else 'drop'} 9 <reason>"
    st = tx.state
    for bid in st["nodes"][nid]["branches"]:
        if st["branches"][bid]["status"] == "open":
            tx.emit({"type": "branch.table", "branch": bid, "reason": reason or status})
    tx.emit({"type": "node.status", "node": nid, "status": status, "reason": reason or None})
    title = st["nodes"][nid]["title"]
    return f"Creek: {nid} ({title}) {'tabled' if status == 'tabled' else 'dropped'}" + (f": {reason}" if reason else ".")


def _drop(tx, args, payload):
    return _table(tx, args, payload, status="abandoned")


def _node(tx, args, payload):
    title = args.strip()
    if not title:
        return "Creek: usage: ::node <title>"
    nid = actions.create_node(tx, title[:200], "user")
    return f"Creek: added {nid}: {title[:200]}"


def _conflict(tx, args, payload, resolution):
    cid = ref(args.split()[0], "conflict") if args.strip() else None
    st = tx.state
    if not cid or cid not in st["conflicts"]:
        return f"Creek: which conflict? Usage: ::{'ok' if resolution == 'ok' else 'supersede'} c1"
    c = st["conflicts"][cid]
    if c["status"] != "open":
        return f"Creek: {cid} is already cleared ({c['status']})."
    tx.emit({"type": "conflict.clear", "conflict": cid, "resolution": resolution})
    a, b = sorted(c["records"], key=lambda r: int(r[1:]))
    if resolution == "ok":
        return f"Creek: {cid} cleared; {a} and {b} both stand."
    return f"Creek: {cid} cleared; {a} is superseded by {b} and won't be shown again."


def _ok(tx, args, payload):
    return _conflict(tx, args, payload, "ok")


def _supersede(tx, args, payload):
    return _conflict(tx, args, payload, "superseded")


def _pull(tx, args, payload):
    rid = ref(args.split()[0], "record") if args.strip() else None
    st = tx.state
    if not rid or rid not in st["records"]:
        return "Creek: which record? Usage: ::pull r7"
    rec = st["records"][rid]
    lines = [f"{rid} on {rec['node']} ({st['nodes'].get(rec['node'], {}).get('title', '')}), msg {rec.get('turn', 0)}" + ("" if rec["active"] else f", superseded by {rec.get('superseded_by')}")]
    lines.append(f"decision: {rec['decision']}")
    for key in ("rule", "affects", "tags", "supersedes"):
        v = rec.get(key)
        if v:
            lines.append(f"{key}: {', '.join(v) if isinstance(v, list) else v}")
    excerpt = source_excerpt(rec)
    if excerpt:
        lines.append(f"source (msg {rec.get('turn', 0)}):\n{excerpt}")
    text = "\n".join(lines)
    tx.emit({"type": "pull", "record": rid, "text": text})
    found = "with its source excerpt" if excerpt else "(source message not found in the transcript)"
    return f"Creek: {rid} {found} will be shown to Claude with your next message."


def source_excerpt(rec: dict) -> str | None:
    src = rec.get("source_msg") or {}
    needle = rec["decision"][:60]
    reply = transcript.find_reply(src.get("transcript"), src.get("offset"), needle)
    if not reply:
        return None
    body = "\n".join(line for line in reply.splitlines() if "creek:" not in line.lower()).strip()
    if len(body) > PULL_EXCERPT:
        body = "…" + body[-(PULL_EXCERPT - 1):]
    return body


_COMMANDS = {
    "status": _status,
    "help": _help,
    "answer": _answer,
    "branch": _branch,
    "merge": _merge,
    "table": _table,
    "drop": _drop,
    "node": _node,
    "ok": _ok,
    "supersede": _supersede,
    "pull": _pull,
}
