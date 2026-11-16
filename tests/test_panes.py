"""Sub-chat panes, with a stand-in `orca` CLI that records what Creek asks of it."""

import json
import os
import stat
import sys
import time
from pathlib import Path

from creek import ledger, panes

from test_hooks import HookCase

FAKE_ORCA = """#!/bin/sh
echo "$@" >> "$FAKE_ORCA_LOG"
case "$2" in
  split) n=$(wc -l < "$FAKE_ORCA_LOG" | tr -d ' '); echo '{"ok":true,"result":{"split":{"handle":"term_new'$n'"}}}' ;;
  *) echo '{"ok":true,"result":{}}' ;;
esac
"""


class PanesTest(HookCase):
    def setUp(self):
        super().setUp()
        bindir = self.tmp / "orcabin"
        bindir.mkdir()
        exe = bindir / "orca"
        exe.write_text(FAKE_ORCA)
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
        self.log = self.tmp / "orca.log"
        self.env = {"ORCA_CLI_BIN_DIR": str(bindir), "ORCA_TERMINAL_HANDLE": "term_main", "FAKE_ORCA_LOG": str(self.log), "CREEK_CLAUDE": "/bin/echo"}
        os.environ.update(self.env)

    def tearDown(self):
        for k in self.env:
            os.environ.pop(k, None)
        super().tearDown()

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_brief_in_an_orca_terminal_opens_one_pane_per_point(self):
        ctx = self.context(self.hook("user-prompt-submit", env=self.env, prompt="Things:\n1. Add done\n2. Show ticks\n3. Pick a name, ask me", permission_mode="acceptEdits"))
        self.assertIn("opened a pane for each point (n1–n3)", ctx)
        self.assertIn("Don't work on the points in this chat", ctx)
        for _ in range(100):  # the panes open from a detached process
            if len([c for c in self.calls() if c.startswith("terminal split")]) == 3 and any("Main" in c for c in self.calls()):
                break
            time.sleep(0.1)
        splits = [c for c in self.calls() if c.startswith("terminal split")]
        self.assertEqual(len(splits), 3)
        self.assertIn("--terminal term_main --direction horizontal", splits[0])  # first beside main
        self.assertIn("--direction vertical", splits[1])  # the rest stack
        self.assertIn("--terminal term_new", splits[1])
        self.assertIn("CREEK_BRANCH=b1 CREEK_NODE=n1 CREEK_PANE=orca", splits[0])
        self.assertIn("--resume sess-main --fork-session --permission-mode acceptEdits", splits[0])
        titles = [c for c in self.calls() if c.startswith("terminal set-pane-title")]
        self.assertTrue(any("① Add done" in t for t in titles))
        st = self.state()
        self.assertEqual(sorted(st["panes"]), ["n1", "n2", "n3"])
        self.assertEqual({b["kind"] for b in st["branches"].values()}, {"fork"})

    def test_a_pane_that_settles_merges_and_closes(self):
        self.hook("user-prompt-submit", prompt="1. a\n2. b\n3. c")  # no orca env: no panes
        with ledger.transaction(self.cdir, "cli") as tx:
            tx.emit({"type": "branch.open", "node": "n2", "kind": "fork", "parent_session": self.session})
        pane_env = {**self.env, "CREEK_BRANCH": "b1", "CREEK_NODE": "n2", "CREEK_PANE": "orca", "ORCA_TERMINAL_HANDLE": "term_p2"}
        self.session = "sess-pane"
        self.hook("session-start", env=pane_env, source="fork")
        self.hook("stop", env=pane_env, last_assistant_message="Still working on it.")
        self.assertEqual(self.state()["branches"]["b1"]["status"], "open")
        self.hook("stop", env=pane_env, last_assistant_message="Done.\ncreek: resolve 2 | decision: ticks shown | affects: todo.py")
        st = self.state()
        self.assertEqual(st["branches"]["b1"]["status"], "merged")
        self.assertIn("b1 merged into n2: ticks shown", st["pending_notes"]["sess-main"])
        self.assertTrue(any("set-pane-title --terminal term_p2 --title ✓ ②" in c for c in self.calls()))
        # The pane closes a few seconds later, from a detached shell.
        self.assertEqual(panes.CLOSE_AFTER, 8)

    def test_no_panes_outside_a_terminal(self):
        for k in ("ORCA_TERMINAL_HANDLE",):
            os.environ.pop(k, None)
        os.environ.pop("TMUX", None)
        self.assertIsNone(panes.host())
        ctx = self.context(self.hook("user-prompt-submit", env={"ORCA_TERMINAL_HANDLE": ""}, prompt="1. a\n2. b\n3. c"))
        self.assertNotIn("opened a pane", ctx)

    def test_labels(self):
        self.assertEqual(panes.label("n3", "Pick a proper name"), "③ Pick a proper name")
        self.assertIn("Work only on point 2: Ticks", panes.first_prompt(2, "Ticks", "1. a\n2. Ticks"))
