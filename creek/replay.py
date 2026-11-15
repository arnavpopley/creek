"""Replay a session reconstruction JSON into a fresh ledger (§16).

The reconstruction lists nodes, branches (with the message they started,
ended, and settled at), expected conflicts, and "lost details": things a
branch knew that mattered at a later message. Replay walks the messages
in order, emitting the events Creek would have logged, runs the guard on
each branch's edits and on probe edits at each lost detail's later
message, and reports what Creek caught.

Optional annotations supply what the model would have written in its
markers (rules, tags, questions), since a reconstruction only has the
one-line resolutions.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import actions, guard, inject, ledger

SESSION = "replay"


def load(path) -> dict:
    return json.loads(Path(path).read_text())


def _ts(msg: int, step: int) -> str:
    return f"2026-01-01T{msg:02d}:{step // 60 % 60:02d}:{step % 60:02d}.000Z"


class _Run:
    def __init__(self, tx, fixture: dict, markers: dict, probes: dict):
        self.tx = tx
        self.fx = fixture
        self.notes = markers
        self.probes = probes
        self.step = 0
        self.msg = 0
        self.rec_of: dict[str, str] = {}  # branch -> record
        self.q_of: dict[str, str] = {}  # branch -> question
        self.injections: list[dict] = []  # {msg, file, records, label}
        self.lost: list[dict] = []

    def emit(self, ev: dict) -> dict:
        self.step += 1
        ev.setdefault("ts", _ts(self.msg, self.step))
        return self.tx.emit(ev)

    # one guarded edit -------------------------------------------------------
    def edit(self, file: str, content: str = "", label: str = "") -> list[str]:
        st = self.tx.state
        hits = guard.check(st, {"file_path": file, "new_string": content}, SESSION)
        rids = [rid for rid, _ in hits]
        if hits:
            text = inject.render_guard(hits)
            self.emit({"type": "inject", "hook": "pre-tool-use", "records": rids, "chars": len(text)})
        node, branch = actions.focus_of(st, SESSION)
        self.emit({"type": "edit", "file": file, "tool": "Edit", "node": node, "branch": branch})
        self.injections.append({"msg": self.msg, "file": file, "records": rids, "label": label})
        return rids

    def run(self) -> None:
        fx = self.fx
        total = int((fx.get("stats") or {}).get("total_msgs") or max(
            [n.get("first_msg") or 0 for n in fx["nodes"]] + [b.get("end_msg") or 0 for b in fx["branches"]]
        ))
        branches = sorted(fx["branches"], key=lambda b: int(b["id"].lstrip("b")))
        for m in range(0, total + 1):
            self.msg = m
            if m:
                self.emit({"type": "turn"})
            for n in sorted(fx["nodes"], key=lambda n: int(n["id"])):
                if (n.get("first_msg") or 0) == m:
                    origin = "brief" if "brief" in str(n.get("origin", "")) else "model"
                    self.emit({"type": "node.create", "node": f"n{n['id']}", "title": n["title"][:80], "origin": origin, "order": int(n["id"])})
            for d in fx.get("lost_detail", []):
                if d.get("later_msg") == m:
                    self.lost.append(self.probe(d))
            for b in branches:
                if b.get("start_msg") == m:
                    self.open_branch(b)
                if b.get("end_msg") == m:
                    self.end_branch(b)
            for bid, note in self.notes.items():
                if m in note.get("asked_at", []) and bid in self.q_of:
                    self.emit({"type": "focus", "node": f"n{self._branch(bid)['node']}"})
                    actions.ask(self.tx, self._question_text(self._branch(bid)))
            for b in branches:
                if b.get("settling_msg") == m and b.get("settling_msg") is not None:
                    for pr in self._prs(b):
                        self.emit({"type": "pr", "number": pr, "action": "merged", "node": f"n{b['node']}", "branch": b["id"]})
        self.final_statuses()

    def _branch(self, bid):
        return next(b for b in self.fx["branches"] if b["id"] == bid)

    def _prs(self, b) -> list[int]:
        from .terms import parse_affects

        return parse_affects(b.get("affects") or [])["prs"]

    def _question_text(self, b) -> str:
        return self.notes.get(b["id"], {}).get("ask") or f"Unsettled ({b['trigger']}): {b['resolution']}"

    def open_branch(self, b) -> None:
        nid = f"n{b['node']}"
        self.emit({"type": "branch.open", "branch": b["id"], "node": nid, "kind": "inline", "parent_session": SESSION})
        self.emit({"type": "focus", "node": nid, "branch": b["id"]})

    def end_branch(self, b) -> None:
        nid = f"n{b['node']}"
        st = self.tx.state
        if (st["focus"].get(SESSION) or {}).get("branch") != b["id"]:
            self.emit({"type": "focus", "node": nid, "branch": b["id"]})
        from .terms import parse_affects

        for f in parse_affects(b.get("affects") or [])["files"]:
            self.edit(f, label=f"{b['id']} edit")
        note = self.notes.get(b["id"], {})
        for pr in self._prs(b):
            self.emit({"type": "pr", "number": pr, "action": "opened", "node": nid, "branch": b["id"]})
        if b.get("settling_msg") is None:
            self.q_of[b["id"]] = actions.ask(self.tx, self._question_text(b))
            return
        supersedes = [self.rec_of[x] for x in note.get("supersedes", []) if x in self.rec_of]
        rid = actions.add_record(
            self.tx,
            nid,
            b["resolution"],
            rule=note.get("rule"),
            affects=b.get("affects"),
            tags=note.get("tags"),
            supersedes=supersedes,
            branch=b["id"],
            source_msg={"turn": self.msg, "replay": b["id"]},
        )
        self.rec_of[b["id"]] = rid
        table = note.get("table")
        if not table and b["resolution"].lower().startswith("tabled"):
            table = b.get("trigger") or b["resolution"]
        if table:
            self.emit({"type": "branch.table", "branch": b["id"], "reason": table})
            self.emit({"type": "node.status", "node": nid, "status": "tabled", "reason": table})

    def probe(self, d: dict) -> dict:
        bid = d["branch"]
        p = self.probes.get(bid, {"kind": "edit" if self.rec_of.get(bid) else "question" if bid in self.q_of else "none"})
        out = {"branch": bid, "later_msg": d["later_msg"], "detail": d["detail"], "kind": p["kind"], "record": self.rec_of.get(bid), "variants": []}
        st = self.tx.state
        if p["kind"] == "question":
            qid = self.q_of.get(bid)
            q = st["questions"].get(qid) if qid else None
            out["question"] = qid
            out["recovered"] = bool(q and q["status"] == "open")
            out["how"] = f"queued as {qid}; shown to the user and the model is told not to re-ask" if q else "no question was recorded"
            return out
        if p["kind"] == "none":
            out["recovered"] = False
            out["how"] = "known miss: " + p.get("reason", "no edit to hook")
            return out
        rid = self.rec_of.get(bid)
        for v in p.get("variants", []):
            scored = guard.score(st, v["file"], v["file"] + "\n" + v.get("content", ""))
            matched = rid in scored
            seen = st["injected"].get(SESSION, {}).get(rid)
            injected = rid in self.edit(v["file"], v.get("content", ""), label=f"probe {bid} {v['label']}")
            if injected:
                verdict = "injected"
            elif matched and seen is not None:
                verdict = f"in context (shown at msg {seen}, within the {guard.COOLDOWN}-turn cooldown)"
            elif matched:
                verdict = "matched but outranked"
            else:
                verdict = "missed"
            out["variants"].append({"label": v["label"], "file": v["file"], "score": scored.get(rid, (0,))[0], "verdict": verdict})
        out["recovered"] = any(v["verdict"].startswith(("injected", "in context")) for v in out["variants"])
        first = out["variants"][0] if out["variants"] else None
        out["how"] = "; ".join(f"{v['label']}: {v['verdict']} (score {v['score']})" for v in out["variants"]) or "no probe"
        out["first_touch_recovered"] = bool(first and first["verdict"].startswith(("injected", "in context")))
        return out

    def final_statuses(self) -> None:
        st = self.tx.state
        for n in self.fx["nodes"]:
            nid = f"n{n['id']}"
            want = n.get("status")
            have = st["nodes"][nid]["status"]
            if want and want != have and have != "tabled" and want in ("open", "resolved", "tabled", "abandoned"):
                self.emit({"type": "node.status", "node": nid, "status": want})


def run(fixture: dict, cdir: Path, annotations: dict | None = None, use_markers: bool = True) -> dict:
    """Replay into `cdir` (which should hold no ledger) and return a report.

    `annotations` may hold "markers" (rules, tags, questions the model would
    have written) and "probes" (edits at each lost detail's later message).
    With use_markers=False only the probes are used: the decision-only baseline.
    """
    ann = annotations or {}
    with ledger.transaction(cdir, SESSION) as tx:
        r = _Run(tx, fixture, ann.get("markers", {}) if use_markers else {}, ann.get("probes", {}))
        r.run()
        st = tx.state
        report = {
            "nodes": len(st["nodes"]),
            "branches": len(st["branches"]),
            "records": len(st["records"]),
            "questions": {k: {"text": q["text"], "asks": q["asks"], "status": q["status"]} for k, q in st["questions"].items()},
            "conflicts_flagged": {
                k: {"records": c["records"], "overlap": [ledger.display_file(st["records"][c["records"][-1]], f) for f in c["overlap"]]}
                for k, c in st["conflicts"].items()
            },
            "record_of": r.rec_of,
            "question_of": r.q_of,
            "injections": r.injections,
            "lost_detail": r.lost,
        }
        report["conflicts_expected"] = [_expected_verdict(c, st, r) for c in fixture.get("conflicts", [])]
    report["recovered"] = sum(1 for d in report["lost_detail"] if d["recovered"])
    report["recovered_first_touch"] = sum(1 for d in report["lost_detail"] if d.get("first_touch_recovered", d["recovered"]))
    report["total_lost"] = len(report["lost_detail"])
    return report


def _expected_verdict(c: dict, st: dict, r: _Run) -> dict:
    bids = c.get("branches") or []
    rids = [r.rec_of[b] for b in bids if b in r.rec_of]
    out = {"branches": bids, "what": c.get("what", "")}
    if len(bids) < 2:
        out["verdict"] = "single branch reversing earlier work: not a two-record conflict"
        return out
    nodes = {st["records"][x]["node"] for x in rids}
    flagged = [k for k, cf in st["conflicts"].items() if set(cf["records"]) & set(rids) and len(set(cf["records"]) & set(rids)) >= 2]
    if flagged:
        out["verdict"] = f"flagged as {', '.join(flagged)}"
    elif len(nodes) == 1:
        later = bids[1:]
        shown = [
            i for i in r.injections
            if any(x in i["records"] for x in rids[:1]) and (i["label"].split(" ")[0] in later or (i["label"].startswith("probe") and i["label"].split(" ")[1] == bids[0]))
        ]
        tail = f"; the guard showed {rids[0]} ({bids[0]}) at msg {shown[0]['msg']} on {shown[0]['file']}" if shown else ""
        out["verdict"] = f"same node {next(iter(nodes))}: a refinement, not flagged by design{tail}"
    else:
        out["verdict"] = "not flagged"
    return out


def format_report(rep: dict) -> str:
    lines = [
        f"nodes {rep['nodes']} · branches {rep['branches']} · records {rep['records']} · questions {len(rep['questions'])} · conflicts flagged {len(rep['conflicts_flagged'])}",
        "",
        "Open questions:",
    ]
    for k, q in rep["questions"].items():
        if q["status"] == "open":
            lines.append(f"  {k} ×{q['asks']}  {q['text']}")
    lines.append("")
    lines.append("Conflicts flagged (cross-node overlaps):")
    for k, c in rep["conflicts_flagged"].items():
        lines.append(f"  {k}  {' vs '.join(c['records'])} on {', '.join(c['overlap'])}")
    lines.append("")
    lines.append("Expected conflicts from the reconstruction:")
    for c in rep["conflicts_expected"]:
        lines.append(f"  {'+'.join(c['branches'])}: {c['verdict']}")
    lines.append("")
    lines.append(
        f"Lost details recovered: {rep['recovered']} of {rep['total_lost']} at their later message "
        f"({rep['recovered_first_touch']} before the mistake, at the first touching edit or as a question)"
    )
    for d in rep["lost_detail"]:
        mark = "✓" if d["recovered"] else "✗"
        lines.append(f"  {mark} {d['branch']} @ msg {d['later_msg']} [{d['kind']}] {d['how']}")
    return "\n".join(lines)
