"""Locate the project root, the .creek directory, and Claude Code settings."""

from __future__ import annotations

import os
from pathlib import Path

CREEK_DIRNAME = ".creek"


def project_root(cwd: str | None = None) -> Path:
    """The git root above `cwd`, or `cwd` itself when there is no git.

    CREEK_ROOT overrides everything (tests, replay). Inside a hook,
    CLAUDE_PROJECT_DIR is the project Claude Code was started in.
    Walking up for `.git` avoids a `git` subprocess on every hook.
    """
    override = os.environ.get("CREEK_ROOT")
    if override:
        return Path(override).resolve()
    start = cwd or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    here = Path(start).resolve()
    for d in (here, *here.parents):
        if (d / ".git").exists():
            return d
    return here


def creek_dir(root: Path | None = None, create: bool = True) -> Path:
    d = (root or project_root()) / CREEK_DIRNAME
    if create and not d.is_dir():
        d.mkdir(parents=True, exist_ok=True)
        # Keep the ledger out of git even if the project's .gitignore doesn't list it.
        (d / ".gitignore").write_text("*\n")
    return d


def events_path(cdir: Path) -> Path:
    return cdir / "events.jsonl"


def state_path(cdir: Path) -> Path:
    return cdir / "state.json"


def lock_path(cdir: Path) -> Path:
    return cdir / "lock"


def errors_path(cdir: Path) -> Path:
    return cdir / "errors.log"


def claude_config_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def user_settings_path() -> Path:
    return claude_config_dir() / "settings.json"
