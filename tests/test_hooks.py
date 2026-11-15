"""End-to-end hook behaviour through `creek hook <event>` in a subprocess."""

import json
import os
import subprocess
import sys
import unittest

from creek import ledger, paths

from helpers import ProjectCase


class HookCase(ProjectCase):
    session = "sess-main"

    def hook(self, event, env=None, **payload):
        body = {"session_id": self.session, "cwd": str(self.tmp), "transcript_path": "", **payload}
        proc = subprocess.run(
            [sys.executable, "-m", "creek", "hook", event],
            input=json.dumps(body).encode(),
            capture_output=True,
            env={**os.environ, **(env or {})},
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        err = paths.errors_path(self.cdir)
        if err.exists():
            self.fail("hook logged an error:\n" + err.read_text())
        return json.loads(proc.stdout) if proc.stdout.strip() else None

    def state(self):
        return ledger.load_state(self.cdir)

    def context(self, out):
        return out["hookSpecificOutput"]["additionalContext"]


class SessionStartStopTest(HookCase):
    def test_protocol_on_empty_ledger(self):
        out = self.hook("session-start", source="startup")
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertTrue(self.context(out).startswith("Creek is tracking this session."))

    def test_stop_records_markers_and_session_start_shows_them(self):
        reply = (
            "Rebuilt the panel.\n\n"
            "creek: node Help panel\n"
            "creek: on 1\n"
            "creek: resolve 1 | decision: native <dialog> with showModal() | rule: panel is in the top layer; overlays must check for an open dialog | affects: src/components/HelpPanel.tsx, PR #30 | tags: dialog, overlay\n"
            "creek: ask Which key should 'more' use?"
        )
        self.assertIsNone(self.hook("stop", last_assistant_message=reply, stop_hook_active=False))
        st = self.state()
        self.assertEqual(st["nodes"]["n1"]["title"], "Help panel")
        self.assertEqual(st["nodes"]["n1"]["status"], "resolved")
        r = st["records"]["r1"]
        self.assertEqual(r["rule"], "panel is in the top layer; overlays must check for an open dialog")
        self.assertEqual(r["prs"], [30])
        self.assertIn("dialog", r["terms"])
        self.assertIn("helppanel.tsx", r["terms"])
        self.assertEqual(st["questions"]["q1"]["asks"], 1)

        ctx = self.context(self.hook("session-start", source="resume"))
        self.assertIn("r1 [HelpPanel.tsx]: panel is in the top layer", ctx)
        self.assertIn("q1 (asked 1×): Which key should 'more' use? — queued, don't re-ask", ctx)

    def test_resolve_defaults_to_focus_then_creates_node(self):
        self.hook("stop", last_assistant_message="creek: resolve | decision: round the dial coordinates to 3 places | affects: DaylightClock.tsx")
        st = self.state()
        self.assertEqual(st["nodes"]["n1"]["title"], "round the dial coordinates to 3 places"[:60])
        self.hook("stop", last_assistant_message="creek: node Ticker\ncreek: on 2\ncreek: resolve | decision: flash digits green")
        st = self.state()
        self.assertEqual(st["records"]["r2"]["node"], "n2")

    def test_on_settled_node_opens_inline_branch_and_resolve_merges_it(self):
        self.hook("stop", last_assistant_message="creek: node Keys\ncreek: resolve 1 | decision: number keys open links | affects: ContactList.tsx")
        self.hook("stop", last_assistant_message="creek: on 1")
        st = self.state()
        self.assertEqual(st["focus"][self.session], {"node": "n1", "branch": "b1"})
        self.assertEqual(st["branches"]["b1"]["status"], "open")
        self.hook("stop", last_assistant_message="creek: resolve | decision: ignore repeats | affects: ContactList.tsx")
        st = self.state()
        self.assertEqual(st["branches"]["b1"]["status"], "merged")
        self.assertEqual(st["branches"]["b1"]["record"], "r2")
        self.assertEqual(st["records"]["r2"]["branch"], "b1")

    def test_malformed_marker_logs_error_event(self):
        self.hook("stop", last_assistant_message="creek: resolve 1 | rule: nothing settled\ncreek: frobnicate")
        st = self.state()
        self.assertEqual(st["records"], {})
        self.assertEqual(st["errors"], 2)

    def test_unknown_node_number_is_an_error_not_a_crash(self):
        self.hook("stop", last_assistant_message="creek: on 42")
        self.assertEqual(self.state()["errors"], 1)

    def test_no_markers_no_writes(self):
        self.hook("stop", last_assistant_message="Just a normal reply.")
        self.assertFalse(paths.events_path(self.cdir).exists())

    def test_bad_stdin_never_fails(self):
        proc = subprocess.run([sys.executable, "-m", "creek", "hook", "stop"], input=b"{not json", capture_output=True, check=False)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, b"")
        self.assertIn("hook stop", paths.errors_path(self.cdir).read_text())
        paths.errors_path(self.cdir).unlink()


if __name__ == "__main__":
    unittest.main()
