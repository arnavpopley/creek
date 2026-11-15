"""Match an edit against active records (§7.3). Read-only: the hook logs the result."""

from __future__ import annotations

import posixpath
import re

from . import terms
from .model import id_num

MAX_TEXT = 20_000
COOLDOWN = 15
MIN_SCORE = 2
FILE_SCORE = 3


def edit_text(tool_input: dict) -> tuple[str, str]:
    """(file path, text to check) for Edit, Write, MultiEdit, and NotebookEdit."""
    ti = tool_input or {}
    path = str(ti.get("file_path") or ti.get("notebook_path") or "")
    parts = [path]
    for key in ("new_string", "content", "new_source"):
        if isinstance(ti.get(key), str):
            parts.append(ti[key])
    for e in ti.get("edits") or []:
        if isinstance(e, dict) and isinstance(e.get("new_string"), str):
            parts.append(e["new_string"])
    return path, "\n".join(parts)[:MAX_TEXT]


BUILTIN_EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")

# Shell commands that write files: redirection, tee, in-place sed/perl, scripts that open for writing.
_WRITES = re.compile(
    r"(?<![0-9&<])>>?(?![&>])|\btee\b|\bsed\b[^|;&]*\s-i|\bperl\b[^|;&]*\s-[a-z]*i|"
    r"open\([^)]*['\"][wa]\+?['\"]|write_text|writeFileSync|\bgit\s+apply\b|\bpatch\b|\bapply_patch\b"
)
_TOKEN = re.compile(r"[^\s'\"`;|&<>()]+")


def is_other_edit_tool(name: str) -> bool:
    """Edit-like tools from plugins and MCP servers, e.g. mcp__plugin_woz_code__Edit."""
    return name.startswith("mcp__") and bool(re.search(r"(Edit|Write)", name.rsplit("__", 1)[-1]))


def bash_targets(command: str) -> list[str]:
    """Files a shell command probably writes, or [] when it doesn't look like a write."""
    if not command or not _WRITES.search(command):
        return []
    # A heredoc body is content, not file names.
    head = re.split(r"<<-?\s*['\"]?\w+['\"]?\n", command, maxsplit=1)[0]
    out = []
    for tok in _TOKEN.findall(head):
        t = tok.rstrip(",")
        if t.startswith("-") or t.startswith("/dev/") or t in out:
            continue
        base = posixpath.basename(t)
        # Only names with a real file extension: sed expressions and URLs have slashes too.
        if "." in base and base.rsplit(".", 1)[1].lower() in terms.FILE_EXTS and "://" not in t:
            out.append(t)
    return out[:5]


def _strings(obj, out: list, depth: int = 0) -> None:
    if depth > 4 or len(out) > 200:
        return
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            _strings(v, out, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            _strings(v, out, depth + 1)


def generic_edit(tool_input: dict) -> tuple[list[str], str]:
    """(paths, text) from an unknown edit tool: path-like fields, and every string in it."""
    paths = []
    for k, v in (tool_input or {}).items():
        if isinstance(v, str) and ("path" in k.lower() or "file" in k.lower()):
            paths.append(v)
        elif isinstance(v, list) and ("path" in k.lower() or "file" in k.lower()):
            paths += [x for x in v if isinstance(x, str)]
    for e in (tool_input or {}).get("edits") or []:
        if isinstance(e, dict):
            paths += [v for k, v in e.items() if isinstance(v, str) and ("path" in k.lower() or "file" in k.lower())]
    texts: list[str] = []
    _strings(tool_input, texts)
    return list(dict.fromkeys(paths))[:5], "\n".join(texts)[:MAX_TEXT]


def targets(tool_name: str, tool_input: dict) -> tuple[list[str], str]:
    """(files, text to check) for any tool call Creek treats as an edit; ([], "") otherwise."""
    if tool_name in BUILTIN_EDIT_TOOLS:
        path, text = edit_text(tool_input)
        return ([path] if path else []), text
    if tool_name == "Bash":
        cmd = str((tool_input or {}).get("command") or "")
        files = bash_targets(cmd)
        return files, (cmd[:MAX_TEXT] if files else "")
    if is_other_edit_tool(tool_name):
        return generic_edit(tool_input)
    return [], ""


def file_matches(rec: dict, path_low: str) -> bool:
    base = posixpath.basename(path_low)
    for f in rec["files"]:
        if f == base or f == path_low or path_low.endswith("/" + f.lstrip("./")):
            return True
    return False


def score(state: dict, path: str, text: str) -> dict[str, tuple[int, list[str], bool]]:
    """rid -> (score, matched terms, file matched) for every active record that scores."""
    idx = state["index"]
    records = state["records"]
    path_low = path.lower()
    base = posixpath.basename(path_low)
    file_hits = set()
    if base:
        for rid in idx["file"].get(base, []):
            rec = records.get(rid)
            if rec and rec["active"] and file_matches(rec, path_low):
                file_hits.add(rid)

    low = text.lower()
    toks = terms.text_tokens(low)
    term_hits: dict[str, list[str]] = {}
    keys = idx["term"]
    matched = [t for t in toks if t in keys]
    matched += [t for t in keys if " " in t and terms.phrase_in(t, low)]
    for t in matched:
        for rid in keys[t]:
            term_hits.setdefault(rid, []).append(t)

    out = {}
    for rid in file_hits | set(term_hits):
        rec = records.get(rid)
        if not rec or not rec["active"]:
            continue
        hit_terms = sorted(set(term_hits.get(rid, [])))
        s = (FILE_SCORE if rid in file_hits else 0) + len(hit_terms) + (1 if rec.get("rule") else 0)
        if s >= MIN_SCORE:
            out[rid] = (s, hit_terms, rid in file_hits)
    return out


def check(state: dict, tool_input: dict, session: str, limit: int = 3, tool_name: str = "Edit") -> list[tuple[str, dict]]:
    """Top records to show for this edit, after the per-session cooldown."""
    if not state["records"]:
        return []
    files, text = targets(tool_name, tool_input)
    if not files and not text:
        return []
    scored: dict = {}
    for path in files or [""]:
        for rid, hit in score(state, path, text).items():
            if rid not in scored or hit[0] > scored[rid][0]:
                scored[rid] = hit
    turn = state["turn"].get(session, 0)
    seen = state["injected"].get(session, {})
    ranked = sorted(
        (rid for rid in scored if not (rid in seen and seen[rid] >= turn - COOLDOWN)),
        key=lambda rid: (-scored[rid][0], -id_num(rid)),
    )
    return [(rid, state["records"][rid]) for rid in ranked[:limit]]
