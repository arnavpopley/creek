"""Overlap detection between records on different nodes (§9).

Two records overlap when they name the same file and, if both narrow it to
identifiers ("site.ts profile.about"), at least one identifier is shared.
Overlaps within one node are refinements and are never flagged.
"""

from __future__ import annotations

from .terms import path_overlap


def _split(key: str) -> tuple[str, str | None]:
    f, _, ident = key.partition("#")
    return f, ident or None


def overlap(a: dict, b: dict) -> list[str]:
    files: list[str] = []
    for ka in a.get("affects_norm") or []:
        fa, ia = _split(ka)
        for kb in b.get("affects_norm") or []:
            fb, ib = _split(kb)
            if not path_overlap(fa, fb):
                continue
            if ia and ib and ia != ib:
                continue
            short = min(fa, fb, key=len)
            if short not in files:
                files.append(short)
    return files


def check(tx, rid: str) -> list[str]:
    """Flag conflicts for the new record `rid`. Returns the new conflict ids."""
    st = tx.state
    rec = st["records"][rid]
    if not rec["active"]:
        return []
    pairs = {frozenset(c["records"]) for c in st["conflicts"].values()}
    out = []
    for oid, other in st["records"].items():
        if oid == rid or not other["active"] or oid in rec["supersedes"]:
            continue
        if other["node"] == rec["node"]:
            continue
        if frozenset((oid, rid)) in pairs:
            continue
        files = overlap(rec, other)
        if files:
            ev = tx.emit({"type": "conflict.flag", "records": [oid, rid], "overlap": files})
            out.append(ev["conflict"])
    return out
