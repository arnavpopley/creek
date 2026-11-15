"""Term extraction for records, and token sets for matching edits against them.

Everything here is plain string work so the guard stays well inside its
latency budget. Terms are lowercase. Matching is on whole tokens, so
`dialog` matches `<dialog>` and `showModal dialog` but not `dialogue`.
"""

from __future__ import annotations

import posixpath
import re

STOP = frozenset(
    """
    a an and are as at be been but by can could did do does done for from had has have
    how if in into is it its just may might must no not now of off on once only
    or other our out over same should so some such than that the their them then there
    these they this those to too under until up use used uses using very was we were what
    when where which while who why will with would you your yours also all any each every
    few new old get got make made need needs keep kept let lets like see set sets via per
    e.g i.e etc ok yes one two three first last next before after again always never ever
    file files line lines code thing things way ways part parts here now page pages
    """.split()
)

# Extensions that mark an affects token as a file rather than a dotted identifier
# like `profile.about`.
FILE_EXTS = frozenset(
    """
    ts tsx js jsx mjs cjs py pyi md mdx json jsonl css scss sass less html htm yml yaml toml
    ini cfg conf env go rs java kt kts swift c h cc cpp hpp m mm rb php sh bash zsh fish txt
    csv sql lock webp png jpg jpeg gif svg ico avif mp4 webm vue svelte astro graphql gql
    proto tf xml plist gradle lua ex exs erl hs ml scala dart r jl ipynb
    """.split()
)

_PR = re.compile(r"^(?:prs?\s*)?#(\d+)$", re.I)
_PR_WORD = re.compile(r"\bPRs?\b", re.I)
_NODE_REF = re.compile(r"^(?:node\s+)?n?\d+$", re.I)
_QUOTED = re.compile(r"""['"`]([^'"`]{3,60})['"`]""")
_BACKTICK = re.compile(r"`([^`]{2,80})`")
_HTML_TAG = re.compile(r"</?([A-Za-z][\w-]*)[^>]{0,40}>")
_CALL = re.compile(r"\b([A-Za-z_$][\w$]*)\(\)")
_CAMEL = re.compile(r"\b[a-z]+[A-Z][A-Za-z0-9]*\b|\b[A-Z][a-z0-9]+[A-Z][A-Za-z0-9]*\b")
_SNAKE = re.compile(r"\b[A-Za-z0-9]+(?:_[A-Za-z0-9]+)+\b")
_DOTTED = re.compile(r"\b[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+\b")
_DIGITS = re.compile(r"\b[A-Za-z]+\d+[A-Za-z0-9]*\b")
_DOLLAR = re.compile(r"\$[A-Za-z][A-Za-z0-9_]*")

_SIMPLE_TOKEN = re.compile(r"[a-z0-9_$]+")
_COMPOUND_TOKEN = re.compile(r"[a-z0-9_$\[][a-z0-9_$.\-/\[\]]*[a-z0-9_$\]]")
_SIMPLE_TERM = re.compile(r"^[a-z0-9_$]+$")
_PUNCT = re.compile(r"[^\w\s$-]")
_WS = re.compile(r"\s+")


def is_file_token(tok: str) -> bool:
    tok = tok.strip("'\"`,;:()")
    if not tok or tok.startswith("#"):
        return False
    base = posixpath.basename(tok.rstrip("/"))
    if "." in base:
        ext = base.rsplit(".", 1)[1].lower()
        if ext in FILE_EXTS:
            return True
    return "/" in tok and len(tok) > 2 and not tok.startswith("http")


def _good(term: str) -> bool:
    t = term.strip().lower()
    return len(t) >= 3 and t not in STOP and not t.isdigit()


def code_tokens(text: str) -> list[str]:
    """Identifiers in prose: backticks, <tags>, calls(), camelCase, snake_case, a.b, x2."""
    if not text:
        return []
    out = []
    out += _BACKTICK.findall(text)
    out += _HTML_TAG.findall(text)
    out += _CALL.findall(text)
    for rx in (_CAMEL, _SNAKE, _DOTTED, _DIGITS, _DOLLAR):
        out += rx.findall(text)
    return out


