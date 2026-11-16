"""Sub-chats as terminal panes: one forked Claude per point, next to the main one.

When a brief arrives in Claude running inside an Orca terminal (or tmux), Creek splits the
tab: the main Claude stays on the left, and each point gets a titled pane on the right running
`claude --resume <main> --fork-session` with that point as its first message. Each pane has its
own input box and the main session's context. When a pane's Claude settles its point, the
branch merges (the main chat gets one line on its next prompt) and the pane closes itself.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from . import ledger, paths
from .model import id_num

CLOSE_AFTER = 8  # seconds a settled pane stays up so its last reply can be read
NUMS = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"


def host() -> str | None:
    """'orca' or 'tmux' when this process runs in a terminal Creek can split; None otherwise."""
    if os.environ.get("CREEK_PANES", "1") == "0":
        return None
    if os.environ.get("ORCA_TERMINAL_HANDLE") and _orca():
        return "orca"
    if os.environ.get("TMUX") and shutil.which("tmux"):
        return "tmux"
    return None


def _orca() -> str | None:
    d = os.environ.get("ORCA_CLI_BIN_DIR")
    if d and os.path.exists(os.path.join(d, "orca")):
        return os.path.join(d, "orca")
    return shutil.which("orca")


def label(nid: str, title: str) -> str:
    n = id_num(nid)
    mark = NUMS[n - 1] if 0 < n <= len(NUMS) else f"{n}."
    return f"{mark} {title[:40]}"


def first_prompt(num: int, title: str, brief: str) -> str:
    return (
        f"You are sub-chat {num} of the brief below, in your own pane. Work only on point {num}: {title}\n"
        f"Leave the other points alone; other panes have them. Keep replies short. When point {num} is settled, "
        f"end your reply with `creek: resolve {num} | decision: … | affects: …` and this pane closes itself.\n\n"
        f"Brief:\n{brief}"
    )


def launch_background(cdir: Path, session: str, nodes: list[str], brief: str) -> None:
    """Start `creek panes open` detached, so the prompt hook returns at once."""
    args = [sys.executable, "-m", "creek", "panes", "open", "--root", str(cdir.parent), "--session", session, "--brief", brief, *nodes]
    log = open(paths.creek_dir(cdir.parent) / "panes.log", "a")
    subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, env=os.environ.copy())


def _run(argv: list[str]) -> dict:
    out = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    try:
        return json.loads(out.stdout)
    except ValueError:
        return {"ok": False, "error": (out.stderr or out.stdout).strip()[:300]}


def _split(kind: str, target: str, direction: str, command: str, cwd: str) -> str | None:
    """Split `target`, run `command` in the new pane, return the new pane's handle."""
    if kind == "orca":
        res = _run([_orca(), "terminal", "split", "--terminal", target, "--direction", direction, "--command", command, "--json"])
        r = res.get("result") or {}
        t = r.get("split") or r.get("terminal") or {}
        return t.get("handle") if isinstance(t, dict) else None
    flag = "-h" if direction == "horizontal" else "-v"
    out = subprocess.run(["tmux", "split-window", flag, "-t", target, "-c", cwd, "-P", "-F", "#{pane_id}", command], capture_output=True, text=True)
    return out.stdout.strip() or None


def set_title(kind: str, handle: str, title: str) -> None:
    if kind == "orca":
        _run([_orca(), "terminal", "set-pane-title", "--terminal", handle, "--title", title, "--json"])
    else:
        subprocess.run(["tmux", "select-pane", "-t", handle, "-T", title], capture_output=True)


def close_later(kind: str, handle: str, delay: int = CLOSE_AFTER) -> None:
    """Close a pane after `delay` seconds, detached from the hook that asks."""
    cmd = f"{shlex.quote(_orca())} terminal close --terminal {shlex.quote(handle)} --json" if kind == "orca" else f"tmux kill-pane -t {shlex.quote(handle)}"
    subprocess.Popen(["/bin/sh", "-c", f"sleep {int(delay)}; {cmd} >/dev/null 2>&1"], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def open_panes(root: Path, session: str, nodes: list[str], brief: str) -> int:
    kind = host()
    if not kind:
        print("creek panes: not in an Orca terminal or tmux", file=sys.stderr)
        return 1
    me = os.environ.get("ORCA_TERMINAL_HANDLE") if kind == "orca" else os.environ.get("TMUX_PANE")
    claude = os.environ.get("CREEK_CLAUDE") or shutil.which("claude") or "claude"
    cdir = paths.creek_dir(root)
    with ledger.transaction(cdir, "cli") as tx:
        st = tx.state
        mode = st["sessions"].get(session, {}).get("mode")
        plan = []
        for nid in nodes:
            if nid not in st["nodes"]:
                continue
            bid = tx.emit({"type": "branch.open", "node": nid, "kind": "fork", "parent_session": session})["branch"]
            plan.append((nid, bid, st["nodes"][nid]["title"]))
    previous = me
    for i, (nid, bid, title) in enumerate(plan):
        argv = [claude, "--resume", session, "--fork-session"]
        if mode:
            argv += ["--permission-mode", mode]
        argv.append(first_prompt(id_num(nid), title, brief))
        env = f"CREEK_BRANCH={bid} CREEK_NODE={nid} CREEK_PANE={kind}"
        command = f"cd {shlex.quote(str(root))} && {env} {' '.join(shlex.quote(a) for a in argv)}"
        # First pane goes beside the main one; the rest stack under it.
        handle = _split(kind, previous, "horizontal" if i == 0 else "vertical", command, str(root))
        if not handle:
            print(f"creek panes: could not open a pane for {nid}", file=sys.stderr)
            continue
        set_title(kind, handle, label(nid, title))
        with ledger.transaction(cdir, "cli") as tx:
            tx.emit({"type": "pane.open", "node": nid, "branch": bid, "handle": handle, "host": kind})
        previous = handle
    if me and kind == "orca":
        set_title(kind, me, "Main")
    return 0


def main(argv: list[str]) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="creek panes")
    sp = p.add_subparsers(dest="cmd", required=True)
    o = sp.add_parser("open", help="open one pane per point (run inside an Orca terminal or tmux)")
    o.add_argument("nodes", nargs="+")
    o.add_argument("--root", required=True)
    o.add_argument("--session", required=True)
    o.add_argument("--brief", default="")
    args = p.parse_args(argv)
    return open_panes(Path(args.root), args.session, args.nodes, args.brief)
