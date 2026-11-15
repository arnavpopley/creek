"""`creek map`: write .creek/map.html, one self-contained file (§12)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path

from . import ledger, paths

# Derived bookkeeping the viewer doesn't need.
_DROP = ("index", "injected", "pending_notes", "pending_pulls", "offset", "next", "cost")


def payload(state: dict, project: str) -> dict:
    view = {k: v for k, v in state.items() if k not in _DROP}
    view["records"] = {}
    for rid, r in state.get("records", {}).items():
        r = {k: v for k, v in r.items() if k not in ("source_msg", "terms", "affects_norm")}
        r["display_files"] = list(dict.fromkeys(ledger.display_file(r, f) for f in r.get("files", [])))
        view["records"][rid] = r
    view["conflicts"] = {}
    for cid, c in state.get("conflicts", {}).items():
        c = dict(c)
        newer = state["records"].get(max(c["records"], key=lambda x: int(x[1:])), {})
        c["display_overlap"] = [ledger.display_file(newer, f) for f in c.get("overlap", [])]
        view["conflicts"][cid] = c
    return {
        "project": project,
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "state": view,
    }


def render(state: dict, project: str) -> str:
    template = resources.files("creek").joinpath("templates/map.html").read_text()
    data = json.dumps(payload(state, project), ensure_ascii=False, separators=(",", ":"))
    # Keep the JSON inert inside <script>: no closing tags or comment openers.
    data = data.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    title = project.replace("&", "&amp;").replace("<", "&lt;")
    return template.replace("__CREEK_TITLE__", title).replace("__CREEK_DATA__", data)


def write(cdir: Path | None = None, state: dict | None = None) -> Path:
    cdir = cdir or paths.creek_dir()
    state = state if state is not None else ledger.load_state(cdir)
    out = cdir / "map.html"
    out.write_text(render(state, cdir.parent.name), encoding="utf-8")
    return out


ORCA_LIMIT = 500_000


def orca_dir() -> Path | None:
    """The Orca chat's scratch folder when running inside Orca, if writable."""
    d = os.environ.get("ORCA_CHAT_SCRATCH_DIR")
    if not d or not os.path.isdir(d) or not os.access(d, os.W_OK):
        return None
    return Path(d)


def write_orca(cdir: Path | None = None, state: dict | None = None) -> str | None:
    """Write the view into the Orca chat folder. Returns the line that shows it inline."""
    d = orca_dir()
    if d is None:
        return None
    cdir = cdir or paths.creek_dir()
    state = state if state is not None else ledger.load_state(cdir)
    project = cdir.parent.name
    html = render(state, project)
    if len(html.encode()) > ORCA_LIMIT:
        return None
    slug = "".join(c if c.isalnum() else "-" for c in project.lower()).strip("-")[:30] or "project"
    name = f"creek-{slug}-{os.urandom(3).hex()}.html"
    (d / name).write_text(html, encoding="utf-8")
    return f'::orca-visual{{file="{name}" title="Creek · {project}"}}'


def open_file(path: Path) -> None:
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    try:
        subprocess.Popen([opener, str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        print(f"creek map: couldn't run {opener}; open {path} yourself.", file=sys.stderr)


def run(open_after: bool = False) -> int:
    cdir = paths.creek_dir()
    state = ledger.load_state(cdir)
    out = write(cdir, state)
    print(f"creek map: wrote {os.path.relpath(out)}")
    line = write_orca(cdir, state)
    if line:
        # Inside Orca: this line, put in Claude's reply, shows the view in the chat.
        print(line)
    if open_after:
        open_file(out)
    return 0
