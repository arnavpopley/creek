import os
import stat
import subprocess
import sys
import unittest

from creek import conflicts, fork, ledger, model

from helpers import ProjectCase
from test_hooks import HookCase


def rec(node, affects):
    from creek import terms

    return {"node": node, "affects_norm": terms.parse_affects(affects)["keys"]}


class OverlapTest(unittest.TestCase):
    def test_overlap_rules(self):
        self.assertEqual(conflicts.overlap(rec("n1", ["src/ContactList.tsx"]), rec("n2", ["ContactList.tsx"])), ["contactlist.tsx"])
        self.assertEqual(conflicts.overlap(rec("n1", ["src/a/X.tsx"]), rec("n2", ["src/b/X.tsx"])), [])
        # Identifiers narrow a shared file.
        self.assertEqual(conflicts.overlap(rec("n1", ["src/config/site.ts profile.about"]), rec("n2", ["src/config/site.ts statsIntro"])), [])
        self.assertEqual(conflicts.overlap(rec("n1", ["src/config/site.ts profile.about"]), rec("n2", ["site.ts profile.about"])), ["site.ts"])
        self.assertEqual(conflicts.overlap(rec("n1", ["src/config/site.ts profile.about"]), rec("n2", ["src/config/site.ts"])), ["src/config/site.ts"])


class DetectTest(ProjectCase):
    def add(self, tx, node, affects, **kw):
        from creek import actions

        return actions.add_record(tx, node, kw.pop("decision", "d"), affects=affects, **kw)

    def test_cross_node_only_and_once_per_pair(self):
        with ledger.transaction(self.cdir, "s") as tx:
            for t in ("Keys", "Help"):
                tx.emit({"type": "node.create", "title": t})
            self.add(tx, "n1", ["src/components/ContactList.tsx"])
            self.add(tx, "n1", ["ContactList.tsx"])  # same node: refinement
            self.assertEqual(tx.state["conflicts"], {})
            self.add(tx, "n2", ["ContactList.tsx"], decision="help groups keys")
            st = tx.state
        self.assertEqual({k: c["records"] for k, c in st["conflicts"].items()}, {"c1": ["r1", "r3"], "c2": ["r2", "r3"]})
        notes = st["pending_notes"]["s"]
        self.assertIn("Creek: r3 overlaps r1 on ContactList.tsx — check they agree (c1).", notes)

    def test_supersedes_and_inactive_skipped(self):
        with ledger.transaction(self.cdir, "s") as tx:
            for t in ("A", "B"):
                tx.emit({"type": "node.create", "title": t})
            self.add(tx, "n1", ["x.ts"])
            self.add(tx, "n2", ["x.ts"], supersedes=["r1"])
            st = tx.state
        self.assertEqual(st["conflicts"], {})
        self.assertFalse(st["records"]["r1"]["active"])


