"""`creek doctor`: PATH, ledger integrity, settings, and hook latency."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import ledger, paths

OK, WARN, FAIL = "ok  ", "warn", "FAIL"


def _line(status: str, text: str) -> None:
    print(f"  [{status}] {text}")


def creek_command() -> list[str]:
    exe = shutil.which("creek")
    return [exe] if exe else [sys.executable, "-m", "creek"]


def check_path() -> bool:
    good = True
    exe = shutil.which("creek")
    if exe:
        _line(OK, f"creek on PATH: {exe}")
    else:
        _line(FAIL, "creek is not on PATH; hooks run `creek hook …` and will do nothing (pipx install creek-cc)")
        good = False
    if sys.version_info < (3, 11):
        _line(FAIL, f"Python {sys.version.split()[0]} is too old; Creek needs 3.11+")
        good = False
    else:
        _line(OK, f"Python {sys.version.split()[0]}")
    return good


def check_ledger(cdir: Path) -> bool:
    if not paths.events_path(cdir).exists():
        _line(OK, f"no ledger yet at {cdir}")
        return True
    good = True
    full = ledger.rebuild(cdir)
    cached = ledger.load_state(cdir)
    n = full["seq"]
    if full.get("bad_lines"):
        _line(WARN, f"{full['bad_lines']} unreadable line(s) in events.jsonl (skipped)")
    if _comparable(full) == _comparable(cached):
        _line(OK, f"ledger: {n} events; cached state matches a full rebuild")
    else:
        _line(FAIL, "state.json differs from a full rebuild; delete .creek/state.json to rebuild it")
        good = False
    if full["errors"]:
        _line(WARN, f"{full['errors']} event(s) could not be applied (bad references or malformed markers)")
    err = paths.errors_path(cdir)
    if err.exists() and err.stat().st_size:
        count = sum(1 for line in err.read_text(errors="replace").splitlines() if line[:4].isdigit())
        _line(WARN, f"{count} hook error(s) logged in {err}")
    return good


def _comparable(state: dict) -> dict:
    s = dict(state)
    s.pop("bad_lines", None)
    return json.loads(json.dumps(s))


def check_settings() -> None:
    sp = paths.user_settings_path()
    try:
        settings = json.loads(sp.read_text())
    except (FileNotFoundError, ValueError):
        settings = {}
    cmd = (settings.get("statusLine") or {}).get("command", "")
    if "creek statusline" in cmd:
        _line(OK, "status line installed")
    else:
        _line(WARN, "status line not installed (run `creek install`)")
    plugins = settings.get("enabledPlugins") or {}
    if any(k.split("@")[0] == "creek" and v for k, v in plugins.items()):
        _line(OK, "creek plugin enabled")
    else:
        _line(WARN, "creek plugin not enabled in user settings (see `creek install`); fine if loaded with --plugin-dir or project settings")


def _payloads(root: Path) -> dict:
    base = {"session_id": "doctor", "cwd": str(root), "transcript_path": "", "hook_event_name": ""}
    edit = {"file_path": str(root / "src/components/ContactList.tsx"), "old_string": "a", "new_string": "const open = document.querySelector('dialog[open]'); if (e.repeat) return;" * 20}
    return {
        "session-start": {**base, "source": "startup"},
        "user-prompt-submit": {**base, "prompt": "Please tidy the header spacing and keep the help panel as is."},
        "pre-tool-use": {**base, "tool_name": "Edit", "tool_input": edit},
        "post-tool-use": {**base, "tool_name": "Edit", "tool_input": edit, "tool_response": {}},
        "stop": {**base, "last_assistant_message": "Done.\ncreek: resolve | decision: kept spacing | affects: src/components/Header.tsx", "stop_hook_active": False},
        "pre-compact": {**base, "trigger": "manual"},
    }


def bench(n_events: int = 500, runs: int = 15) -> dict[str, float]:
    """p95 wall time (ms) of each hook against a synthetic ledger of `n_events`."""
    from .synth import random_events

    results = {}
    with tempfile.TemporaryDirectory(prefix="creek-doctor-") as tmp:
        root = Path(tmp)
        (root / ".git").mkdir()
        cdir = paths.creek_dir(root)
        with open(paths.events_path(cdir), "w") as f:
            for ev in random_events(n_events, seed=7):
                f.write(json.dumps(ev) + "\n")
        ledger.load_state(cdir)
        env = {**os.environ, "CLAUDE_PROJECT_DIR": str(root)}
        env.pop("CREEK_BRANCH", None)
        cmd = creek_command()
        for event, payload in _payloads(root).items():
            data = json.dumps(payload).encode()
            times = []
            for _ in range(runs):
                t0 = time.perf_counter()
                subprocess.run([*cmd, "hook", event], input=data, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                times.append((time.perf_counter() - t0) * 1000)
            times.sort()
            results[event] = times[min(len(times) - 1, int(round(0.95 * (len(times) - 1))))]
        errs = paths.errors_path(cdir)
        results["_errors"] = errs.read_text() if errs.exists() else ""
    return results


def check_latency() -> bool:
    res = bench()
    errors = res.pop("_errors")
    good = True
    for event, p95 in res.items():
        status = OK if p95 < 100 else FAIL
        good &= p95 < 100
        _line(status, f"hook {event:<19} p95 {p95:6.1f} ms (500-event ledger)")
    if errors:
        _line(FAIL, "hooks logged errors during the benchmark:\n" + errors[-1500:])
        good = False
    return good


def check_claude() -> None:
    exe = shutil.which("claude")
    if not exe:
        _line(WARN, "claude not on PATH (needed for `creek fork`)")
        return
    try:
        out = subprocess.run([exe, "--help"], capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.TimeoutExpired):
        _line(WARN, "could not run `claude --help`")
        return
    if "--fork-session" in out:
        _line(OK, "claude supports --fork-session (creek fork)")
    else:
        _line(WARN, "this claude has no --fork-session; `creek fork` will refuse to run")


def run(quick: bool = False) -> int:
    print("creek doctor")
    good = check_path()
    root = paths.project_root()
    good &= check_ledger(paths.creek_dir(root, create=False)) if (root / paths.CREEK_DIRNAME).is_dir() else True
    check_settings()
    check_claude()
    if not quick:
        good &= check_latency()
    return 0 if good else 1
