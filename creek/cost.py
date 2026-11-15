"""`creek cost`: what Creek put in front of the model, per hook, against the transcript."""

from __future__ import annotations

import json
import re
from collections import defaultdict

from . import inject, ledger, paths

_MARKER = re.compile(r"^\s*(?:[-*>]\s+)?[`*]*creek:\s*(on|node|resolve|ask)\b.*$", re.I | re.M)


def injected(cdir, session: str | None = None) -> dict:
    """{session: {hook: {"count", "chars"}}} from inject events, plus transcript paths."""
    events, _, _ = ledger.read_events(cdir, 0)
    per: dict = defaultdict(lambda: defaultdict(lambda: {"count": 0, "chars": 0}))
    transcripts: dict[str, str] = {}
    for ev in events:
        s = ev.get("session")
        if session and s != session:
            continue
        if ev.get("type") == "inject":
            c = per[s][ev.get("hook", "?")]
            c["count"] += 1
            c["chars"] += int(ev.get("chars", 0))
            if ev.get("transcript"):
                transcripts[s] = ev["transcript"]
    return {"per": per, "transcripts": transcripts}


def transcript_usage(path: str) -> dict | None:
    """Token usage summed over a transcript's assistant messages, plus marker lines written."""
    try:
        f = open(path)
    except OSError:
        return None
    totals = {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0, "requests": 0, "marker_lines": 0, "marker_chars": 0}
    seen = set()
    with f:
        for line in f:
            if '"assistant"' not in line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("type") != "assistant":
                continue
            msg = e.get("message") or {}
            mid = msg.get("id")
            u = msg.get("usage") or {}
            # One API response can be split over several transcript entries.
            if mid and mid not in seen:
                seen.add(mid)
                totals["requests"] += 1
                totals["input"] += u.get("input_tokens") or 0
                totals["cache_write"] += u.get("cache_creation_input_tokens") or 0
                totals["cache_read"] += u.get("cache_read_input_tokens") or 0
                totals["output"] += u.get("output_tokens") or 0
            for c in msg.get("content") or []:
                if isinstance(c, dict) and c.get("type") == "text":
                    for m in _MARKER.finditer(c.get("text", "")):
                        totals["marker_lines"] += 1
                        totals["marker_chars"] += len(m.group(0).strip())
    return totals


def run(session: str | None = None, as_json: bool = False) -> int:
    cdir = paths.creek_dir()
    data = injected(cdir, session)
    per = data["per"]
    hooks = ("session-start", "user-prompt-submit", "pre-tool-use")
    total = defaultdict(lambda: {"count": 0, "chars": 0})
    for s, by_hook in per.items():
        for h, c in by_hook.items():
            total[h]["count"] += c["count"]
            total[h]["chars"] += c["chars"]
    usage = {s: transcript_usage(p) for s, p in data["transcripts"].items()}
    if as_json:
        print(json.dumps({"per_session": per, "total": total, "transcripts": usage}, indent=2, default=dict))
        return 0
    if not per:
        print("creek cost: nothing injected yet.")
        return 0
    print(f"{'hook':<22}{'injections':>11}{'chars':>9}{'~tokens':>9}")
    all_c = all_n = 0
    for h in [*hooks, *sorted(set(total) - set(hooks))]:
        if h not in total:
            continue
        c = total[h]
        all_c += c["chars"]
        all_n += c["count"]
        print(f"{h:<22}{c['count']:>11}{c['chars']:>9}{inject.tokens_for(c['chars']):>9}")
    print(f"{'total':<22}{all_n:>11}{all_c:>9}{inject.tokens_for(all_c):>9}")
    for s, u in usage.items():
        mine = sum(c["chars"] for c in per[s].values())
        label = s[:8]
        if not u:
            print(f"\nsession {label}: transcript not readable")
            continue
        new_in = u["input"] + u["cache_write"]
        share = (inject.tokens(" " * mine) / new_in * 100) if new_in else 0
        print(
            f"\nsession {label}: Creek injected ~{inject.tokens_for(mine):,} tokens; the transcript shows "
            f"{u['requests']} requests, {new_in:,} new input tokens (uncached + cache writes), "
            f"{u['cache_read']:,} cache reads, {u['output']:,} output tokens."
            f"\n  Creek's share of new input ≈ {share:.1f}%. Marker lines written by the model: "
            f"{u['marker_lines']} (~{inject.tokens_for(u['marker_chars'])} output tokens)."
        )
    return 0
