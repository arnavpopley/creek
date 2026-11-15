import json
import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from creek import ledger, mapview, paths, replay
from creek.synth import big_project

from helpers import ProjectCase

FIXTURES = Path(__file__).parent / "fixtures"
CHROME_CANDIDATES = [
    os.environ.get("CREEK_CHROME", ""),
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    shutil.which("google-chrome") or "",
    shutil.which("chromium") or "",
    shutil.which("chromium-browser") or "",
]
CHROME = next((c for c in CHROME_CANDIDATES if c and os.path.exists(c)), None)


def dump_dom(path: Path, fragment: str = "", profile: Path | None = None) -> str:
    cmd = [CHROME, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check"]
    if profile:
        cmd.append(f"--user-data-dir={profile}")
    cmd += ["--dump-dom", path.as_uri() + fragment]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60).stdout


class MapStaticTest(ProjectCase):
    def setUp(self):
        super().setUp()
        replay.run(replay.load(FIXTURES / "website-session.json"), self.cdir, replay.load(FIXTURES / "website-annotations.json"))
        self.state = ledger.load_state(self.cdir)
        self.out = mapview.write(self.cdir, self.state)
        self.html = self.out.read_text()

    def test_single_file_no_external_requests(self):
        self.assertFalse(re.search(r"<(script|link|img|iframe)[^>]+(src|href)=", self.html))
        self.assertNotIn("@import", self.html)
        self.assertNotIn("url(", self.html)

    def test_inlined_state_parses_and_is_inert(self):
        m = re.search(r'<script type="application/json" id="creek-data">(.*?)</script>', self.html, re.S)
        data = json.loads(m.group(1))
        self.assertEqual(len(data["state"]["nodes"]), 19)
        self.assertNotIn("index", data["state"])
        self.assertNotIn("</", m.group(1))

    def test_three_panes(self):
        for pane in ('id="lanes-pane"', 'id="needs-pane"', 'id="ledger-pane"'):
            self.assertIn(pane, self.html)


@unittest.skipUnless(CHROME, "needs Chrome or Chromium for a headless render")
class MapBrowserTest(ProjectCase):
    def test_website_fixture_panes_and_search(self):
        rep = replay.run(replay.load(FIXTURES / "website-session.json"), self.cdir, replay.load(FIXTURES / "website-annotations.json"))
        state = ledger.load_state(self.cdir)
        out = mapview.write(self.cdir, state)
        dom = dump_dom(out, "#q=ContactList.tsx", self.tmp / "chrome")
        # Lanes: every node and branch rendered.
        self.assertEqual(len(re.findall(r'class="node ', dom)), 19)
        self.assertEqual(len(re.findall(r'data-branch="b\d+"', dom)), 21)
        # Needs you: three questions, three conflicts, the tabled pointer, each with its command.
        for cmd in ("::answer q1 ", "::answer q2 ", "::answer q3 ", "::ok c1", "::branch 9"):
            self.assertIn(f'data-cmd="{cmd}"', dom)
        # Ledger search by file finds exactly the active records that affect it.
        want = {rid for rid, r in state["records"].items() if r["active"] and any(f.endswith("contactlist.tsx") for f in r["files"])}
        got = set(re.search(r'data-matches="([^"]*)"', dom).group(1).split())
        self.assertEqual(got, want)
        # b5's record was superseded by b7's, so the active matches are b6 and b7.
        self.assertEqual(got, {rep["record_of"]["b6"], rep["record_of"]["b7"]})

    def test_300_nodes_open_under_a_second(self):
        events = big_project(300)
        with open(paths.events_path(self.cdir), "w") as f:
            for ev in events:
                f.write(json.dumps(ev) + "\n")
        state = ledger.load_state(self.cdir)
        self.assertEqual(len(state["nodes"]), 300)
        out = mapview.write(self.cdir, state)
        dom = dump_dom(out, "", self.tmp / "chrome")
        load_ms = float(re.search(r'data-load-ms="([\d.]+)"', dom).group(1))
        render_ms = float(re.search(r'data-render-ms="([\d.]+)"', dom).group(1))
        print(f"\n  300-node map: rendered in {render_ms:.0f} ms, {load_ms:.0f} ms from navigation start", flush=True)
        self.assertLess(load_ms, 1000)
        self.assertEqual(len(re.findall(r'class="node ', dom)), 300)


if __name__ == "__main__":
    unittest.main()
