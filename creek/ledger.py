"""The event log (source of truth) and the state folded from it.

Writers append one JSON line per event with a single O_APPEND write().
Anything that reads state to decide what to write (allocating an id,
tagging an edit with the current focus) does so inside `transaction()`,
which holds an flock on .creek/lock, folds any new events, and applies
each emitted event to the in-memory state straight away. IDs are allocated
in log order, so a full rebuild always reproduces them.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import model, paths, terms
from .model import PREFIX, empty_state, new

MAX_NOTES = 20
MAX_HISTORY = 50


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _encode(event: dict) -> bytes:
    return (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def append_event(cdir: Path, event: dict, session: str | None = None) -> dict:
    """Append one event as a single write(). Returns the event as written."""
    ev = {"v": 1, "ts": event.get("ts") or now(), "session": event.get("session") or session or "cli"}
    ev.update({k: v for k, v in event.items() if k not in ("v", "ts", "session")})
    fd = os.open(paths.events_path(cdir), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, _encode(ev))
    finally:
        os.close(fd)
    return ev


# Folding -----------------------------------------------------------------------


def read_events(cdir: Path, offset: int = 0) -> tuple[list[dict], int, int]:
    """Events from byte `offset` up to the last complete line.

    Returns (events, new_offset, bad_lines). A trailing partial line (a writer
    mid-write) is left for the next fold.
    """
    p = paths.events_path(cdir)
    try:
        with open(p, "rb") as f:
            f.seek(offset)
            data = f.read()
    except FileNotFoundError:
        return [], offset, 0
    end = data.rfind(b"\n")
    if end < 0:
        return [], offset, 0
    events, bad = [], 0
    for line in data[: end + 1].splitlines():
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            bad += 1
            continue
        if isinstance(ev, dict) and ev.get("v") == 1:
            events.append(ev)
        else:
            bad += 1
    return events, offset + end + 1, bad


def fold(state: dict, events) -> dict:
    for ev in events:
        apply(state, ev)
    return state


def rebuild(cdir: Path) -> dict:
    events, offset, bad = read_events(cdir, 0)
    state = fold(empty_state(), events)
    state["offset"] = offset
    state["bad_lines"] = bad
    return state


def _read_state_file(cdir: Path) -> dict | None:
    try:
        with open(paths.state_path(cdir), "rb") as f:
            st = json.loads(f.read())
    except (FileNotFoundError, ValueError):
        return None
    if not isinstance(st, dict) or st.get("schema") != model.SCHEMA:
        return None
    return st


def _refresh(cdir: Path, state: dict | None) -> tuple[dict, bool]:
    """Bring `state` up to date with the log. Returns (state, changed)."""
    size = _log_size(cdir)
    if state is None or state.get("offset", 0) > size:
        return rebuild(cdir), True
    if state["offset"] == size:
        return state, False
    events, offset, bad = read_events(cdir, state["offset"])
    fold(state, events)
    state["offset"] = offset
    state["bad_lines"] = state.get("bad_lines", 0) + bad
    return state, True


def _log_size(cdir: Path) -> int:
    try:
        return paths.events_path(cdir).stat().st_size
    except FileNotFoundError:
        return 0


def write_state(cdir: Path, state: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=cdir, prefix=".state.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode())
        os.replace(tmp, paths.state_path(cdir))
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


@contextmanager
def locked(cdir: Path):
    fd = os.open(paths.lock_path(cdir), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def load_state(cdir: Path) -> dict:
    """Fold any new events into state.json and return the state."""
    with locked(cdir):
        state, changed = _refresh(cdir, _read_state_file(cdir))
        if changed:
            write_state(cdir, state)
        return state


def read_cached_state(cdir: Path) -> dict | None:
    """state.json as it is on disk, without folding. For the status line."""
    return _read_state_file(cdir)


def fresh_state(cdir: Path) -> dict | None:
    """Current state without taking the lock when state.json is already up to date.

    None when there is no ledger yet.
    """
    size = _log_size(cdir)
    if size == 0:
        return None
    st = _read_state_file(cdir)
    if st is not None and st.get("offset") == size:
        return st
    return load_state(cdir)


class Tx:
    """A locked view of the state. `emit` appends an event and applies it."""

    def __init__(self, cdir: Path, state: dict, session: str):
        self.cdir = cdir
        self.state = state
        self.session = session
        self.emitted: list[dict] = []

    def emit(self, event: dict) -> dict:
        ev = dict(event)
        ev.setdefault("session", self.session)
        key = _ALLOC.get(ev.get("type"))
        if key and not ev.get(key):
            if ev["type"] == "question.ask":
                # A repeat names the question it folds into; the fold matches first anyway.
                norm = ev.get("norm") or terms.normalize_question(ev.get("text", ""))
                ev[key] = _matching_question(self.state, norm) or _peek_id(self.state, key)
            else:
                ev[key] = _peek_id(self.state, key)
        ev = append_event(self.cdir, ev)
        apply(self.state, ev)
        # Keep the offset in step so the save below doesn't refold this event.
        self.state["offset"] += len(_encode(ev))
        self.emitted.append(ev)
        return ev


@contextmanager
def transaction(cdir: Path, session: str = "cli"):
    with locked(cdir):
        state, changed = _refresh(cdir, _read_state_file(cdir))
        tx = Tx(cdir, state, session)
        try:
            yield tx
        finally:
            if changed or tx.emitted:
                # If another writer appended without the lock meanwhile, our
                # offset would skip its line: refold from the true end instead.
                if _log_size(cdir) != state["offset"]:
                    state = rebuild(cdir)
                write_state(cdir, state)


# Applying events ---------------------------------------------------------------

_ALLOC = {
    "node.create": "node",
    "branch.open": "branch",
    "record.add": "record",
    "question.ask": "question",
    "conflict.flag": "conflict",
}

_COLLECTION = {"node": "nodes", "branch": "branches", "record": "records", "question": "questions", "conflict": "conflicts"}


def _peek_id(state: dict, kind: str) -> str:
    p = PREFIX[kind]
    return f"{p}{state['next'][p]}"


def _take_id(state: dict, kind: str, wanted: str | None) -> str:
    p = PREFIX[kind]
    coll = state[_COLLECTION[kind]]
    if wanted and wanted.startswith(p) and wanted not in coll and model.id_num(wanted) > 0:
        eid = wanted
    else:
        eid = f"{p}{state['next'][p]}"
        while eid in coll:
            state["next"][p] += 1
            eid = f"{p}{state['next'][p]}"
    state["next"][p] = max(state["next"][p], model.id_num(eid) + 1)
    return eid


def _matching_question(state: dict, norm: str) -> str | None:
    best, best_score = None, 0.0
    for qid, q in state["questions"].items():
        if q["status"] != "open":
            continue
        score = terms.jaccard(norm, q["norm"])
        if score >= 0.6 and score > best_score:
            best, best_score = qid, score
    return best


def _note(state: dict, session: str | None, text: str) -> None:
    if not session:
        return
    notes = state["pending_notes"].setdefault(session, [])
    if text not in notes:
        notes.append(text)
    del notes[:-MAX_NOTES]


def _uninject(state: dict, rids) -> None:
    for per in state["injected"].values():
        for rid in rids:
            per.pop(rid, None)


def display_file(record: dict, norm_path: str) -> str:
    """The original spelling of a normalised file from a record's affects."""
    base = norm_path.rsplit("/", 1)[-1]
    for entry in record.get("affects", []):
        for tok in str(entry).replace(",", " ").split():
            t = tok.strip("'\"`,;:()")
            if t.lower().endswith(base):
                return t.rsplit("/", 1)[-1]
    return base


