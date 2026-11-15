"""`creek install`: status line in ~/.claude/settings.json, plugin check (§11)."""

from __future__ import annotations

import json
import shlex
import shutil
import time

from . import paths

STATUS_CMD = "creek statusline"
MARKETPLACE = "arnavpopley/creek"


def wrap(existing: str | None) -> str:
    if not existing:
        return STATUS_CMD
    if existing.startswith(STATUS_CMD):
        return existing
    return f"{STATUS_CMD} --then {shlex.quote(existing)}"


def unwrap(command: str) -> str | None:
    """The command `creek install` wrapped, or None if it was creek's own."""
    if not command.startswith(STATUS_CMD):
        return command
    rest = command[len(STATUS_CMD):].strip()
    if rest.startswith("--then"):
        parts = shlex.split(rest)
        return parts[1] if len(parts) > 1 else None
    return None


def _load(sp):
    if not sp.exists():
        return {}
    text = sp.read_text()
    if not text.strip():
        return {}
    return json.loads(text)  # a ValueError here stops install before anything is written


def _backup(sp) -> str | None:
    if not sp.exists():
        return None
    dest = sp.with_name(f"{sp.name}.creek-backup-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(sp, dest)
    return str(dest)


def _write(sp, settings) -> None:
    sp.parent.mkdir(parents=True, exist_ok=True)
    tmp = sp.with_name(sp.name + ".creek-tmp")
    tmp.write_text(json.dumps(settings, indent=2) + "\n")
    tmp.replace(sp)


def install(dry_run: bool = False, remove: bool = False) -> int:
    sp = paths.user_settings_path()
    try:
        settings = _load(sp)
    except ValueError as exc:
        print(f"creek: {sp} is not valid JSON ({exc}); not touching it.")
        return 1
    sl = dict(settings.get("statusLine") or {})
    current = sl.get("command")
    if remove:
        if not current or not current.startswith(STATUS_CMD):
            print("creek: status line isn't Creek's; nothing to remove.")
            return 0
        inner = unwrap(current)
        if inner:
            sl["command"] = inner
            settings["statusLine"] = sl
        else:
            settings.pop("statusLine", None)
    else:
        new = wrap(current)
        if new == current:
            print(f"creek: status line already set ({current}).")
            new = None
        else:
            sl.update({"type": "command", "command": new})
            settings["statusLine"] = sl
    changed = remove or (not remove and new)
    if changed:
        if dry_run:
            print(f"creek: would set statusLine in {sp} to:\n  {json.dumps(settings.get('statusLine'))}")
        else:
            backup = _backup(sp)
            _write(sp, settings)
            print(f"creek: {'removed the' if remove else 'set the'} status line in {sp}" + (f" (backup: {backup})" if backup else ""))
            if current and not remove and current != STATUS_CMD:
                print(f"       your existing status line still prints first: {current}")
    if not remove:
        _plugin_check(settings)
        if not shutil.which("creek"):
            print("creek: warning: `creek` is not on PATH, so the hooks and status line can't run it.")
    return 0


def _plugin_check(settings: dict) -> None:
    plugins = settings.get("enabledPlugins") or {}
    enabled = [k for k, v in plugins.items() if k.split("@")[0] == "creek" and v]
    if enabled:
        print(f"creek: plugin enabled ({enabled[0]}).")
        return
    print(
        "creek: the Creek plugin (its hooks) isn't enabled yet. Install it with:\n"
        f"  claude plugin marketplace add {MARKETPLACE}\n"
        "  claude plugin install creek@creek\n"
        "or, for one session: claude --plugin-dir <path to creek>/plugin"
    )
