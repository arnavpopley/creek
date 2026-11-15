"""PostToolUse: log edits and PR activity (§7.4). No output, no network."""

from __future__ import annotations

import os
import re

from .. import actions, ledger
from . import env_branch, locate

EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
REVIEWERS = ("greptile", "coderabbit", "copilot", "sourcery", "codium", "qodo", "ellipsis", "graphite", "sweep", "codeant", "bito", "korbit", "bugbot")

_GH_PR = re.compile(r"\bgh\s+pr\s+(create|merge|ready|close)\b(.*)", re.S)
_PR_URL = re.compile(r"github\.com/[^\s/]+/[^\s/]+/pull/(\d+)")
_PR_TEXT = re.compile(r"pull request\s+(?:[\w.-]+/[\w.-]+)?#(\d+)", re.I)
_ARG_NUM = re.compile(r"^\s*#?(\d+)\b")
_SCORE = re.compile(r"\b([0-5])\s*/\s*5\b")


def run(payload: dict) -> None:
    tool = payload.get("tool_name") or ""
    ti = payload.get("tool_input") or {}
    cdir, session = locate(payload)
    if tool in EDIT_TOOLS:
        path = ti.get("file_path") or ti.get("notebook_path")
        if not path:
            return None
        with ledger.transaction(cdir, session) as tx:
            node, branch = _focus(tx)
            tx.emit({"type": "edit", "file": _relative(path, cdir), "tool": tool, "node": node, "branch": branch})
        return None
    if tool == "Bash":
        events = pr_events(str(ti.get("command") or ""), _output(payload.get("tool_response")))
        if events:
            with ledger.transaction(cdir, session) as tx:
                node, branch = _focus(tx)
                for ev in events:
                    tx.emit({**ev, "node": node, "branch": branch})
    return None


def _focus(tx):
    node, branch = actions.focus_of(tx.state, tx.session)
    env_b, env_n = env_branch()
    if env_b in tx.state["branches"]:
        branch = env_b
        node = tx.state["branches"][env_b]["node"]
    return node, branch


def _relative(path: str, cdir) -> str:
    root = os.path.realpath(cdir.parent)
    full = os.path.join(os.path.realpath(os.path.dirname(path) or "."), os.path.basename(path))
    try:
        rel = os.path.relpath(full, root)
    except ValueError:
        return path
    return path if rel.startswith("..") else rel


def _output(resp) -> str:
    if isinstance(resp, dict):
        return "\n".join(str(resp.get(k) or "") for k in ("stdout", "stderr", "output"))
    return str(resp or "")


def pr_events(command: str, output: str) -> list[dict]:
    """PR events from one Bash call: `gh pr …` commands and PR URLs in the output."""
    action = None
    numbers: list[int] = []
    m = _GH_PR.search(command)
    if m:
        verb, rest = m.group(1), m.group(2)
        undo = "--undo" in rest
        action = {
            "create": "draft" if "--draft" in rest or " -d" in f" {rest}" else "opened",
            "merge": "merged",
            "ready": "draft" if undo else "opened",
            "close": "closed",
        }[verb]
        n = _ARG_NUM.match(rest.split("--")[0]) if verb != "create" else None
        if n:
            numbers.append(int(n.group(1)))
        if verb == "merge" and re.search(r"\b(failed|error|not mergeable|cannot)\b", output, re.I) and "Merged" not in output:
            return []
    for rx in (_PR_URL, _PR_TEXT):
        for num in rx.findall(output):
            if int(num) not in numbers:
                numbers.append(int(num))
    if not m and not numbers:
        return []
    score, reviewer = review_score(output)
    out = []
    for num in numbers[:3]:
        ev = {"type": "pr", "number": num, "action": action}
        if score is not None:
            ev["score"], ev["reviewer"] = score, reviewer
        out.append(ev)
    return out


def review_score(output: str) -> tuple[int | None, str | None]:
    """'Greptile … 4/5' within one line or 120 characters -> (4, 'greptile')."""
    low = output.lower()
    for name in REVIEWERS:
        start = 0
        while (i := low.find(name, start)) >= 0:
            window = output[max(0, i - 120): i + 120]
            sm = _SCORE.search(window)
            if sm:
                return int(sm.group(1)), name
            start = i + len(name)
    return None, None