def _index_record(state: dict, rid: str, rec: dict) -> None:
    idx = state["index"]
    for f in rec["files"]:
        for k in {f, f.rsplit("/", 1)[-1]}:
            lst = idx["file"].setdefault(k, [])
            if rid not in lst:
                lst.append(rid)
    for t in rec["terms"]:
        lst = idx["term"].setdefault(t, [])
        if rid not in lst:
            lst.append(rid)


def _deactivate(state: dict, rid: str, by: str | None) -> None:
    rec = state["records"].get(rid)
    if not rec or not rec["active"]:
        return
    rec["active"] = False
    rec["superseded_by"] = by
    _uninject(state, [rid])


def apply(state: dict, ev: dict) -> None:
    t = ev.get("type")
    s = ev.get("session")
    state["seq"] += 1
    state["last_ts"] = ev.get("ts")
    if s and not s.startswith("cli"):
        sess = state["sessions"].setdefault(s, {"first_ts": ev.get("ts"), "branch": None, "events": 0})
        sess["last_ts"] = ev.get("ts")
        sess["events"] += 1
    handler = _HANDLERS.get(t)
    if handler is None:
        return
    try:
        handler(state, ev, s)
    except (KeyError, TypeError, ValueError, AttributeError):
        # A bad event never stops the fold; it is counted and skipped.
        state["errors"] += 1


