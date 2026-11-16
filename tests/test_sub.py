"""Sub-chats end to end, with a stand-in `claude` that runs Creek's real hooks."""

import json
import os
import stat
import subprocess
import sys
import textwrap

from creek import ledger, sub

from test_hooks import HookCase

FAKE = textwrap.dedent('''\
    #!{python}
    # Stand-in for `claude -p`: fires SessionStart and Stop like Claude Code, replies from $FAKE_REPLY.
    import json, os, subprocess, sys, uuid
    args = sys.argv[1:]
    fork = "--fork-session" in args
    sid = str(uuid.uuid4()) if fork else args[args.index("--resume") + 1]
    def hook(event, **payload):
        body = {{"session_id": sid, "cwd": os.getcwd(), "transcript_path": "", **payload}}
        subprocess.run([{python!r}, "-m", "creek", "hook", event], input=json.dumps(body).encode(), capture_output=True, check=False)
    hook("session-start", source="fork" if fork else "resume")
    hook("user-prompt-submit", prompt=args[-1], permission_mode="bypassPermissions")
    reply = os.environ.get("FAKE_REPLY", "Done.")
    hook("stop", last_assistant_message=reply)
    with open(os.environ["FAKE_LOG"], "a") as f:
        f.write(json.dumps(args) + "\\n")
    print(json.dumps({{"session_id": sid, "result": reply, "is_error": False}}))
''')


class SubChatTest(HookCase):
    def setUp(self):
        super().setUp()
        exe = self.tmp / "claude"
        exe.write_text(FAKE.format(python=sys.executable))
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
        self.log = self.tmp / "claude.log"
        os.environ.update(CREEK_CLAUDE=str(exe), FAKE_LOG=str(self.log))
        # The main chat: a brief, two points done, a question waiting on point 3.
        self.hook("session-start", source="startup")
        self.hook("user-prompt-submit", prompt="Changes:\n1. Sticky header\n2. Shortcuts w and t\n3. Pick a link colour (ask me)", permission_mode="acceptEdits")
        self.hook("stop", last_assistant_message=(
            "creek: resolve 1 | decision: sticky header | affects: style.css\n"
            "creek: resolve 2 | decision: w and t shortcuts | affects: app.js\n"
            "creek: on 3\ncreek: ask Which colour should links be?"
        ))

    def tearDown(self):
        for k in ("CREEK_CLAUDE", "FAKE_LOG", "FAKE_REPLY"):
            os.environ.pop(k, None)
        super().tearDown()

    def test_describe(self):
        d = sub.describe(self.cdir)
        self.assertEqual(d["brief"], "Changes:\n1. Sticky header\n2. Shortcuts w and t\n3. Pick a link colour (ask me)")
        self.assertEqual([s["node"] for s in d["subs"]], ["n3"])
        self.assertEqual(d["subs"][0]["waiting"], "Which colour should links be?")
        self.assertEqual([s["node"] for s in d["done"]], ["n1", "n2"])
        self.assertEqual(d["main_session"], self.session)

    def test_answer_in_a_sub_chat_settles_and_merges(self):
        os.environ["FAKE_REPLY"] = "Links are teal.\ncreek: resolve 3 | decision: links are teal | affects: style.css"
        res = sub.send(self.cdir, "3", "teal")
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["settled"])
        self.assertEqual(res["reply"], "Links are teal.")
        args = json.loads(self.log.read_text().splitlines()[0])
        self.assertEqual(args[args.index("--resume") + 1], self.session)  # forked from the main chat
        self.assertIn("--fork-session", args)
        self.assertEqual(args[args.index("--permission-mode") + 1], "acceptEdits")  # the main chat's mode
        st = ledger.load_state(self.cdir)
        self.assertEqual(st["questions"]["q1"]["status"], "answered")
        self.assertEqual(st["nodes"]["n3"]["status"], "resolved")
        br = st["branches"][res["branch"]]
        self.assertEqual((br["kind"], br["status"]), ("fork", "merged"))
        self.assertEqual([m["who"] for m in st["subchats"]["n3"]["thread"]], ["you", "claude"])
        # The main chat hears it in one line.
        self.assertIn(f"{res['branch']} merged into n3: links are teal", st["pending_notes"][self.session])
        d = sub.describe(self.cdir)
        self.assertEqual(d["subs"], [])

    def test_unsettled_turn_keeps_the_sub_chat_and_resumes_it(self):
        os.environ["FAKE_REPLY"] = "Teal or navy? Say which."
        first = sub.send(self.cdir, "3", "something calm")
        self.assertFalse(first["settled"])
        os.environ["FAKE_REPLY"] = "Navy it is.\ncreek: resolve 3 | decision: navy links"
        second = sub.send(self.cdir, "3", "navy")
        self.assertTrue(second["settled"])
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertNotIn("--fork-session", calls[1])  # the second turn resumes the sub-chat's own session
        self.assertEqual(first["branch"], second["branch"])

    def test_reopening_a_settled_point(self):
        os.environ["FAKE_REPLY"] = "Added h.\ncreek: resolve 2 | decision: h goes home too | affects: app.js"
        res = sub.send(self.cdir, "2", "also h for home")
        self.assertTrue(res["settled"])
        st = ledger.load_state(self.cdir)
        self.assertEqual(st["conflicts"], {})  # same point: no false conflict

    def test_cli(self):
        out = subprocess.run([sys.executable, "-m", "creek", "sub", "list", "--root", str(self.tmp)], capture_output=True, text=True)
        self.assertEqual(json.loads(out.stdout)["subs"][0]["node"], "n3")
        bad = subprocess.run([sys.executable, "-m", "creek", "sub", "send", "9", "x", "--root", str(self.tmp)], capture_output=True, text=True)
        self.assertEqual(bad.returncode, 1)
        self.assertIn("no point 9", bad.stdout)
