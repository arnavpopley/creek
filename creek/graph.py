"""Lay out the session as a git-style graph for the board.

Main is the row of your prompts. Each point gets its own row: it forks off
main where it starts, gains a commit for each decision (or an open circle
for a question waiting on you), and merges back into main once it is
settled. Reopening a point forks it again. Arcs link a decision Creek
reminded Claude of to the decision that followed, and two decisions that
conflict.

Columns are positions in time, in log order. The layout is computed here
so the page only draws it.
"""

from __future__ import annotations

from . import ledger, model

MAX_ROWS = 40


def build(events: list[dict], previews: list[str] | None = None) -> dict:
    st = model.empty_state()
    g = _Graph(st, previews or [])
    for ev in events:
        g.before(ev)
        ledger.apply(st, ev)
        g.after(ev)
    g.flush_merges()
    g.finish()
    return g.out()


class _Graph:
    def __init__(self, st: dict, previews: list[str]):
        self.st = st
        self.previews = previews
        self.col = 0
        self.turns = 0
        self.rows: dict[str, int] = {}  # node -> row (main is 0)
        self.seg: dict[str, dict] = {}  # node -> open segment
        self.segments: list[dict] = []
        self.dots: list[dict] = []
        self.arcs: list[dict] = []
        self.pos: dict[str, tuple[int, int]] = {}  # record -> (col, row)
        self.qdot: dict[str, dict] = {}
        self.pending_merge: list[str] = []
        self.reminders: list[str] = []
        self.last_main = 0
        self.born: dict[str, int] = {}  # node -> main column it was created at
        self.brief_waiting: list[str] = []

    # helpers -----------------------------------------------------------------
    def _row(self, nid: str) -> int:
        if nid not in self.rows:
            self.rows[nid] = len(self.rows) + 1
        return self.rows[nid]

    def _open(self, nid: str) -> dict:
        s = self.seg.get(nid)
        if s is None:
            # A point's first branch starts at the prompt that created it; a reopened one starts now.
            start = self.born.pop(nid, self.last_main)
            s = {"node": nid, "row": self._row(nid), "from": start, "to": None, "end": None, "merge": None}
            self.seg[nid] = s
            self.segments.append(s)
        return s

    def _tick(self) -> int:
        self.col += 1
        return self.col

    def flush_merges(self, keep: str | None = None) -> None:
        for nid in list(self.pending_merge):
            if nid == keep:
                continue
            self.pending_merge.remove(nid)
            s = self.seg.get(nid)
            if s is None or self.st["nodes"].get(nid, {}).get("status") != "resolved":
                continue
            c = self._tick()
            s["to"], s["merge"] = s["to"] or c, c
            self.dots.append({"col": c, "row": 0, "kind": "merge", "node": nid, "label": f"merged {nid}"})
            self.last_main = c
            del self.seg[nid]

    # events -------------------------------------------------------------------
    def before(self, ev: dict) -> None:
        t = ev.get("type")
        # Don't merge a point just before more happens to it (another decision, or being parked).
        same_node = ev.get("node") if t in ("record.add", "node.status") else None
        if t in ("turn", "record.add", "question.ask", "node.status", "branch.open", "node.create"):
            self.flush_merges(keep=same_node)
        if t == "question.ask":
            self._was_open = {k for k, q in self.st["questions"].items() if q["status"] == "open"}

    def after(self, ev: dict) -> None:
        t = ev.get("type")
        st = self.st
        if t not in ("node.create", "turn"):
            self.brief_waiting = []
        if t == "turn":
            self.turns += 1
            c = self._tick()
            text = ev.get("preview") or (self.previews[self.turns - 1] if self.turns <= len(self.previews) else "")
            self.dots.append({"col": c, "row": 0, "kind": "prompt", "label": f"Prompt {self.turns}", "text": text})
            self.last_main = c
            for nid in self.brief_waiting:
                if nid in self.born:
                    self.born[nid] = c
            self.brief_waiting = []
        elif t == "node.create":
            nid = ev.get("node") or ""
            self._row(nid)
            self.born[nid] = self.last_main
            if ev.get("origin") == "brief":
                # Older ledgers log a brief's points just before its prompt.
                self.brief_waiting.append(nid)
        elif t == "branch.open":
            nid = ev.get("node")
            if nid in st["nodes"]:
                self._open(nid)
        elif t == "record.add":
            rid, nid = ev.get("record"), ev.get("node")
            if rid not in st["records"]:
                return
            s = self._open(nid)
            c = self._tick()
            rec = st["records"][rid]
            kind = "answer" if rec.get("question") else "decision"
            self.dots.append({"col": c, "row": s["row"], "kind": kind, "id": rid, "node": nid,
                              "label": rid, "text": rec["decision"], "rule": rec.get("rule"), "files": rec.get("affects", [])})
            s["to"] = c
            self.pos[rid] = (c, s["row"])
            q = rec.get("question")
            if q in self.qdot:
                self.qdot[q]["answered"] = rid
            for old in self.reminders:
                if old in self.pos and old != rid:
                    self.arcs.append({"kind": "remind", "a": self.pos[old], "b": (c, s["row"]), "label": f"Creek reminded Claude of {old}"})
            self.reminders = []
            if st["nodes"][nid]["status"] == "resolved" and nid not in self.pending_merge:
                self.pending_merge.append(nid)
        elif t == "question.ask":
            qid = ev.get("question")
            q = st["questions"].get(qid)
            if not q:
                return
            if qid in getattr(self, "_was_open", set()) and qid in self.qdot:
                self.qdot[qid]["asks"] = q["asks"]
                return
            nid = q.get("node")
            row = self._open(nid)["row"] if nid in st["nodes"] else 0
            c = self._tick()
            d = {"col": c, "row": row, "kind": "question", "id": qid, "node": nid, "label": qid, "text": q["text"], "asks": q["asks"]}
            self.dots.append(d)
            self.qdot[qid] = d
            if nid in self.seg:
                self.seg[nid]["to"] = c
        elif t == "node.status":
            nid, status = ev.get("node"), ev.get("status")
            s = self.seg.get(nid)
            if s and status in ("tabled", "abandoned"):
                if nid in self.pending_merge:
                    self.pending_merge.remove(nid)
                c = self._tick()
                s["to"], s["end"] = c, status
                self.dots.append({"col": c, "row": s["row"], "kind": status, "node": nid, "label": "parked" if status == "tabled" else "dropped", "text": ev.get("reason") or ""})
                del self.seg[nid]
        elif t == "conflict.flag":
            a, b = (ev.get("records") or [None, None])[:2]
            if a in self.pos and b in self.pos:
                self.arcs.append({"kind": "conflict", "a": self.pos[a], "b": self.pos[b], "label": f"{a} and {b} both change {', '.join(ev.get('overlap') or [])}", "id": ev.get("conflict")})
        elif t == "inject" and ev.get("hook") == "pre-tool-use":
            self.reminders = list(ev.get("records") or [])

    def finish(self) -> None:
        now = self.col + 1
        for s in self.seg.values():
            s["to"] = s["to"] or s["from"]
            s["open_to"] = now
        self.now = now

    def out(self) -> dict:
        nodes = self.st["nodes"]
        rows = [{"row": 0, "id": "main", "title": "Your prompts", "status": "main"}]
        for nid, r in sorted(self.rows.items(), key=lambda kv: kv[1]):
            if nid in nodes:
                rows.append({"row": r, "id": nid, "title": nodes[nid]["title"], "status": nodes[nid]["status"]})
        keep = {r["row"] for r in rows}
        if len(rows) > MAX_ROWS + 1:
            # Big sessions: show the most recently created points.
            keep = {0} | {r["row"] for r in rows[-MAX_ROWS:]}
            rows = [r for r in rows if r["row"] in keep]
        remap = {r["row"]: i for i, r in enumerate(rows)}
        for r in rows:
            r["row"] = remap[r["row"]]
        segs = [dict(s, row=remap[s["row"]]) for s in self.segments if s["row"] in remap]
        dots = [dict(d, row=remap[d["row"]]) for d in self.dots if d["row"] in remap]
        arcs = [dict(a, a=(a["a"][0], remap[a["a"][1]]), b=(a["b"][0], remap[a["b"][1]])) for a in self.arcs if a["a"][1] in remap and a["b"][1] in remap]
        return {"cols": self.now, "rows": rows, "segments": segs, "dots": dots, "arcs": arcs}
