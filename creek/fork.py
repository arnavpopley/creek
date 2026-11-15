"""`creek fork <node>`: work on one node in a forked Claude session (§8.2)."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys

from . import actions, ledger, paths


class ForkError(Exception):
    pass


def claude_supports_fork(exe: str) -> bool:
    try:
        out = subprocess.run([exe, "--help"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ForkError(f"could not run `{exe} --help`: {exc}") from exc
    return "--fork-session" in (out.stdout + out.stderr)


def latest_session(state: dict) -> str | None:
    """The most recently active session that isn't itself a fork branch."""
    best, best_ts = None, ""
    for sid, s in state["sessions"].items():
        if s.get("branch") or sid in ("replay", "unknown", "doctor"):
            continue
        ts = s.get("last_ts") or ""
        if ts >= best_ts:
            best, best_ts = sid, ts
    return best


def prepare(node_text: str, session: str | None = None) -> tuple[list[str], dict, str, str]:
    """Open the fork branch. Returns (argv, env additions, branch, node)."""
    exe = shutil.which("claude")
    if not exe:
        raise ForkError("`claude` is not on PATH.")
    if not claude_supports_fork(exe):
        raise ForkError(
            "this version of Claude Code has no --fork-session, so Creek can't fork a session. "
            "Update Claude Code, or use an inline branch instead: type ::branch <node> in the session."
        )
    cdir = paths.creek_dir()
    with ledger.transaction(cdir, "cli") as tx:
        st = tx.state
        nid = actions.find_node(st, node_text)
        if not nid:
            raise ForkError(f"no node {node_text!r}. See `creek status`.")
        parent = session or latest_session(st)
        if not parent:
            raise ForkError("no Claude session has used Creek in this project yet, so there's nothing to fork.")
        bid = tx.emit({"type": "branch.open", "node": nid, "kind": "fork", "parent_session": parent})["branch"]
    env = {"CREEK_BRANCH": bid, "CREEK_NODE": nid}
    return [exe, "--resume", parent, "--fork-session"], env, bid, nid


def shell_line(argv: list[str], env: dict) -> str:
    pre = " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items())
    return f"{pre} {' '.join(shlex.quote(a) for a in ['claude', *argv[1:]])}"


def run(node_text: str, session: str | None = None, print_only: bool = False) -> int:
    try:
        argv, env, bid, nid = prepare(node_text, session)
    except ForkError as exc:
        print(f"creek fork: {exc}", file=sys.stderr)
        return 1
    line = shell_line(argv, env)
    if print_only or not (sys.stdin.isatty() and sys.stdout.isatty()):
        print(f"Opened {bid} on {nid}. Run this in a second terminal:\n  {line}\nIn the fork, type ::merge once Claude has resolved {nid}.")
        return 0
    print(f"Opened {bid} on {nid}; starting a forked session. Type ::merge there once Claude has resolved {nid}.")
    sys.stdout.flush()
    os.execvpe(argv[0], argv, {**os.environ, **env})
    return 0  # not reached
