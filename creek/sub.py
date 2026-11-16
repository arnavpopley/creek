"""Sub-chats: one small conversation per point, each a fork of the main session.

`creek sub list` describes them for a panel; `creek sub send <point> <text>` runs one turn
of that point's sub-chat with `claude -p`. A sub-chat is a fork branch: its first turn forks
the main session (so it has all the context), later turns resume the fork. When a turn
settles the point, the branch merges, the main session gets one note line, and the
sub-chat is done.

This is the one place Creek starts Claude, and only because the person typed into a
sub-chat: each send is the turn they asked for, nothing extra.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

from . import actions, ledger, paths
from .fork import latest_session
from .model import id_num

MAX_THREAD = 30
TIMEOUT = 20 * 60


def _brief_nodes(st: dict) -> list[str]:
    """Points of the most recent brief, in order, plus any other point still open."""
    nodes = st["nodes"]
    briefs = [n.get("brief_id") for n in nodes.values() if n.get("origin") == "brief" and n.get("brief_id")]
    latest = briefs[-1] if briefs else None
    picked = [k for k, n in nodes.items() if latest and n.get("brief_id") == latest]
    picked.sort(key=lambda k: (nodes[k].get("order") or 0, id_num(k)))
    picked += [k for k, n in sorted(nodes.items(), key=lambda kv: id_num(kv[0])) if n["status"] in ("open", "tabled") and k not in picked]
    return picked


def _prompt_preview(cdir) -> str | None:
    """The prompt that made the most recent brief, if the ledger has it."""
    events, _, _ = ledger.read_events(cdir, 0)
    last_brief_turn = None
    preview = None
    for ev in events:
        if ev.get("type") == "turn":
            preview = ev.get("preview")
        elif ev.get("type") == "node.create" and ev.get("origin") == "brief":
            last_brief_turn = preview
    return last_brief_turn


def _open_questions(st: dict, nid: str) -> list[str]:
    """Open questions on this point, including ones asked before any point was in focus."""
    out = [k for k in st["nodes"][nid].get("questions", []) if st["questions"][k]["status"] == "open"]
    for k, q in st["questions"].items():
        if q["status"] == "open" and not q.get("node") and k not in out and actions.question_node(st, q["text"]) == nid:
            out.append(k)
    return out


def describe(cdir) -> dict:
    st = ledger.load_state(cdir)
    subs, done = [], []
    for nid in _brief_nodes(st):
        n = st["nodes"][nid]
        sc = st.get("subchats", {}).get(nid, {"thread": [], "busy": False})
        live = [r for r in n["records"] if st["records"][r]["active"]]
        q = next((st["questions"][k] for k in _open_questions(st, nid)), None)
        item = {
            "node": nid,
            "num": id_num(nid),
            "title": n["title"],
            "status": n["status"],
            "busy": sc.get("busy", False),
            "thread": sc.get("thread", [])[-MAX_THREAD:],
            "waiting": q["text"] if q else None,
            "latest": st["records"][live[-1]]["decision"] if live else None,
        }
        (done if n["status"] in ("resolved", "abandoned") and not item["busy"] else subs).append(item)
    return {
        "project": cdir.parent.name,
        "root": str(cdir.parent),
        "main_session": latest_session(st),
        "brief": _prompt_preview(cdir),
        "subs": subs,
        "done": done,
    }


def _claude() -> str | None:
    return os.environ.get("CREEK_CLAUDE") or shutil.which("claude")


def _sub_branch(tx, nid: str, main: str) -> tuple[str, str | None]:
    """The open fork branch for this point's sub-chat, opening one if needed. (branch, session)."""
    st = tx.state
    for bid in reversed(st["nodes"][nid]["branches"]):
        br = st["branches"][bid]
        if br["kind"] == "fork" and br["status"] == "open":
            return bid, br.get("session")
    bid = tx.emit({"type": "branch.open", "node": nid, "kind": "fork", "parent_session": main})["branch"]
    return bid, None