def _turn(state, s):
    return state["turn"].get(s, 0)


def _node_create(state, ev, s):
    nid = _take_id(state, "node", ev.get("node"))
    node = new(model.Node(title=str(ev.get("title", ""))[:200], origin=ev.get("origin", "user")))
    node.update(brief_id=ev.get("brief_id"), order=ev.get("order"), session=s, ts=ev.get("ts"), turn=_turn(state, s))
    state["nodes"][nid] = node


def _node_status(state, ev, s):
    node = state["nodes"][ev["node"]]
    status = ev["status"]
    if status not in model.NODE_STATUSES:
        raise ValueError(status)
    node["status"] = status
    node["reason"] = ev.get("reason")


def _node_flag(state, ev, s):
    node = state["nodes"][ev["node"]]
    if ev.get("flag") == "unrecorded":
        node["unrecorded"] = True


def _focus(state, ev, s):
    nid = ev.get("node")
    bid = ev.get("branch")
    if nid is not None and nid not in state["nodes"]:
        raise KeyError(nid)
    prev = state["focus"].get(s) or {}
    if nid:
        node = state["nodes"][nid]
        if node["focused"] and prev.get("node") != nid:
            node["revisits"] += 1
        node["focused"] = True
    if bid:
        br = state["branches"][bid]
        if br["kind"] == "fork" and not br["session"]:
            br["session"] = s
            state["sessions"].setdefault(s, {"first_ts": ev.get("ts"), "events": 0})["branch"] = bid
    state["focus"][s] = {"node": nid, "branch": bid}


def _branch_open(state, ev, s):
    nid = ev["node"]
    node = state["nodes"][nid]
    bid = _take_id(state, "branch", ev.get("branch"))
    br = new(model.Branch(node=nid, kind=ev.get("kind", "inline")))
    br.update(
        parent_session=ev.get("parent_session") or s,
        session=s if br["kind"] == "inline" else None,
        ts=ev.get("ts"),
    )
    state["branches"][bid] = br
    node["branches"].append(bid)
    # Branching on a node means it is being worked on again.
    node["status"] = "open"


def _branch_merge(state, ev, s):
    bid = ev["branch"]
    br = state["branches"][bid]
    br["status"] = "merged"
    rid = ev.get("record") or (br["records"][-1] if br["records"] else None)
    br["record"] = rid
    for sess, f in state["focus"].items():
        if f.get("branch") == bid:
            f["branch"] = None
    if br["kind"] == "fork" and rid:
        rec = state["records"][rid]
        text = f"{bid} merged into {br['node']}: {rec['decision']}"
        if rec.get("rule"):
            text += f". Rule: {rec['rule']}"
        _note(state, br["parent_session"], text)


def _branch_table(state, ev, s):
    br = state["branches"][ev["branch"]]
    br["status"] = "tabled"
    br["reason"] = ev.get("reason")
    for f in state["focus"].values():
        if f.get("branch") == ev["branch"]:
            f["branch"] = None


def _record_add(state, ev, s):
    nid = ev["node"]
    node = state["nodes"][nid]
    rid = _take_id(state, "record", ev.get("record"))
    affects = [str(a) for a in ev.get("affects") or []]
    tags = [str(a) for a in ev.get("tags") or []]
    parsed = terms.parse_affects(affects)
    rec = new(model.Record(node=nid, decision=str(ev.get("decision", ""))))
    rec.update(
        branch=ev.get("branch"),
        rule=ev.get("rule") or None,
        affects=affects,
        tags=tags,
        supersedes=[x for x in ev.get("supersedes") or [] if x != rid],
        terms=terms.extract_terms(affects, tags, ev.get("rule"), ev.get("decision")),
        files=parsed["files"],
        affects_norm=parsed["keys"],
        prs=parsed["prs"],
        question=ev.get("question"),
        source_msg=ev.get("source_msg"),
        session=s,
        ts=ev.get("ts"),
        turn=_turn(state, s),
    )
    state["records"][rid] = rec
    node["records"].append(rid)
    node["status"] = "resolved"
    node["unrecorded"] = False
    node["record_seq"] = state["seq"]
    for old in rec["supersedes"]:
        _deactivate(state, old, rid)
    bid = rec["branch"]
    if bid and bid in state["branches"]:
        br = state["branches"][bid]
        br["records"].append(rid)
        if br["kind"] == "inline" and br["status"] == "open" and br["node"] == nid:
            br["status"] = "merged"
            br["record"] = rid
            for f in state["focus"].values():
                if f.get("branch") == bid:
                    f["branch"] = None
    _index_record(state, rid, rec)


