"""`creek` command line. `creek hook <event>` is the hot path, so it skips argparse."""

from __future__ import annotations

import json
import sys


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["hook"]:
        return run_hook(argv[1] if len(argv) > 1 else "")
    if argv[:1] == ["sub"]:
        from .sub import main as sub_main

        return sub_main(argv[1:])
    if argv[:1] == ["statusline"]:
        from .statusline import main as statusline_main

        return statusline_main(argv[1:])
    return _main(argv)


def run_hook(event: str) -> int:
    """Run one hook. Any failure is logged and swallowed: Creek never breaks a session."""
    cdir = None
    try:
        import importlib

        from .hooks import MODULES, locate

        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        cdir, _ = locate(payload)
        mod = importlib.import_module(f"creek.hooks.{MODULES[event]}")
        out = mod.run(payload)
        if out:
            sys.stdout.write(json.dumps(out, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001
        from .ledger import log_error

        log_error(cdir, f"hook {event}", exc)
    return 0


# Status text -------------------------------------------------------------------


def render_status(state: dict, session: str | None = None) -> str:
    nodes = state["nodes"]
    lines: list[str] = []
    focus = (state["focus"].get(session) or {}) if session else {}

    def title(nid):
        return nodes[nid]["title"] if nid in nodes else nid

    open_nodes = [(k, n) for k, n in nodes.items() if n["status"] == "open"]
    tabled = [(k, n) for k, n in nodes.items() if n["status"] == "tabled"]
    questions = sorted(
        ((k, q) for k, q in state["questions"].items() if q["status"] == "open"),
        key=lambda kv: -kv[1]["asks"],
    )
    conflicts = [(k, c) for k, c in state["conflicts"].items() if c["status"] == "open"]
    unrecorded = [(k, n) for k, n in nodes.items() if n.get("unrecorded")]
    branches = [(k, b) for k, b in state["branches"].items() if b["status"] == "open"]

    if not (open_nodes or tabled or questions or conflicts or unrecorded or branches):
        resolved = sum(1 for n in nodes.values() if n["status"] == "resolved")
        return f"creek · clear ({resolved} resolved, {len(state['records'])} records)"

    if open_nodes:
        lines.append(f"Open ({len(open_nodes)}):")
        for k, n in open_nodes:
            mark = "  ← focus" if focus.get("node") == k else ""
            lines.append(f"  {k:<5} {n['title'][:70]}{mark}")
    if tabled:
        lines.append(f"Tabled ({len(tabled)}):")
        for k, n in tabled:
            why = f" — {n['reason']}" if n.get("reason") else ""
            lines.append(f"  {k:<5} {n['title'][:60]}{why}")
    if questions:
        lines.append(f"Needs you ({len(questions)}):")
        for k, q in questions:
            on = f" [{q['node']}]" if q.get("node") else ""
            lines.append(f"  {k:<5} ×{q['asks']} {q['text'][:90]}{on}   → ::answer {k} <answer>")
    if conflicts:
        lines.append(f"Conflicts ({len(conflicts)}):")
        for k, c in conflicts:
            a, b = c["records"][:2]
            lines.append(f"  {k:<5} {a} ↔ {b} on {', '.join(c['overlap'])}   → ::ok {k}  or  ::supersede {k}")
    if branches:
        lines.append(f"Open branches ({len(branches)}):")
        for k, b in branches:
            where = "fork" + ("" if b.get("session") else ", not started") if b["kind"] == "fork" else "inline"
            lines.append(f"  {k:<5} on {b['node']} ({where})   → ::merge in it, or ::table {b['node'][1:]} <reason>")
    if unrecorded:
        lines.append(f"Unrecorded ({len(unrecorded)}):")
        for k, n in unrecorded:
            prs = ", ".join(f"#{p}" for p in n.get("prs", [])) or "a PR"
            lines.append(f"  {k:<5} {n['title'][:60]} — merged {prs} with no record")
    return "\n".join(lines)


# Commands ----------------------------------------------------------------------


def cmd_status(args) -> int:
    from . import ledger, paths

    cdir = paths.creek_dir()
    print(render_status(ledger.load_state(cdir)))
    return 0


def cmd_fork(args) -> int:
    from . import fork

    return fork.run(args.node, session=args.session, print_only=args.print)


def cmd_map(args) -> int:
    from . import mapview

    return mapview.run(open_after=args.open)


def cmd_replay(args) -> int:
    import tempfile
    from pathlib import Path

    from . import paths, replay

    fixture = replay.load(args.fixture)
    ann = replay.load(args.annotations) if args.annotations else None
    if args.into:
        root = Path(args.into).resolve()
        root.mkdir(parents=True, exist_ok=True)
    else:
        root = Path(tempfile.mkdtemp(prefix="creek-replay-"))
    cdir = paths.creek_dir(root)
    if paths.events_path(cdir).exists():
        print(f"creek replay: {cdir} already has a ledger; replay needs a fresh one.", file=sys.stderr)
        return 1
    rep = replay.run(fixture, cdir, ann, use_markers=not args.decisions_only)
    if args.json:
        print(json.dumps(rep, indent=2, ensure_ascii=False))
    else:
        print(f"Replayed {args.fixture} into {cdir}\n")
        print(replay.format_report(rep))
    return 0


def cmd_cost(args) -> int:
    from . import cost

    return cost.run(session=args.session, as_json=args.json)


def cmd_sync(args) -> int:
    from . import sync

    return sync.run(reviews=not args.no_reviews)


def cmd_install(args) -> int:
    from . import install

    return install.install(dry_run=args.dry_run, remove=args.remove)


def cmd_doctor(args) -> int:
    from . import doctor

    return doctor.run(quick=args.quick)


def _main(argv: list[str]) -> int:
    import argparse

    from . import __version__

    p = argparse.ArgumentParser(prog="creek", description="Branch-and-merge decision tracking for Claude Code sessions.")
    p.add_argument("--version", action="version", version=f"creek {__version__}")
    sub = p.add_subparsers(dest="cmd", metavar="<command>")

    sub.add_parser("status", help="open nodes, questions, and conflicts").set_defaults(fn=cmd_status)

    d = sub.add_parser("doctor", help="hook latency, ledger integrity, PATH check")
    d.add_argument("--quick", action="store_true", help="skip the latency benchmark")
    d.set_defaults(fn=cmd_doctor)

    f = sub.add_parser("fork", help="open a forked branch on a node in a second Claude session")
    f.add_argument("node", help="node id (9 or n9) or exact title")
    f.add_argument("--session", help="session to fork (default: the most recently active one)")
    f.add_argument("--print", action="store_true", help="print the command instead of starting claude")
    f.set_defaults(fn=cmd_fork)

    m = sub.add_parser("map", help="write .creek/map.html")
    m.add_argument("--open", action="store_true", help="open it in the browser")
    m.set_defaults(fn=cmd_map)

    y = sub.add_parser("sync", help="pull PR state (and review scores) from gh into the ledger")
    y.add_argument("--no-reviews", action="store_true", help="skip looking up review scores")
    y.set_defaults(fn=cmd_sync)

    c = sub.add_parser("cost", help="what Creek injected, per hook, against transcript token usage")
    c.add_argument("--session", help="only this session id")
    c.add_argument("--json", action="store_true")
    c.set_defaults(fn=cmd_cost)

    r = sub.add_parser("replay", help="load a session reconstruction JSON into a fresh ledger (testing)")
    r.add_argument("fixture")
    r.add_argument("--annotations", help="markers and probe edits to replay with (see tests/fixtures)")
    r.add_argument("--decisions-only", action="store_true", help="ignore annotated rules/tags: the baseline")
    r.add_argument("--into", metavar="DIR", help="project dir for the new ledger (default: a temp dir)")
    r.add_argument("--json", action="store_true", help="print the full report as JSON")
    r.set_defaults(fn=cmd_replay)

    i = sub.add_parser("install", help="set up the status line and check the plugin is enabled")
    i.add_argument("--dry-run", action="store_true", help="show the settings change without writing it")
    i.add_argument("--remove", action="store_true", help="take Creek out of the status line again")
    i.set_defaults(fn=cmd_install)

    sl = sub.add_parser("statusline", help="print the one-line status (Claude Code runs this)")
    sl.add_argument("--then", metavar="CMD", help="run an existing status line command first")

    sub.add_parser("sub", help="sub-chats: `creek sub list` / `creek sub send <point> <text>`", add_help=False)

    h = sub.add_parser("hook", help="internal: called by Claude Code hooks")
    h.add_argument("event")

    args = p.parse_args(argv)
    if not getattr(args, "fn", None):
        p.print_help()
        return 0
    return args.fn(args) or 0


if __name__ == "__main__":
    raise SystemExit(main())
