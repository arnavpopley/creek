"""Match an edit against active records (§7.3). Read-only: the hook logs the result."""

from __future__ import annotations

import posixpath

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


def check(state: dict, tool_input: dict, session: str, limit: int = 3) -> list[tuple[str, dict]]:
    """Top records to show for this edit, after the per-session cooldown."""
    path, text = edit_text(tool_input)
    if not state["records"]:
        return []
    scored = score(state, path, text)
    turn = state["turn"].get(session, 0)
    seen = state["injected"].get(session, {})
    ranked = sorted(
        (rid for rid in scored if not (rid in seen and seen[rid] >= turn - COOLDOWN)),
        key=lambda rid: (-scored[rid][0], -id_num(rid)),
    )
    return [(rid, state["records"][rid]) for rid in ranked[:limit]]
