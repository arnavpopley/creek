import json
import os
import stat
import subprocess
import sys
import unittest
from pathlib import Path

from creek import cost, ledger, paths, replay

from helpers import ProjectCase

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE = FIXTURES / "website-session.json"
ANNOTATIONS = FIXTURES / "website-annotations.json"


def creek(*args, cwd=None, env=None):
    return subprocess.run([sys.executable, "-m", "creek", *args], capture_output=True, text=True, cwd=cwd, env={**os.environ, **(env or {})})


class ReplayTest(ProjectCase):
    def test_reproduces_nodes_branches_and_open_questions(self):
        for use_markers in (True, False):
            with self.subTest(markers=use_markers):
                root = self.tmp / f"r{use_markers}"
                (root / ".git").mkdir(parents=True)
                rep = replay.run(replay.load(FIXTURE), paths.creek_dir(root), replay.load(ANNOTATIONS), use_markers=use_markers)
                st = ledger.load_state(paths.creek_dir(root))
                self.assertEqual(rep["nodes"], 19)
                self.assertEqual(rep["branches"], 21)
                self.assertEqual(sorted(st["nodes"], key=lambda k: int(k[1:])), [f"n{i}" for i in range(1, 20)])
                self.assertEqual(sorted(st["branches"], key=lambda k: int(k[1:])), [f"b{i}" for i in range(1, 22)])
                # Branches that never settled become the open questions.
                open_q = {q["text"] for q in st["questions"].values() if q["status"] == "open"}
                self.assertEqual(len(open_q), 3)
                self.assertTrue(any("'more'" in q for q in open_q), open_q)
                self.assertTrue(any("year 1" in q for q in open_q), open_q)
                self.assertEqual(st["nodes"]["n9"]["status"], "tabled")
                self.assertEqual(st["nodes"]["n18"]["status"], "open")
                self.assertEqual(st["errors"], 0)

    def test_more_key_asked_every_reply_counts_once(self):
        rep = replay.run(replay.load(FIXTURE), self.cdir, replay.load(ANNOTATIONS))
        q = rep["questions"][rep["question_of"]["b13"]]
        self.assertEqual(q["asks"], 7)  # msg 9 plus each reply to 15

    def test_cli(self):
        out = creek("replay", str(FIXTURE), "--annotations", str(ANNOTATIONS), "--into", str(self.tmp / "proj"))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("nodes 19 · branches 21", out.stdout)
        self.assertIn("Lost details recovered: 6 of 8", out.stdout)
        again = creek("replay", str(FIXTURE), "--into", str(self.tmp / "proj"))
        self.assertEqual(again.returncode, 1)
        self.assertIn("already has a ledger", again.stderr)
        js = json.loads(creek("replay", str(FIXTURE), "--json").stdout)
        self.assertEqual(js["nodes"], 19)


class CostTest(ProjectCase):
    def test_per_hook_and_transcript(self):
        t = self.tmp / "t.jsonl"
        lines = [
            {"type": "assistant", "message": {"id": "m1", "usage": {"input_tokens": 100, "cache_creation_input_tokens": 900, "cache_read_input_tokens": 5000, "output_tokens": 50},
                                               "content": [{"type": "text", "text": "Done.\ncreek: resolve 1 | decision: x"}]}},
            {"type": "assistant", "message": {"id": "m1", "usage": {"input_tokens": 100, "cache_creation_input_tokens": 900, "cache_read_input_tokens": 5000, "output_tokens": 50},
                                               "content": [{"type": "tool_use", "name": "Edit"}]}},
            {"type": "user", "message": {"content": "hi"}},
        ]
        t.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
        with ledger.transaction(self.cdir, "s1") as tx:
            tx.emit({"type": "inject", "hook": "session-start", "records": [], "chars": 800, "transcript": str(t)})
            tx.emit({"type": "inject", "hook": "pre-tool-use", "records": ["r1"], "chars": 200})
            tx.emit({"type": "inject", "hook": "pre-tool-use", "records": ["r2"], "chars": 120})
        data = cost.injected(self.cdir)
        self.assertEqual(data["per"]["s1"]["pre-tool-use"], {"count": 2, "chars": 320})
        u = cost.transcript_usage(str(t))
        self.assertEqual((u["requests"], u["input"], u["cache_write"], u["output"], u["marker_lines"]), (1, 100, 900, 50, 1))
        out = creek("cost", cwd=self.tmp).stdout
        self.assertIn("pre-tool-use", out)
        self.assertRegex(out, r"session-start\s+1\s+800\s+200")
        self.assertIn("Creek's share of new input ≈ 28.0%", out)


class SyncTest(ProjectCase):
    def fake_gh(self):
        bindir = self.tmp / "bin"
        bindir.mkdir()
        gh = bindir / "gh"
        gh.write_text("""#!/bin/sh
if [ "$1 $2" = "pr list" ]; then
  echo '[{"number":42,"state":"OPEN","isDraft":true,"mergedAt":null,"title":"Crosshair"},{"number":43,"state":"MERGED","isDraft":false,"mergedAt":"2026-11-14T10:00:00Z","title":"When field"},{"number":44,"state":"CLOSED","isDraft":false,"mergedAt":null,"title":"x"}]'
elif [ "$1 $2" = "pr view" ]; then
  echo '{"comments":[{"body":"Greptile Summary ... Confidence Score: 3/5"},{"body":"<h3>Greptile</h3> Confidence score: 4/5"}],"reviews":[]}'
fi
""")
        gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
        return {"PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"}

    def test_sync_events_and_scores(self):
        with ledger.transaction(self.cdir, "s") as tx:
            tx.emit({"type": "node.create", "title": "Pointer"})
            tx.emit({"type": "pr", "number": 42, "action": "opened", "node": "n1"})
        env = self.fake_gh()
        out = creek("sync", cwd=self.tmp, env=env)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("3 PRs checked; 3 changed; 1 review scores", out.stdout)
        st = ledger.load_state(self.cdir)
        self.assertEqual({n: p["action"] for n, p in st["prs"].items()}, {"42": "draft", "43": "merged", "44": "closed"})
        self.assertEqual((st["prs"]["42"]["score"], st["prs"]["42"]["reviewer"]), (4, "greptile"))
        self.assertEqual(st["prs"]["42"]["node"], "n1")
        out = creek("sync", cwd=self.tmp, env=env)
        self.assertIn("0 changed", out.stdout)

    def test_sync_without_gh(self):
        out = creek("sync", cwd=self.tmp, env={"PATH": "/usr/bin:/bin"})
        self.assertEqual(out.returncode, 1)
        self.assertIn("gh", out.stderr)


if __name__ == "__main__":
    unittest.main()
