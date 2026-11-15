"""`creek` command line. `creek hook <event>` is the hot path, so it skips argparse."""

from __future__ import annotations

import json
import sys


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["hook"]:
        return run_hook(argv[1] if len(argv) > 1 else "")
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

    if not (open_nodes or tabled or questions or conflicts or unrecorded):
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

    i = sub.add_parser("install", help="set up the status line and check the plugin is enabled")
    i.add_argument("--dry-run", action="store_true", help="show the settings change without writing it")
    i.add_argument("--remove", action="store_true", help="take Creek out of the status line again")
    i.set_defaults(fn=cmd_install)

    sl = sub.add_parser("statusline", help="print the one-line status (Claude Code runs this)")
    sl.add_argument("--then", metavar="CMD", help="run an existing status line command first")

    h = sub.add_parser("hook", help="internal: called by Claude Code hooks")
    h.add_argument("event")

    args = p.parse_args(argv)
    if not getattr(args, "fn", None):
        p.print_help()
        return 0
    return args.fn(args) or 0


if __name__ == "__main__":
    raise SystemExit(main())
