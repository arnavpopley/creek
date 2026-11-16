"""Everything Creek puts in front of the model, each within a hard budget.

Token counts are estimated at 4 characters per token.
"""

from __future__ import annotations

import os

from .model import id_num

CHARS_PER_TOKEN = 4
STATE_TOKENS = 1200
NOTES_TOKENS = 400
GUARD_LINE = 220
GUARD_MAX = 3

PROTOCOL = """Creek is tracking this session. Work normally; end a reply with these one-line markers only when they apply:
creek: on <node>   (starting a node: number or short title)
creek: node <title>   (a new point arises that isn't in the brief)
creek: resolve <node> | decision: <what was settled, e.g. a fix or a choice> | rule: <constraint it creates for later work, optional> | affects: <files, identifiers> | tags: <key terms, optional> | supersedes: <record ids, optional>
creek: ask <question>   (only the user can answer; ask once, then don't repeat it, Creek shows it to them)
Records Creek shows you are settled decisions. To go against one, say so and list it under supersedes.
Keep each marker under 300 characters."""


def tokens(text: str) -> int:
    return tokens_for(len(text))


def tokens_for(chars: int) -> int:
    return (chars + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


def clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _files_label(rec: dict, n: int = 2) -> str:
    from .ledger import display_file

    names = [display_file(rec, f) for f in rec.get("files", [])[:n]]
    return ", ".join(dict.fromkeys(names))


def rule_line(rid: str, rec: dict) -> str:
    label = _files_label(rec)
    head = f"{rid} [{label}]" if label else rid
    return clip(f"{head}: {rec['rule']}", GUARD_LINE)


def decision_line(rid: str, rec: dict) -> str:
    return clip(f"{rid} ({rec['node']}): {rec['decision']}", 160)


def render_state(state: dict, session: str | None = None, source: str = "startup", branch: str | None = None) -> tuple[str, list[str]]:
    """The SessionStart state block and the record ids it shows.

    Kept to STATE_TOKENS. When over, resolved decisions go first, then rules
    for resolved nodes. Open questions and open nodes are never dropped for
    those; if they alone overflow, the tail of the node list is summarised.
    """
    budget = STATE_TOKENS * CHARS_PER_TOKEN
    nodes = state["nodes"]
    header: list[str] = ["Creek state:"]
    if branch and branch in state["branches"]:
        br = state["branches"][branch]
        title = nodes.get(br["node"], {}).get("title", "")
        if os.environ.get("CREEK_PANE"):
            header.append(clip(f"You are sub-chat {br['node'][1:]} ({title}) in your own pane. Resolve point {br['node'][1:]} here; the pane then closes itself.", 300))
        else:
            header.append(clip(f"You are in branch {branch} on {br['node']} ({title}). Resolve here, then the user runs ::merge.", 300))
    focus = state["focus"].get(session) or {} if session else {}
    if focus.get("node") in nodes and not branch:
        header.append(clip(f"Focus: {focus['node']} {nodes[focus['node']]['title']}", 120))

    questions = sorted(
        ((k, q) for k, q in state["questions"].items() if q["status"] == "open"),
        key=lambda kv: (-kv[1]["asks"], id_num(kv[0])),
    )
    q_lines = [clip(f"{k} (asked {q['asks']}×): {q['text']} — queued, don't re-ask", 260) for k, q in questions]
    open_lines = [clip(f"{k} open: {n['title']}", 100) for k, n in nodes.items() if n["status"] == "open"]
    tabled_lines = [
        clip(f"{k} tabled: {n['title']}" + (f" — {n['reason']}" if n.get("reason") else ""), 140)
        for k, n in nodes.items()
        if n["status"] == "tabled"
    ]
    conflict_lines = [
        clip(f"{k} conflict: {c['records'][0]} vs {c['records'][1]} on {', '.join(c['overlap'])}", 140)
        for k, c in state["conflicts"].items()
        if c["status"] == "open"
    ]

    active = sorted(((k, r) for k, r in state["records"].items() if r["active"]), key=lambda kv: -id_num(kv[0]))
    unresolved = {k for k, n in nodes.items() if n["status"] != "resolved"}
    rules_open = [(k, rule_line(k, r)) for k, r in active if r.get("rule") and r["node"] in unresolved]
    rules_resolved = [(k, rule_line(k, r)) for k, r in active if r.get("rule") and r["node"] not in unresolved]
    decisions = [(k, decision_line(k, r)) for k, r in active if not r.get("rule")] if source == "compact" else []

    titles = ("Needs the user:", "Open:", "Tabled:", "Conflicts:")
    rules_title, decisions_title = "Active rules (newest first):", "Settled decisions (newest first):"
    # Joined with newlines, so each line costs len + 1; the first saves one.
    used = sum(len(x) + 1 for x in header) - 1
    must: list[list[str]] = [q_lines, open_lines, tabled_lines, conflict_lines]
    kept_must: list[list[str]] = [[] for _ in must]
    reserve = 80
    overflow = 0
    for i, group in enumerate(must):
        for line in group:
            cost = len(line) + 1 + (0 if kept_must[i] else len(titles[i]) + 1)
            if used + cost + reserve <= budget:
                kept_must[i].append(line)
                used += cost
            else:
                overflow += 1
    if overflow:
        more = f"… {overflow} more open items not shown (creek status)."
        used += len(more) + 1

    shown: list[str] = []
    kept_rules: dict[str, str] = {}
    for group in (rules_open, rules_resolved):
        for rid, line in group:
            cost = len(line) + 1 + (0 if kept_rules else len(rules_title) + 1)
            if used + cost <= budget:
                kept_rules[rid] = line
                used += cost
    kept_decisions = []
    for rid, line in decisions:
        cost = len(line) + 1 + (0 if kept_decisions else len(decisions_title) + 1)
        if used + cost <= budget:
            kept_decisions.append(line)
            used += cost
            shown.append(rid)

    out = list(header)
    for i, title in enumerate(titles):
        if kept_must[i]:
            out.append(title)
            out += kept_must[i]
    if overflow:
        out.append(more)
    if kept_rules:
        out.append(rules_title)
        for rid, _ in active:
            if rid in kept_rules:
                out.append(kept_rules[rid])
                shown.append(rid)
    if kept_decisions:
        out.append(decisions_title)
        out += kept_decisions
    if len(out) == 1:
        return "", []
    return "\n".join(out), shown


def render_notes(notes: list[str]) -> tuple[str, int]:
    """Change notes for the next turn, within NOTES_TOKENS. Returns (text, notes used)."""
    budget = NOTES_TOKENS * CHARS_PER_TOKEN
    out = ["Creek: changes since your last turn —"]
    used = len(out[0]) + 1
    n = 0
    for note in notes:
        line = clip(note, 400)
        if used + len(line) + 1 > budget:
            break
        out.append(line)
        used += len(line) + 1
        n += 1
    return ("\n".join(out) if n else ""), n


def guard_line(rid: str, rec: dict) -> str:
    msg = f", msg {rec['turn']}" if rec.get("turn") else ""
    text = f"{rid} ({rec['node']}{msg}): {rec['decision'].rstrip('.')}"
    if rec.get("rule"):
        rule = f". Rule: {rec['rule']}"
        room = GUARD_LINE - len(rule)
        if room < 60:
            return clip(text + rule, GUARD_LINE)
        return clip(text, room) + rule if len(text) > room else text + rule
    return clip(text, GUARD_LINE)


def render_guard(hits: list[tuple[str, dict]]) -> str:
    lines = ["Creek: decisions that touch this edit —"]
    lines += [clip(guard_line(rid, rec), GUARD_LINE) for rid, rec in hits[:GUARD_MAX]]
    return "\n".join(lines)
