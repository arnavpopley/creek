"""Synthetic ledgers for tests and `creek doctor` latency checks."""

from __future__ import annotations

import random

WORDS = (
    "header panel dialog overlay pointer ticker stats hero contact keys nav theme cache "
    "scroll focus modal layout byline graph clock sparkline crosshair redirect config"
).split()
FILES = [
    "src/components/Header.tsx",
    "src/components/HelpPanel.tsx",
    "src/components/ContactList.tsx",
    "src/components/Hero.tsx",
    "src/components/Crosshair.tsx",
    "src/config/site.ts",
    "src/app/layout.tsx",
    "next.config.ts",
]


def random_events(n: int, seed: int = 0, sessions: int = 3) -> list[dict]:
    """`n` plausible events referring only to ids that exist at that point."""
    rng = random.Random(seed)
    sess = [f"s{i}" for i in range(sessions)]
    out: list[dict] = []
    nodes = branches = records = questions = conflicts = 0

    def pick(k):
        return f"{k}{rng.randint(1, {'n': nodes, 'b': branches, 'r': records, 'q': questions, 'c': conflicts}[k])}"

    for i in range(n):
        s = rng.choice(sess)
        roll = rng.random()
        ev: dict
        if nodes == 0 or roll < 0.08:
            nodes += 1
            ev = {"type": "node.create", "title": " ".join(rng.sample(WORDS, 3)), "origin": rng.choice(["brief", "model", "user"]), "order": nodes}
        elif roll < 0.14:
            ev = {"type": "focus", "node": pick("n")}
        elif roll < 0.19:
            branches += 1
            ev = {"type": "branch.open", "node": pick("n"), "kind": rng.choice(["inline", "fork"]), "parent_session": s}
        elif roll < 0.30:
            records += 1
            sup = [pick("r")] if records > 1 and rng.random() < 0.15 else []
            ev = {
                "type": "record.add",
                "node": pick("n"),
                "branch": pick("b") if branches and rng.random() < 0.5 else None,
                "decision": " ".join(rng.sample(WORDS, 6)),
                "rule": " ".join(rng.sample(WORDS, 5)) if rng.random() < 0.6 else None,
                "affects": rng.sample(FILES, rng.randint(1, 2)),
                "tags": rng.sample(WORDS, 2),
                "supersedes": sup,
            }
        elif roll < 0.35:
            questions += 1
            ev = {"type": "question.ask", "text": f"Which {' '.join(rng.sample(WORDS, 3))} should we use?"}
        elif roll < 0.37 and questions:
            ev = {"type": "question.answer", "question": pick("q"), "answer": rng.choice(WORDS)}
        elif roll < 0.40 and branches:
            ev = {"type": "branch.merge", "branch": pick("b")}
        elif roll < 0.42 and branches:
            ev = {"type": "branch.table", "branch": pick("b"), "reason": "later"}
        elif roll < 0.45:
            ev = {"type": "node.status", "node": pick("n"), "status": rng.choice(["open", "resolved", "tabled", "abandoned"])}
        elif roll < 0.62:
            ev = {"type": "edit", "file": rng.choice(FILES), "tool": "Edit", "node": pick("n")}
        elif roll < 0.66:
            ev = {"type": "pr", "number": rng.randint(1, 60), "action": rng.choice(["opened", "merged", "draft", "closed"]), "node": pick("n")}
        elif roll < 0.69 and records >= 2:
            conflicts += 1
            a, b = rng.sample(range(1, records + 1), 2)
            ev = {"type": "conflict.flag", "records": [f"r{a}", f"r{b}"], "overlap": [rng.choice(FILES).lower()]}
        elif roll < 0.71 and conflicts:
            ev = {"type": "conflict.clear", "conflict": pick("c"), "resolution": rng.choice(["ok", "superseded"])}
        elif roll < 0.78 and records:
            ev = {"type": "inject", "hook": "pre-tool-use", "records": [pick("r")], "chars": rng.randint(50, 600)}
        elif roll < 0.80:
            ev = {"type": "compact", "trigger": rng.choice(["manual", "auto"])}
        elif roll < 0.83:
            ev = {"type": "notes.clear", "count": rng.randint(0, 3)}
        elif roll < 0.85:
            # Deliberately bad: refers to a node that may not exist.
            ev = {"type": "node.status", "node": f"n{nodes + 50}", "status": "open"}
        else:
            ev = {"type": "turn"}
        ev["session"] = s
        ev["v"] = 1
        ev["ts"] = f"2026-11-15T12:00:{i % 60:02d}.000Z"
        out.append(ev)
    return out


def big_project(n_nodes: int = 300, seed: int = 1) -> list[dict]:
    """A ledger with `n_nodes` nodes, a branch and record on most, and open questions."""
    rng = random.Random(seed)
    out: list[dict] = []
    rec = 0
    for i in range(1, n_nodes + 1):
        out.append({"type": "node.create", "title": f"{' '.join(rng.sample(WORDS, 4))} #{i}".capitalize(), "origin": "brief", "order": i})
        if rng.random() < 0.7:
            out.append({"type": "branch.open", "node": f"n{i}", "kind": "inline"})
            for _ in range(rng.randint(1, 2)):
                rec += 1
                out.append({
                    "type": "record.add",
                    "node": f"n{i}",
                    "decision": " ".join(rng.sample(WORDS, 8)),
                    "rule": " ".join(rng.sample(WORDS, 8)) if rng.random() < 0.7 else None,
                    "affects": rng.sample(FILES, 2),
                    "tags": rng.sample(WORDS, 2),
                })
                out.append({"type": "edit", "file": rng.choice(FILES), "tool": "Edit", "node": f"n{i}"})
        if rng.random() < 0.1:
            out.append({"type": "node.status", "node": f"n{i}", "status": "tabled", "reason": "later"})
        if rng.random() < 0.05:
            out.append({"type": "question.ask", "text": f"Question {i}: {' '.join(rng.sample(WORDS, 5))}?", "node": f"n{i}"})
        if rng.random() < 0.3:
            out.append({"type": "turn"})
    for i, ev in enumerate(out):
        ev.setdefault("session", "main")
        ev["v"] = 1
        ev["ts"] = f"2026-11-15T12:{(i // 60) % 60:02d}:{i % 60:02d}.000Z"
    return out