def parse_affects(entries: list[str]) -> dict:
    """Split affects entries into files, identifiers, PR numbers, and conflict keys.

    "src/config/site.ts profile.about" -> file src/config/site.ts, ident profile.about,
    key src/config/site.ts#profile.about. "PRs #35, #41" -> prs [35, 41].
    "node 3" and plain prose are dropped.
    """
    files, idents, prs, keys = [], [], [], []
    for raw in entries or []:
        entry = str(raw).strip()
        if not entry or _NODE_REF.match(entry):
            continue
        entry_files, entry_idents = [], []
        for q in _QUOTED.findall(entry):
            entry_idents.append(q)
        for tok in entry.replace(",", " ").split():
            clean = tok.strip("'\"`,;:()")
            m = _PR.match(clean)
            if m:
                prs.append(int(m.group(1)))
                continue
            if _PR_WORD.fullmatch(clean) or not clean:
                continue
            if is_file_token(clean):
                entry_files.append(clean.lower())
            elif code_tokens(clean) and not clean.startswith("http"):
                entry_idents.append(clean.lstrip("$") if clean.startswith("$") else clean)
        idents += entry_idents
        files += entry_files
        for f in entry_files:
            if entry_idents:
                keys += [f"{f}#{i.lower()}" for i in entry_idents]
            else:
                keys.append(f)
    return {
        "files": _dedup(files),
        "idents": _dedup(i.lower() for i in idents),
        "prs": _dedup(prs),
        "keys": _dedup(keys),
    }


def extract_terms(affects, tags, rule, decision) -> list[str]:
    a = parse_affects(affects or [])
    terms = []
    for f in a["files"]:
        terms.append(f)
        terms.append(posixpath.basename(f))
    terms += a["idents"]
    terms += [t.lower().strip() for t in tags or []]
    terms += [t.lower() for t in code_tokens(rule or "")]
    terms += [t.lower() for t in code_tokens(decision or "")]
    return sorted({t.strip() for t in terms if _good(t)})


def text_tokens(text: str) -> set[str]:
    """Every simple and compound token in `text`, plus path and dotted suffixes."""
    low = text.lower()
    toks = set(_SIMPLE_TOKEN.findall(low))
    for c in _COMPOUND_TOKEN.findall(low):
        toks.add(c)
        if "/" in c or "." in c:
            parts = re.split(r"([/.])", c)
            # rebuild every suffix that starts after a separator
            for i in range(2, len(parts), 2):
                toks.add("".join(parts[i:]))
    return toks


def phrase_in(term: str, low_text: str) -> bool:
    """Whole-word match for a multi-word term against already-lowercased text."""
    pat = r"(?<![a-z0-9_])" + r"\s+".join(map(re.escape, term.split())) + r"(?![a-z0-9_])"
    return re.search(pat, low_text) is not None


def is_simple_term(term: str) -> bool:
    return bool(_SIMPLE_TERM.match(term))


# Questions -------------------------------------------------------------------

# Function words only: content words like "key" or "more" carry the question.
QSTOP = frozenset(
    """
    a an and are as at be been but by can could did do does for from had has have how i
    if in into is it its me my of on or our should so than that the their them then there
    these they this those to was we were what when where which who why will with would
    you your please still yet ok
    """.split()
)


def normalize_question(text: str) -> str:
    low = _PUNCT.sub(" ", (text or "").lower())
    words = [w for w in _WS.split(low) if w and w not in QSTOP]
    return " ".join(words)


def jaccard(a: str, b: str) -> float:
    sa, sb = set(a.split()), set(b.split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def path_overlap(a: str, b: str) -> bool:
    """Same file: equal, or one is a path suffix of the other."""
    if a == b:
        return True
    return a.endswith("/" + b) or b.endswith("/" + a)


def _dedup(items):
    seen, out = set(), []
    for i in items:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out
