"""Read Claude Code transcript JSONL without loading the whole file."""

from __future__ import annotations

import json
import os

TAIL = 512 * 1024


def size(path: str | None) -> int | None:
    try:
        return os.path.getsize(path) if path else None
    except OSError:
        return None


def _message_text(entry: dict) -> str:
    msg = entry.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return content
    parts = []
    for c in content or []:
        if isinstance(c, dict) and c.get("type") == "text":
            parts.append(c.get("text", ""))
    return "\n".join(parts)


def _entries_before(path: str, end: int | None, window: int = TAIL):
    """Transcript entries in the `window` bytes before `end`, newest first."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        stop = f.tell() if end is None else min(end, f.tell())
        start = max(0, stop - window)
        f.seek(start)
        data = f.read(stop - start)
    lines = data.split(b"\n")
    if start > 0:
        lines = lines[1:]  # first line is probably cut
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            yield json.loads(line)
        except ValueError:
            continue


def last_assistant_text(path: str | None) -> str | None:
    """Text of the last main-thread assistant message that has any text."""
    if not path:
        return None
    try:
        for e in _entries_before(path, None):
            if e.get("type") == "assistant" and not e.get("isSidechain"):
                text = _message_text(e)
                if text.strip():
                    return text
    except OSError:
        return None
    return None


def find_reply(path: str | None, end: int | None, needle: str) -> str | None:
    """The assistant reply before byte `end` that contains `needle`."""
    if not path:
        return None
    try:
        for e in _entries_before(path, end, window=4 * TAIL):
            if e.get("type") == "assistant":
                text = _message_text(e)
                if needle in text:
                    return text
    except OSError:
        return None
    return None
