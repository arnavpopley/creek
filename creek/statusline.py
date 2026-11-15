"""`creek statusline`: one line from state.json, for Claude Code's status line (§11).

Kept import-light: it runs on every status line refresh.
"""

from __future__ import annotations

import json
import os
import sys
import time

FRESH_SECONDS = 2.0
MAX_WIDTH = 80


def render(state: dict | None) -> str:
    if not state:
        return "creek · clear"
    nodes = state.get("nodes", {})
    n_open = sum(1 for n in nodes.values() if n["status"] == "open")
    qs = [(k, q) for k, q in state.get("questions", {}).items() if q["status"] == "open"]
    n_conf = sum(1 for c in state.get("conflicts", {}).values() if c["status"] == "open")
    n_unrec = sum(1 for n in nodes.values() if n.get("unrecorded"))
    parts = ["creek"]
    if n_open:
        parts.append(f"{n_open} open")
    if qs:
        top, q = max(qs, key=lambda kv: (kv[1]["asks"], -int(kv[0][1:])))
        parts.append(f"{len(qs)} you ({top} ×{q['asks']})")
    if n_conf:
        parts.append(f"{n_conf} conflict" + ("s" if n_conf > 1 else ""))
    if n_unrec:
        parts.append(f"{n_unrec} unrecorded")
    if len(parts) == 1:
        return "creek · clear"
    line = " · ".join(parts)
    return line if len(line) <= MAX_WIDTH else line[: MAX_WIDTH - 1] + "…"


def _root_from(payload: dict) -> str | None:
    ws = payload.get("workspace") or {}
    return ws.get("project_dir") or ws.get("current_dir") or payload.get("cwd")


def _project_root(cwd: str | None) -> str:
    # Same rules as paths.project_root, without importing pathlib (~10 ms on 3.13).
    override = os.environ.get("CREEK_ROOT")
    if override:
        return os.path.realpath(override)
    here = os.path.realpath(cwd or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    d = here
    while True:
        if os.path.exists(os.path.join(d, ".git")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return here
        d = parent


def load(cwd: str | None) -> dict | None:
    cdir = os.path.join(_project_root(cwd), ".creek")
    sp = os.path.join(cdir, "state.json")
    try:
        st_mtime = os.stat(sp).st_mtime
    except FileNotFoundError:
        st_mtime = None
    if st_mtime is not None and time.time() - st_mtime < FRESH_SECONDS:
        return _read(sp)
    try:
        log_size = os.stat(os.path.join(cdir, "events.jsonl")).st_size
    except FileNotFoundError:
        return None
    state = _read(sp) if st_mtime is not None else None
    if state is not None and state.get("offset") == log_size:
        return state
    from pathlib import Path

    from . import ledger

    return ledger.load_state(Path(cdir))


def _read(path) -> dict | None:
    try:
        with open(path, "rb") as f:
            return json.loads(f.read())
    except (OSError, ValueError):
        return None


def main(argv: list[str]) -> int:
    then = None
    if "--then" in argv:
        i = argv.index("--then")
        then = argv[i + 1] if i + 1 < len(argv) else None
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        payload = {}
    if then:
        import subprocess

        try:
            out = subprocess.run(then, shell=True, input=raw, capture_output=True, text=True, timeout=5).stdout.rstrip("\n")
        except (OSError, subprocess.TimeoutExpired):
            out = ""
        if out:
            sys.stdout.write(out + "\n")
    try:
        line = render(load(_root_from(payload)))
    except Exception:  # noqa: BLE001 - the status line must always print something
        line = "creek · ?"
    sys.stdout.write(line + "\n")
    return 0
