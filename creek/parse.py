"""Grammars: `creek:` marker lines, `::` commands, and the brief splitter.

Nothing here raises on bad input. Malformed markers come back as errors
for the caller to log.
"""

from __future__ import annotations

import re

from .model import ref

VERBS = ("on", "node", "resolve", "ask")
RESOLVE_KEYS = ("decision", "rule", "affects", "tags", "supersedes")
LIST_KEYS = ("affects", "tags", "supersedes")
MAX_FIELD = 600

MARKER = re.compile(r"^\s*creek:\s*(on|node|resolve|ask)\b(.*)$", re.I)
_ANY_CREEK = re.compile(r"^\s*creek:", re.I)
# Markdown the model may wrap a marker in: list bullets, quotes, bold, backticks.
_DECOR_LEAD = re.compile(r"^\s*(?:[-*+>]\s+|\d+[.)]\s+)?(\*\*|__|`)?")
_FIELD = re.compile(r"^\s*([A-Za-z_]+)\s*:\s*(.*)$", re.S)


def _undecorate(line: str) -> str:
    m = _DECOR_LEAD.match(line)
    s = line[m.end():].rstrip()
    wrap = m.group(1)
    # Only strip a closing ** or ` that pairs with an opening one.
    if wrap and s.endswith(wrap):
        s = s[: -len(wrap)].rstrip()
    return s


def _split_list(value: str) -> list[str]:
    return [v.strip()[:MAX_FIELD] for v in value.split(",") if v.strip()]


def parse_markers(text: str | None) -> tuple[list[dict], list[str]]:
    """All markers in a reply, in order, plus error strings for malformed ones."""
    markers: list[dict] = []
    errors: list[str] = []
    for raw in (text or "").splitlines():
        if "creek:" not in raw.lower():
            continue
        line = _undecorate(raw)
        m = MARKER.match(line)
        if not m:
            if _ANY_CREEK.match(line):
                errors.append(f"unknown marker: {line[:200]}")
            continue
        verb, rest = m.group(1).lower(), m.group(2).strip()
        try:
            marker = _PARSERS[verb](rest)
        except ValueError as exc:
            errors.append(f"{exc}: {line[:200]}")
            continue
        marker["line"] = line[:1000]
        markers.append(marker)
    return markers, errors


def _parse_on(rest: str) -> dict:
    if not rest:
        raise ValueError("creek: on needs a node")
    return {"kind": "on", "node": rest[:200]}


def _parse_node(rest: str) -> dict:
    title = rest.strip(" -:")
    if not title:
        raise ValueError("creek: node needs a title")
    return {"kind": "node", "title": title[:200]}


def _parse_ask(rest: str) -> dict:
    q = rest.strip(" -:")
    if not q:
        raise ValueError("creek: ask needs a question")
    return {"kind": "ask", "text": q[:MAX_FIELD]}


def _parse_resolve(rest: str) -> dict:
    segments = rest.split("|")
    out: dict = {"kind": "resolve", "node": None, "decision": None, "rule": None, "affects": [], "tags": [], "supersedes": []}
    first = segments[0].strip()
    fm = _FIELD.match(first)
    if fm and fm.group(1).lower() in RESOLVE_KEYS:
        fields = segments
    else:
        out["node"] = first[:200] or None
        fields = segments[1:]
    for seg in fields:
        fm = _FIELD.match(seg)
        if not fm:
            continue
        key, value = fm.group(1).lower(), fm.group(2).strip()
        if key not in RESOLVE_KEYS or not value:
            continue  # unknown keys are ignored
        if key in LIST_KEYS:
            vals = _split_list(value)
            if key == "supersedes":
                vals = [r for r in (ref(v, "record") for v in vals) if r]
            out[key] = vals
        else:
            out[key] = value[:MAX_FIELD]
    if not out["decision"]:
        raise ValueError("creek: resolve needs decision:")
    return out


_PARSERS = {"on": _parse_on, "node": _parse_node, "resolve": _parse_resolve, "ask": _parse_ask}


# :: commands -------------------------------------------------------------------

COMMANDS = ("status", "answer", "branch", "merge", "table", "drop", "node", "ok", "supersede", "pull", "help")


def parse_command(prompt: str | None) -> tuple[str, str] | None:
    """'::answer q1 o' -> ('answer', 'q1 o'). None if the prompt isn't a command."""
    if not prompt:
        return None
    s = prompt.strip()
    if not s.startswith("::"):
        return None
    body = s[2:].strip()
    name, _, args = body.partition(" ")
    return name.lower(), args.strip()


# Brief splitter ----------------------------------------------------------------

_ITEM = re.compile(r"^([ \t]*)\d+[.)][ \t]+(\S.*)$")
_BULLET = re.compile(r"^([ \t]*)[-*•][ \t]+(\S.*)$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_BRIEF = re.compile(r"^\s*brief\s*:", re.I)
_MD = re.compile(r"(\*\*|__|`)")


def _unfenced(lines):
    fenced = False
    for line in lines:
        if _FENCE.match(line):
            fenced = not fenced
            continue
        if not fenced:
            yield line


def _top_level(rx, lines):
    hits = [(len(m.group(1).expandtabs(4)), m.group(2)) for m in map(rx.match, lines) if m]
    if not hits:
        return []
    top = min(i for i, _ in hits)
    return [text for indent, text in hits if indent == top]


def _title(text: str) -> str:
    t = _MD.sub("", text).strip()
    return t[:80].rstrip()


def split_brief(prompt: str | None) -> list[str]:
    """Node titles for a brief, or [] when the prompt isn't one.

    A brief is a prompt with 3+ top-level numbered items, or one starting with
    `brief:`. Nested items are not split out.
    """
    if not prompt:
        return []
    is_brief = bool(_BRIEF.match(prompt))
    raw = prompt.splitlines()
    if is_brief:
        raw[0] = _BRIEF.sub("", raw[0], count=1).lstrip()
    lines = list(_unfenced(raw))
    items = _top_level(_ITEM, lines)
    if len(items) >= 3:
        return [_title(i) for i in items if _title(i)]
    if not is_brief:
        return []
    if items:
        return [_title(i) for i in items if _title(i)]
    bullets = _top_level(_BULLET, lines)
    if bullets:
        return [_title(b) for b in bullets if _title(b)]
    # brief: one point per line
    return [_title(line) for line in lines if _title(line)]