class ForkTest(HookCase):
    def fake_claude(self, supports=True):
        bindir = self.tmp / "bin"
        bindir.mkdir(exist_ok=True)
        exe = bindir / "claude"
        flag = "  --fork-session   When resuming, create a new session ID" if supports else "  --resume"
        exe.write_text(f"#!/bin/sh\necho 'Usage: claude'\necho '{flag}'\n")
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
        return {"PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"}

    def creek(self, *args, env=None):
        return subprocess.run([sys.executable, "-m", "creek", *args], capture_output=True, text=True, env={**os.environ, **(env or {})}, cwd=self.tmp)

    def test_fork_round_trip(self):
        main = self.session
        self.hook("session-start", source="startup")
        self.hook("stop", last_assistant_message="creek: node Custom pointer\ncreek: node Clock\ncreek: on 2")
        env = self.fake_claude()
        out = self.creek("fork", "9", env=env)
        self.assertEqual(out.returncode, 1)
        self.assertIn("no node '9'", out.stderr)
        out = self.creek("fork", "1", env=env)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn(f"CREEK_BRANCH=b1 CREEK_NODE=n1 claude --resume {main} --fork-session", out.stdout)
        st = self.state()
        self.assertEqual(st["branches"]["b1"]["kind"], "fork")
        self.assertEqual(st["branches"]["b1"]["parent_session"], main)

        # The fork: a new session id, with the env creek fork set.
        fenv = {"CREEK_BRANCH": "b1", "CREEK_NODE": "n1"}
        self.session = "sess-fork"
        ctx = self.context(self.hook("session-start", env=fenv, source="fork"))
        self.assertIn("You are in branch b1 on n1 (Custom pointer). Resolve here, then the user runs ::merge.", ctx)
        self.hook("post-tool-use", env=fenv, tool_name="Edit", tool_input={"file_path": str(self.tmp / "src/Crosshair.tsx")}, tool_response={})
        self.hook("stop", env=fenv, last_assistant_message="creek: resolve 1 | decision: clamp the corner tags | rule: tags stay inside the viewport | affects: src/Crosshair.tsx")
        st = self.state()
        self.assertEqual(st["records"]["r1"]["branch"], "b1")
        self.assertEqual(st["branches"]["b1"]["status"], "open")  # forks wait for ::merge
        self.assertEqual(st["branches"]["b1"]["edits"], {"src/Crosshair.tsx": 1})
        self.assertEqual(st["sessions"]["sess-fork"]["branch"], "b1")
        out = self.hook("user-prompt-submit", env=fenv, prompt="::merge")
        self.assertIn("merged b1 into n1 with r1", out["reason"])
        self.assertIn("close this session", out["reason"])

        # Back in main: exactly one note line about the merge.
        self.session = main
        ctx = self.context(self.hook("user-prompt-submit", prompt="what's next?"))
        lines = [line for line in ctx.splitlines() if "b1 merged into n1" in line]
        self.assertEqual(lines, ["b1 merged into n1: clamp the corner tags. Rule: tags stay inside the viewport"])
        self.assertEqual((self.state()["focus"][main]), {"node": "n2", "branch": None})
        # creek fork picks main, not the fork, next time.
        self.assertEqual(fork.latest_session(self.state()), main)

    def test_fork_refuses_without_fork_session(self):
        self.hook("stop", last_assistant_message="creek: node Pointer")
        out = self.creek("fork", "1", env=self.fake_claude(supports=False))
        self.assertEqual(out.returncode, 1)
        self.assertIn("no --fork-session", out.stderr)
        self.assertEqual(self.state()["branches"], {})

    def test_fork_with_no_sessions(self):
        with ledger.transaction(self.cdir, "cli") as tx:
            tx.emit({"type": "node.create", "title": "x"})
        out = self.creek("fork", "1", env=self.fake_claude())
        self.assertEqual(out.returncode, 1)
        self.assertIn("nothing to fork", out.stderr)


class CompactTest(HookCase):
    def test_compact_reinjects_state_with_decisions(self):
        self.hook("stop", last_assistant_message=(
            "creek: node Help panel\ncreek: node Keys\n"
            "creek: resolve 1 | decision: native dialog | rule: overlays check for an open dialog | affects: HelpPanel.tsx\n"
            "creek: resolve 2 | decision: number keys open contact links | affects: ContactList.tsx\n"
            "creek: ask Which key should 'more' use?"
        ))
        self.hook("pre-compact", trigger="manual", custom_instructions=None)
        st = self.state()
        self.assertEqual(st["compacts"], 1)
        ctx = self.context(self.hook("session-start", source="compact"))
        self.assertIn("Creek state:", ctx)
        self.assertIn("r1 [HelpPanel.tsx]: overlays check for an open dialog", ctx)
        self.assertIn("r2 (n2): number keys open contact links", ctx)  # decisions only after compaction
        self.assertIn("q1 (asked 1×)", ctx)
        injects = [e for e in ledger.read_events(self.cdir)[0] if e["type"] == "inject"]
        self.assertEqual(injects[-1]["source"], "compact")


if __name__ == "__main__":
    unittest.main()


class SiblingPointsTest(ProjectCase):
    def test_points_of_one_brief_sharing_a_file_are_not_flagged(self):
        from creek import actions

        with ledger.transaction(self.cdir, "s") as tx:
            for t in ("a", "b"):
                tx.emit({"type": "node.create", "title": t, "origin": "brief", "brief_id": "p1"})
            tx.emit({"type": "node.create", "title": "later", "origin": "model"})
            actions.add_record(tx, "n1", "done command", affects=["todo.py"])
            actions.add_record(tx, "n2", "tick marks", affects=["todo.py"])
            self.assertEqual(tx.state["conflicts"], {})
            actions.add_record(tx, "n3", "rewrite storage", affects=["todo.py"])
            self.assertEqual(len(tx.state["conflicts"]), 2)  # a point from outside the brief still flags