def send(cdir, node_text: str, text: str, permission_mode: str | None = None) -> dict:
    claude = _claude()
    if not claude:
        return {"ok": False, "error": "claude is not on PATH"}
    with ledger.transaction(cdir, "cli") as tx:
        st = tx.state
        nid = actions.find_node(st, node_text)
        if not nid:
            return {"ok": False, "error": f"no point {node_text}"}
        main = latest_session(st)
        if not main:
            return {"ok": False, "error": "no Claude session has used Creek here yet; send your brief in the chat first"}
        if st.get("subchats", {}).get(nid, {}).get("busy"):
            return {"ok": False, "error": "this sub-chat is still working"}
        bid, session = _sub_branch(tx, nid, main)
        # A reply to a question waiting on this point answers it. Claude records what it then
        # decides; the point only settles when Claude's own turn resolves it.
        for qid in _open_questions(st, nid)[:1]:
            tx.emit({"type": "question.answer", "question": qid, "answer": text[:300]})
        before = len(st["branches"][bid]["records"])
        tx.emit({"type": "sub.msg", "node": nid, "who": "you", "text": text})
        tx.emit({"type": "sub.busy", "node": nid, "busy": True})
        title = st["nodes"][nid]["title"]
        # Sub-chats act with the same permissions as the main chat they come from.
        permission_mode = permission_mode or st["sessions"].get(main, {}).get("mode") or "acceptEdits"

    num = id_num(nid)
    prompt = (
        f"[Creek sub-chat for point {num}: {title}]\n{text}\n\n"
        f"Work only on point {num}. When it is settled, end your reply with "
        f"`creek: resolve {num} | decision: … | rule: … | affects: …`."
    )
    argv = [claude, "-p", "--output-format", "json"]
    argv += ["--resume", session] if session else ["--resume", main, "--fork-session"]
    if permission_mode:
        argv += ["--permission-mode", permission_mode]
    argv.append(prompt)
    env = {**os.environ, "CREEK_BRANCH": bid, "CREEK_NODE": nid}
    reply, err = "", None
    try:
        proc = subprocess.run(argv, cwd=str(cdir.parent), env=env, capture_output=True, text=True, timeout=TIMEOUT, stdin=subprocess.DEVNULL)
        try:
            out = json.loads(proc.stdout)
            reply = (out.get("result") or "").strip()
            if out.get("is_error"):
                err = reply or "Claude reported an error"
        except ValueError:
            err = (proc.stderr or proc.stdout or "no output from claude").strip()[:500]
    except subprocess.TimeoutExpired:
        err = "timed out"
    except OSError as exc:
        err = str(exc)

    shown = "\n".join(line for line in reply.splitlines() if not line.strip().lower().startswith("creek:")).strip()
    with ledger.transaction(cdir, "cli") as tx:
        st = tx.state
        tx.emit({"type": "sub.msg", "node": nid, "who": "claude", "text": shown if not err else f"(couldn't run: {err})"})
        tx.emit({"type": "sub.busy", "node": nid, "busy": False})
        br = st["branches"].get(bid, {})
        settled = st["nodes"][nid]["status"] == "resolved" and br.get("status") == "open" and len(br.get("records", [])) > before
        if settled:
            # The point is settled: merge the sub-chat; the main chat hears it in one line.
            tx.emit({"type": "branch.merge", "branch": bid, "record": br["records"][-1]})
    return {"ok": err is None, "node": nid, "branch": bid, "reply": shown, "settled": bool(settled), "error": err}


def main(argv: list[str]) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="creek sub", description="Sub-chats: one small conversation per point.")
    sp = p.add_subparsers(dest="cmd", required=True)
    ls = sp.add_parser("list", help="the sub-chats, as JSON")
    ls.add_argument("--root", help="project folder (default: here)")
    sd = sp.add_parser("send", help="send a message to one point's sub-chat")
    sd.add_argument("node")
    sd.add_argument("text")
    sd.add_argument("--root", help="project folder (default: here)")
    sd.add_argument("--permission-mode", default=os.environ.get("CREEK_SUB_PERMISSION_MODE"), help="default: the main chat's mode")
    args = p.parse_args(argv)
    cdir = paths.creek_dir(paths.project_root(args.root))
    if args.cmd == "list":
        print(json.dumps(describe(cdir), ensure_ascii=False))
        return 0
    res = send(cdir, args.node, args.text, permission_mode=args.permission_mode or None)
    print(json.dumps(res, ensure_ascii=False))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