def _question_ask(state, ev, s):
    text = str(ev.get("text", "")).strip()
    norm = ev.get("norm") or terms.normalize_question(text)
    qid = _matching_question(state, norm)
    if qid:
        q = state["questions"][qid]
        q["asks"] += 1
    else:
        qid = _take_id(state, "question", ev.get("question"))
        q = new(model.Question(text=text[:300], norm=norm, node=ev.get("node")))
        q["ts"] = ev.get("ts")
        state["questions"][qid] = q
        if q["node"] in state["nodes"]:
            state["nodes"][q["node"]]["questions"].append(qid)
    q["history"].append({"ts": ev.get("ts"), "session": s, "turn": _turn(state, s)})
    del q["history"][:-MAX_HISTORY]
    if s and s not in q["told"]:
        q["told"].append(s)
        _note(state, s, f"Creek queued {qid} for the user ({q['text'][:120]}). It shows in their status line; don't repeat it in replies.")


def _question_answer(state, ev, s):
    q = state["questions"][ev["question"]]
    q["status"] = "answered"
    q["answer"] = ev.get("answer")
    q["record"] = ev.get("record")
    rec = state["records"].get(ev.get("record") or "", {})
    where = f" (recorded as {ev['record']} on {rec['node']})" if rec else ""
    _note(state, s, f"The user answered {ev['question']} ({q['text'][:120]}): {str(ev.get('answer'))[:200]}{where}")


def _conflict_flag(state, ev, s):
    cid = _take_id(state, "conflict", ev.get("conflict"))
    recs = list(ev["records"])
    state["conflicts"][cid] = new(model.Conflict(records=recs, overlap=list(ev.get("overlap") or [])))
    state["conflicts"][cid]["ts"] = ev.get("ts")
    newer, older = sorted(recs, key=model.id_num, reverse=True)[:2]
    rec_new = state["records"].get(newer, {})
    files = ", ".join(display_file(rec_new, f) for f in ev.get("overlap") or []) or "the same files"
    _note(state, rec_new.get("session") or s, f"Creek: {newer} overlaps {older} on {files} — check they agree ({cid}).")


def _conflict_clear(state, ev, s):
    c = state["conflicts"][ev["conflict"]]
    res = ev.get("resolution", "ok")
    c["status"] = "superseded" if res == "superseded" else "ok"
    if res == "superseded":
        older, newer = sorted(c["records"], key=model.id_num)[:2]
        _deactivate(state, older, newer)


def _edit(state, ev, s):
    f = ev.get("file")
    if not f:
        return
    nid, bid = ev.get("node"), ev.get("branch")
    if nid in state["nodes"]:
        e = state["nodes"][nid]["edits"]
        e[f] = e.get(f, 0) + 1
    if bid in state["branches"]:
        e = state["branches"][bid]["edits"]
        e[f] = e.get(f, 0) + 1
    state["edit_count"] = state.get("edit_count", 0) + 1


def _pr(state, ev, s):
    num = str(int(ev["number"]))
    pr = state["prs"].setdefault(num, {"action": None, "node": None, "branch": None, "score": None, "history": []})
    pr["action"] = ev.get("action") or pr["action"] or "opened"
    # A PR belongs to the node it was opened on, even if focus has moved since.
    pr["node"] = pr["node"] or ev.get("node")
    pr["branch"] = pr["branch"] or ev.get("branch")
    if ev.get("score") is not None:
        pr["score"] = ev.get("score")
        pr["reviewer"] = ev.get("reviewer")
    pr["history"].append({"ts": ev.get("ts"), "action": ev.get("action")})
    del pr["history"][:-MAX_HISTORY]
    node = state["nodes"].get(pr["node"])
    if node is not None:
        if int(num) not in node["prs"]:
            node["prs"].append(int(num))
        if ev.get("action") == "merged":
            node["pr_merged_seq"] = state["seq"]
    br = state["branches"].get(pr["branch"])
    if br is not None and int(num) not in br["prs"]:
        br["prs"].append(int(num))


