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


class QuestionFilingTest(HookCase):
    def test_question_without_focus_files_under_the_open_point(self):
        self.hook("user-prompt-submit", prompt="1. Sticky header\n2. Shortcuts w and t\n3. Pick an accent colour for links, ask me")
        self.hook("stop", last_assistant_message=(
            "creek: resolve 1 | decision: sticky\ncreek: resolve 2 | decision: shortcuts\n"
            "creek: ask What accent colour should links use? A hex value or a colour name is fine."
        ))
        st = ledger.load_state(self.cdir)
        self.assertEqual(st["questions"]["q1"]["node"], "n3")
        self.assertEqual(sub.describe(self.cdir)["subs"][0]["waiting"], "What accent colour should links use? A hex value or a colour name is fine.")


class InChatRoutingTest(HookCase):
    """'3: teal' in the main chat goes to point 3's sub-chat, with a view of the sub-chats in Orca."""

    def setUp(self):
        super().setUp()
        self.scratch = self.tmp / "orca-scratch"
        self.scratch.mkdir()
        self.env = {"ORCA_CHAT_SCRATCH_DIR": str(self.scratch)}

    def ctx(self, prompt):
        return self.context(self.hook("user-prompt-submit", env=self.env, prompt=prompt))

    def test_whole_flow(self):
        ctx = self.ctx("Small changes:\n1. Sticky header\n2. Shortcuts w and t\n3. Pick a link colour, ask me")
        self.assertIn('::orca-visual{file="creek-subchats-', ctx)
        self.assertIn("**1 · title**", ctx)
        # Claude answers point by point; each section lands in its sub-chat.
        self.hook("stop", env=self.env, last_assistant_message=(
            "::orca-visual{file=\"x.html\"}\n\n**1 · Sticky header**\nThe header is sticky now.\n\n"
            "**2 · Shortcuts**\nw and t work.\n\n**3 · Link colour**\nWhich colour do you want?\n\n"
            "creek: resolve 1 | decision: sticky header | affects: style.css\n"
            "creek: resolve 2 | decision: w and t | affects: app.js\ncreek: on 3\ncreek: ask Which colour should links be?"
        ))
        st = self.state()
        self.assertEqual(st["subchats"]["n1"]["thread"][0]["text"], "The header is sticky now.")
        self.assertEqual(st["subchats"]["n3"]["thread"][0]["text"], "Which colour do you want?")

        ctx = self.ctx("3: teal")
        self.assertIn("this message is for sub-chat 3", ctx)
        st = self.state()
        self.assertEqual(st["questions"]["q1"]["status"], "answered")
        self.assertEqual(st["sub_route"][self.session], "n3")
        view = max(self.scratch.glob("creek-subchats-*.html"), key=lambda p: p.stat().st_mtime_ns).read_text()
        data = json.loads(view.split('id="d">')[1].split("</script>")[0])
        cards = {c["node"]: c for c in data["cards"]}
        # Points 1 and 2 settled since the first view: they dissolve in this one.
        self.assertTrue(cards["n1"]["settling"] and cards["n2"]["settling"])
        self.assertEqual(cards["n3"]["state"], "replying below ↓")
        self.assertEqual(cards["n3"]["thread"][-1], {"who": "you", "text": "teal", "ts": cards["n3"]["thread"][-1]["ts"]})

        self.hook("stop", env=self.env, last_assistant_message="Links are teal now.\ncreek: resolve 3 | decision: teal links | affects: style.css")
        st = self.state()
        self.assertEqual(st["subchats"]["n3"]["thread"][-1]["text"], "Links are teal now.")
        self.assertEqual(st["nodes"]["n3"]["status"], "resolved")
        self.assertNotIn(self.session, st.get("sub_route", {}))

        # A settled point reopens when addressed.
        ctx = self.ctx("2: also h for home")
        self.assertIn("sub-chat 2", ctx)
        self.assertEqual(self.state()["nodes"]["n2"]["status"], "open")

    def test_numbered_prompt_that_is_not_a_point(self):
        out = self.hook("user-prompt-submit", env=self.env, prompt="42: what is this")
        self.assertIsNone(out)

    def test_outside_orca_routes_without_a_view(self):
        self.hook("user-prompt-submit", prompt="1. a\n2. b\n3. c")
        ctx = self.context(self.hook("user-prompt-submit", prompt="2: more on b"))
        self.assertIn("sub-chat 2", ctx)
        self.assertNotIn("orca-visual", ctx)


class StickyTest(HookCase):
    def test_number_once_then_plain_messages_stay_until_settled_or_zero(self):
        self.hook("user-prompt-submit", prompt="1. a thing\n2. b thing\n3. c thing")
        self.hook("stop", last_assistant_message="**1 · a**\nok\n<!--\ncreek: resolve 1 | decision: a done\n-->")
        self.assertEqual(self.state()["nodes"]["n1"]["status"], "resolved")  # markers inside a comment still count

        self.assertIn("sub-chat 3", self.context(self.hook("user-prompt-submit", prompt="3: make it blue")))
        self.hook("stop", last_assistant_message="Which blue?")
        ctx = self.context(self.hook("user-prompt-submit", prompt="navy please"))
        self.assertIn("sub-chat 3", ctx)  # still with point 3
        st = self.state()
        self.assertEqual([m["text"] for m in st["subchats"]["n3"]["thread"]], ["make it blue", "Which blue?", "navy please"])
        self.hook("stop", last_assistant_message="Navy it is.\n<!-- creek: resolve 3 | decision: navy -->")
        st = self.state()
        self.assertEqual(st["subchats"]["n3"]["thread"][-1]["text"], "Navy it is.")  # comment stripped
        self.assertNotIn(self.session, st.get("sticky", {}))  # settled: back to everyone
        out = self.hook("user-prompt-submit", prompt="thanks")
        self.assertIsNone(out)

        self.hook("user-prompt-submit", prompt="2: start b")
        self.assertEqual(self.state()["sticky"][self.session], "n2")
        ctx = self.context(self.hook("user-prompt-submit", prompt="0: what's left overall?"))
        self.assertIn("whole brief again", ctx)
        self.assertNotIn(self.session, self.state().get("sticky", {}))
