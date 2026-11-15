import json
import re
import os
import subprocess
import sys
import time
import unittest

from creek import install, ledger, paths, statusline

from helpers import ProjectCase
from test_hooks import HookCase


class CommandTest(HookCase):
    def block(self, prompt):
        out = self.hook("user-prompt-submit", prompt=prompt)
        self.assertEqual(out["decision"], "block", out)
        return out["reason"]

    def seed(self):
        self.hook("stop", last_assistant_message="creek: node Keys\ncreek: on 1\ncreek: ask Which key should 'more' use?")

    def test_answer_records_and_notes_next_turn(self):
        self.seed()
        reason = self.block("::answer q1 o")
        self.assertIn("q1 answered (o)", reason)
        st = self.state()
        self.assertEqual(st["questions"]["q1"]["status"], "answered")
        rid = st["questions"]["q1"]["record"]
        self.assertEqual(st["records"][rid]["node"], "n1")
        self.assertEqual(st["records"][rid]["question"], "q1")
        self.assertEqual(st["turn"].get(self.session, 0), 0)  # a command is not a model turn
        ctx = self.context(self.hook("user-prompt-submit", prompt="carry on"))
        self.assertIn("The user answered q1", ctx)
        self.assertIn(": o", ctx)
        # Notes go once.
        self.assertIsNone(self.hook("user-prompt-submit", prompt="and again"))

    def test_answer_errors(self):
        self.seed()
        self.assertIn("no question q9", self.block("::answer q9 x"))
        self.assertIn("what's the answer", self.block("::answer q1"))
        self.block("::answer q1 o")
        self.assertIn("already answered", self.block("::answer q1 m"))

    def test_status_and_help_and_unknown(self):
        self.seed()
        reason = self.block("::status")
        self.assertIn("n1", reason)
        self.assertIn("::answer q1 <answer>", reason)
        self.assertIn("::branch 13", self.block("::help"))
        self.assertIn("unknown command ::frob", self.block("::frob"))

    def test_branch_merge(self):
        self.hook("stop", last_assistant_message="creek: node Pointer\ncreek: resolve 1 | decision: built the crosshair | affects: Crosshair.tsx")
        self.assertIn("opened b1 on n1", self.block("::branch 1"))
        self.assertIn("no record yet", self.block("::merge"))
        # A resolve on n1 merges an inline branch on its own.
        self.hook("stop", last_assistant_message="creek: resolve 1 | decision: clamp the tags")
        st = self.state()
        self.assertEqual(st["branches"]["b1"]["status"], "merged")
        self.assertIn("no open branch", self.block("::merge"))
        self.block("::branch n1")
        self.hook("stop", last_assistant_message="creek: resolve | decision: cache text per frame")
        self.assertEqual(self.state()["branches"]["b2"]["status"], "merged")

    def test_table_drop_node(self):
        self.hook("stop", last_assistant_message="creek: node Pointer\ncreek: node Clock")
        self.block("::branch 1")
        self.assertIn("tabled: hides the real information", self.block("::table 1 hides the real information"))
        st = self.state()
        self.assertEqual(st["nodes"]["n1"]["status"], "tabled")
        self.assertEqual(st["branches"]["b1"]["status"], "tabled")
        self.block("::drop 2 not needed")
        self.assertEqual(self.state()["nodes"]["n2"]["status"], "abandoned")
        self.assertIn("added n3", self.block("::node Year 1 project entry"))

    def test_conflict_ok_and_supersede(self):
        self.hook("stop", last_assistant_message=(
            "creek: node Keys\ncreek: node Help\n"
            "creek: resolve 1 | decision: ignore repeats | affects: src/ContactList.tsx\n"
            "creek: resolve 2 | decision: grouped help | affects: ContactList.tsx\n"
            "creek: resolve 2 | decision: help layout | affects: ContactList.tsx"
        ))
        st = self.state()
        self.assertEqual({k: c["records"] for k, c in st["conflicts"].items()}, {"c1": ["r1", "r2"], "c2": ["r1", "r3"]})
        self.assertIn("both stand", self.block("::ok c1"))
        self.assertIn("r1 is superseded by r3", self.block("::supersede c2"))
        st = self.state()
        self.assertFalse(st["records"]["r1"]["active"])
        self.assertEqual(st["conflicts"]["c2"]["status"], "superseded")

    def test_pull_with_source_excerpt(self):
        tpath = self.tmp / "transcript.jsonl"
        reply = "Long reasoning about the panel and focus. " * 10 + "\ncreek: node Help\ncreek: resolve 1 | decision: native dialog with showModal | affects: HelpPanel.tsx"
        tpath.write_text(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": reply}]}}) + "\n")
        self.hook("stop", last_assistant_message=reply, transcript_path=str(tpath))
        self.assertIn("with its source excerpt", self.block("::pull r1"))
        ctx = self.context(self.hook("user-prompt-submit", prompt="ok"))
        self.assertIn("decision: native dialog with showModal", ctx)
        self.assertIn("Long reasoning about the panel", ctx)
        self.assertNotIn("creek: resolve", ctx)


class BriefAndQuestionsTest(HookCase):
    def test_twelve_item_brief_makes_twelve_nodes(self):
        prompt = "Website changes:\n" + "\n".join(f"{i}. change number {i}\n   - detail" for i in range(1, 13))
        ctx = self.context(self.hook("user-prompt-submit", prompt=prompt))
        self.assertEqual(ctx, "Creek: created n1–n12 from this brief, in order.")
        st = self.state()
        self.assertEqual(len(st["nodes"]), 12)
        self.assertEqual(st["nodes"]["n12"]["title"], "change number 12")
        self.assertEqual({n["origin"] for n in st["nodes"].values()}, {"brief"})
        # Pasting the same brief again doesn't duplicate it.
        self.hook("user-prompt-submit", prompt=prompt)
        self.assertEqual(len(self.state()["nodes"]), 12)

    def test_same_question_six_times(self):
        variants = [
            "Which key should 'more' use?",
            "Which key should 'more' use now?",
            "which key should more use",
            "Which key should 'more' use? (m is taken)",
            "Which key should \"more\" use?",
            "Which key should 'more' use?",
        ]
        for i, q in enumerate(variants):
            self.hook("stop", last_assistant_message=f"Done with step {i}.\ncreek: ask {q}")
        st = self.state()
        self.assertEqual(len(st["questions"]), 1)
        self.assertEqual(st["questions"]["q1"]["asks"], 6)
        # The model is told once that it is queued.
        notes = st["pending_notes"][self.session]
        self.assertEqual(sum("queued q1" in n for n in notes), 1)
        self.assertIn("1 you (q1 ×6)", statusline.render(st))

    def test_different_question_not_merged(self):
        self.hook("stop", last_assistant_message="creek: ask Which key should 'more' use?")
        self.hook("stop", last_assistant_message="creek: ask Is the year 1 project the transmission design?")
        self.assertEqual(len(self.state()["questions"]), 2)


class StatusLineTest(ProjectCase):
    def test_render(self):
        self.assertEqual(statusline.render(None), "creek · clear")
        with ledger.transaction(self.cdir, "s") as tx:
            for t in ("a", "b", "c", "d", "e"):
                tx.emit({"type": "node.create", "title": t})
            tx.emit({"type": "node.status", "node": "n5", "status": "resolved"})
            tx.emit({"type": "question.ask", "text": "Which key should 'more' use?"})
            for _ in range(5):
                tx.emit({"type": "question.ask", "text": "Which key should 'more' use?"})
            tx.emit({"type": "record.add", "node": "n1", "decision": "x", "affects": ["ContactList.tsx"]})
            tx.emit({"type": "record.add", "node": "n2", "decision": "y", "affects": ["ContactList.tsx"]})
            tx.emit({"type": "conflict.flag", "records": ["r1", "r2"], "overlap": ["contactlist.tsx"]})
            tx.emit({"type": "node.flag", "node": "n3", "flag": "unrecorded"})
            st = tx.state
        line = statusline.render(st)
        self.assertEqual(line, "creek · 2 open · 1 you (q1 ×6) · 1 conflict · 1 unrecorded")
        self.assertLessEqual(len(line), 80)

    def test_clear_when_all_zero(self):
        with ledger.transaction(self.cdir, "s") as tx:
            tx.emit({"type": "node.create", "title": "a"})
            tx.emit({"type": "record.add", "node": "n1", "decision": "x"})
            st = tx.state
        self.assertEqual(statusline.render(st), "creek · clear")

    def test_command_fast_and_wraps_existing(self):
        with ledger.transaction(self.cdir, "s") as tx:
            tx.emit({"type": "node.create", "title": "a"})
        payload = json.dumps({"session_id": "s", "workspace": {"current_dir": str(self.tmp)}}).encode()
        exe = [sys.executable, "-m", "creek", "statusline"]

        def p95(cmd):
            times = []
            for _ in range(20):
                t0 = time.perf_counter()
                out = subprocess.run(cmd, input=payload, capture_output=True, check=True).stdout.decode()
                times.append((time.perf_counter() - t0) * 1000)
            return sorted(times)[18], out

        base, _ = p95([sys.executable, "-c", "pass"])
        took, out = p95(exe)
        self.assertEqual(out, "creek · 1 open\n")
        print(f"\n  statusline p95 {took:.1f} ms (bare interpreter start {base:.1f} ms)", file=sys.stderr)
        # Interpreter start depends on the install (an editable install adds ~10 ms);
        # Creek's own share has to stay small for the 30 ms target.
        self.assertLess(took - base, 15)
        out = subprocess.run([*exe, "--then", "echo existing-line"], input=payload, capture_output=True, check=True).stdout.decode()
        self.assertEqual(out, "existing-line\ncreek · 1 open\n")


class InstallTest(ProjectCase):
    def setUp(self):
        super().setUp()
        self.cfg = self.tmp / "claude-config"
        self.cfg.mkdir()
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.cfg)
        self.sp = self.cfg / "settings.json"

    def tearDown(self):
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        super().tearDown()

    def test_fresh_install(self):
        install.install()
        self.assertEqual(json.loads(self.sp.read_text())["statusLine"], {"type": "command", "command": "creek statusline"})

    def test_wraps_existing_and_backs_up(self):
        self.sp.write_text(json.dumps({"model": "opus", "statusLine": {"type": "command", "command": "bash ~/my line.sh", "padding": 0}}))
        install.install()
        s = json.loads(self.sp.read_text())
        self.assertEqual(s["statusLine"]["command"], "creek statusline --then 'bash ~/my line.sh'")
        self.assertEqual(s["statusLine"]["padding"], 0)
        self.assertEqual(s["model"], "opus")
        backups = list(self.cfg.glob("settings.json.creek-backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertIn("my line.sh", backups[0].read_text())
        install.install()  # idempotent
        self.assertEqual(json.loads(self.sp.read_text())["statusLine"]["command"], "creek statusline --then 'bash ~/my line.sh'")
        install.install(remove=True)
        self.assertEqual(json.loads(self.sp.read_text())["statusLine"]["command"], "bash ~/my line.sh")

    def test_invalid_json_untouched(self):
        self.sp.write_text("{oops")
        self.assertEqual(install.install(), 1)
        self.assertEqual(self.sp.read_text(), "{oops")


if __name__ == "__main__":
    unittest.main()


class MapCommandTest(HookCase):
    def test_map_inside_orca_shows_inline(self):
        scratch = self.tmp / "orca-scratch"
        scratch.mkdir()
        self.hook("stop", last_assistant_message="creek: node Header\ncreek: resolve 1 | decision: sticky header | affects: style.css")
        out = self.hook("user-prompt-submit", env={"ORCA_CHAT_SCRATCH_DIR": str(scratch)}, prompt="::map")
        ctx = self.context(out)
        m = re.search(r'::orca-visual\{file="(creek-[\w.-]+\.html)" title="Creek · [^"]+"\}', ctx)
        self.assertIsNotNone(m, ctx)
        page = (scratch / m.group(1)).read_text()
        self.assertIn("sticky header", page)
        self.assertLess(len(page.encode()), 512_000)

    def test_map_outside_orca_writes_file_and_blocks(self):
        env = {"ORCA_CHAT_SCRATCH_DIR": ""}
        out = self.hook("user-prompt-submit", env=env, prompt="::map")
        self.assertEqual(out["decision"], "block")
        self.assertIn("map.html", out["reason"])
        self.assertTrue((self.cdir / "map.html").exists())

    def test_question_files_under_matching_point(self):
        self.hook("user-prompt-submit", prompt="1. Make the header sticky\n2. Add keyboard shortcuts\n3. Pick an accent colour for links")
        self.hook("stop", last_assistant_message="creek: ask What accent colour should links use?")
        st = self.state()
        self.assertEqual(st["questions"]["q1"]["node"], "n3")
        out = self.hook("user-prompt-submit", prompt="::answer q1 teal")
        self.assertIn("on n3", out["reason"])
        st = self.state()
        self.assertEqual(len(st["nodes"]), 3)
        self.assertEqual(st["records"]["r1"]["decision"], "teal (your answer to: What accent colour should links use?)")
