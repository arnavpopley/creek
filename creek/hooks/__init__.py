"""Claude Code hook handlers. Each module exposes `run(payload) -> dict | None`."""

from __future__ import annotations

import os

from .. import paths

MODULES = {
    "session-start": "session_start",
    "user-prompt-submit": "user_prompt_submit",
    "pre-tool-use": "pre_tool_use",
    "post-tool-use": "post_tool_use",
    "stop": "stop",
    "pre-compact": "pre_compact",
}


def locate(payload: dict):
    """(creek dir, session id) for a hook payload."""
    cwd = payload.get("cwd") if not os.environ.get("CLAUDE_PROJECT_DIR") else None
    root = paths.project_root(cwd)
    return paths.creek_dir(root), payload.get("session_id") or "unknown"


def env_branch() -> tuple[str | None, str | None]:
    return os.environ.get("CREEK_BRANCH") or None, os.environ.get("CREEK_NODE") or None


def context_output(event: str, text: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}
