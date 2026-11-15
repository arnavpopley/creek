"""`creek sync`: PR state from `gh` into the ledger. The only network Creek does, on request."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys

from . import ledger, paths
from .hooks.post_tool_use import review_score

LIMIT = 100
REVIEW_LOOKUPS = 20


def _gh(args: list[str], cwd) -> str:
    gh = shutil.which("gh")
    if not gh:
        raise RuntimeError("`gh` is not on PATH (https://cli.github.com).")
    out = subprocess.run([gh, *args], cwd=cwd, capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or f"gh {' '.join(args)} failed")
    return out.stdout


def action_of(pr: dict) -> str:
    state = (pr.get("state") or "").upper()
    if state == "MERGED" or pr.get("mergedAt"):
        return "merged"
    if state == "CLOSED":
        return "closed"
    return "draft" if pr.get("isDraft") else "opened"


def run(reviews: bool = True) -> int:
    cdir = paths.creek_dir()
    root = cdir.parent
    try:
        prs = json.loads(_gh(["pr", "list", "--state", "all", "--limit", str(LIMIT), "--json", "number,state,isDraft,mergedAt,title"], root))
    except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"creek sync: {exc}", file=sys.stderr)
        return 1
    changed = []
    with ledger.transaction(cdir, "cli") as tx:
        known = tx.state["prs"]
        for pr in prs:
            num, action = int(pr["number"]), action_of(pr)
            if (known.get(str(num)) or {}).get("action") != action:
                tx.emit({"type": "pr", "number": num, "action": action, "title": (pr.get("title") or "")[:120], "via": "sync"})
                changed.append((num, action))
        scores = []
        if reviews:
            # Only PRs Creek already ties to a node, newest first: that's where a score means something.
            mine = sorted((int(n) for n, p in known.items() if p.get("node")), reverse=True)[:REVIEW_LOOKUPS]
            for num in mine:
                try:
                    view = json.loads(_gh(["pr", "view", str(num), "--json", "comments,reviews"], root))
                except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired):
                    continue
                bodies = [c.get("body") or "" for c in view.get("comments") or []] + [r.get("body") or "" for r in view.get("reviews") or []]
                score, who = None, None
                for body in reversed(bodies):  # latest score wins
                    score, who = review_score(body)
                    if score is not None:
                        break
                if score is not None and known[str(num)].get("score") != score:
                    tx.emit({"type": "pr", "number": num, "action": None, "score": score, "reviewer": who, "via": "sync"})
                    scores.append((num, who, score))
    print(f"creek sync: {len(prs)} PRs checked; {len(changed)} changed" + (f"; {len(scores)} review scores" if scores else "") + ".")
    for num, action in changed[:20]:
        print(f"  #{num} {action}")
    for num, who, score in scores:
        print(f"  #{num} {who} {score}/5")
    return 0
