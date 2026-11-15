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
        r = {k: v for k, v in r.items() if k != "source_msg"}
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


def open_file(path: Path) -> None:
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    try:
        subprocess.Popen([opener, str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        print(f"creek map: couldn't run {opener}; open {path} yourself.", file=sys.stderr)


def run(open_after: bool = False) -> int:
    out = write()
    print(f"creek map: wrote {os.path.relpath(out)}")
    if open_after:
        open_file(out)
    return 0