def _inject(state, ev, s):
    hook = ev.get("hook", "?")
    c = state["cost"].setdefault(hook, {"chars": 0, "count": 0})
    c["chars"] += int(ev.get("chars", 0))
    c["count"] += 1
    if hook == "pre-tool-use":
        per = state["injected"].setdefault(s, {})
        for rid in ev.get("records") or []:
            per[rid] = _turn(state, s)


def _compact(state, ev, s):
    state["compacts"] += 1
    # The summary drops what was injected, so the guard may show it again.
    state["injected"].pop(s, None)


def _turn_ev(state, ev, s):
    state["turn"][s] = state["turn"].get(s, 0) + 1
    if ev.get("mode") and s in state["sessions"]:
        state["sessions"][s]["mode"] = ev["mode"]


def _note_ev(state, ev, s):
    _note(state, ev.get("to") or s, str(ev.get("text", ""))[:500])


def _notes_clear(state, ev, s):
    notes = state["pending_notes"].get(s, [])
    del notes[: int(ev.get("count", len(notes)))]


def _pull(state, ev, s):
    pulls = state.setdefault("pending_pulls", {}).setdefault(s, [])
    pulls.append(str(ev.get("text", ""))[:4000])
    del pulls[:-2]


def _pulls_clear(state, ev, s):
    pulls = state.setdefault("pending_pulls", {}).get(s, [])
    del pulls[: int(ev.get("count", len(pulls)))]


def _sub_msg(state, ev, s):
    sc = state.setdefault("subchats", {}).setdefault(ev["node"], {"thread": [], "busy": False})
    sc["thread"].append({"who": ev.get("who", "you"), "text": str(ev.get("text", ""))[:4000], "ts": ev.get("ts")})
    del sc["thread"][:-MAX_HISTORY]


def _sub_busy(state, ev, s):
    sc = state.setdefault("subchats", {}).setdefault(ev["node"], {"thread": [], "busy": False})
    sc["busy"] = bool(ev.get("busy"))


def _sub_route(state, ev, s):
    # The sub-chat this session's current turn was addressed to ("3: teal"), until Stop files the reply.
    routes = state.setdefault("sub_route", {})
    if ev.get("node"):
        routes[s] = ev["node"]
    else:
        routes.pop(s, None)


def _view_shown(state, ev, s):
    state.setdefault("view_open", {})[s] = list(ev.get("open") or [])


def _error(state, ev, s):
    state["errors"] += 1


_HANDLERS = {
    "node.create": _node_create,
    "node.status": _node_status,
    "node.flag": _node_flag,
    "focus": _focus,
    "branch.open": _branch_open,
    "branch.merge": _branch_merge,
    "branch.table": _branch_table,
    "record.add": _record_add,
    "question.ask": _question_ask,
    "question.answer": _question_answer,
    "edit": _edit,
    "pr": _pr,
    "conflict.flag": _conflict_flag,
    "conflict.clear": _conflict_clear,
    "inject": _inject,
    "compact": _compact,
    "turn": _turn_ev,
    "note": _note_ev,
    "notes.clear": _notes_clear,
    "pull": _pull,
    "sub.msg": _sub_msg,
    "sub.busy": _sub_busy,
    "sub.route": _sub_route,
    "view.shown": _view_shown,
    "pulls.clear": _pulls_clear,
    "error": _error,
}


def log_error(cdir: Path | None, where: str, exc: BaseException | str) -> None:
    """Append to .creek/errors.log. Never raises."""
    try:
        import traceback

        if cdir is None:
            cdir = paths.creek_dir()
        if isinstance(exc, BaseException):
            detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        else:
            detail = str(exc)
        with open(paths.errors_path(cdir), "a") as f:
            f.write(f"{now()} {where}: {detail.rstrip()}\n")
    except Exception:
        pass
