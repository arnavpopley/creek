import unittest
from pathlib import Path

from creek import doctor, guard, ledger, replay, terms
from creek.hooks.post_tool_use import pr_events, review_score

from helpers import ProjectCase
from test_hooks import HookCase

FIXTURES = Path(__file__).parent / "fixtures"


class TermsTest(unittest.TestCase):
    def test_whole_token_matching(self):
        toks = terms.text_tokens("const d = document.querySelector('dialog[open]'); <dialog> showModal dialogue")
        self.assertIn("dialog", toks)
        self.assertIn("showmodal", toks)
        self.assertIn("dialogue", toks)
        self.assertNotIn("dialo", toks)

    def test_path_suffixes(self):
        toks = terms.text_tokens("/Users/me/site/src/components/HelpPanel.tsx")
        self.assertIn("helppanel.tsx", toks)
        self.assertIn("components/helppanel.tsx", toks)

    def test_extract_terms(self):
        t = terms.extract_terms(
            ["src/components/ContactIcons.tsx key field", "sessionStorage 'hero-typed'", "PR #31", "node 3"],
            ["dialog", "top layer"],
            "overlays must check `isOpen` via showModal() and data_series",
            "native <dialog>",
        )
        for want in ("src/components/contacticons.tsx", "contacticons.tsx", "sessionstorage", "hero-typed", "dialog", "top layer", "isopen", "showmodal", "data_series"):
            self.assertIn(want, t)
        self.assertNotIn("node", t)
        self.assertFalse([x for x in t if len(x) < 3])

    def test_parse_affects(self):
        a = terms.parse_affects(["src/config/site.ts profile.about", "PRs #35, #41", "next.config.ts", "production /"])
        self.assertEqual(a["files"], ["src/config/site.ts", "next.config.ts"])
        self.assertEqual(a["prs"], [35, 41])
        self.assertIn("src/config/site.ts#profile.about", a["keys"])


class GuardUnitTest(ProjectCase):
    def make(self, records):
        with ledger.transaction(self.cdir, "s") as tx:
            tx.emit({"type": "node.create", "title": "a"})
            tx.emit({"type": "node.create", "title": "b"})
            for r in records:
                tx.emit({"type": "record.add", **r})
            return tx.state

    def test_scoring_and_threshold(self):
        st = self.make([
            {"node": "n1", "decision": "number keys open links", "affects": ["src/components/ContactList.tsx"]},
            {"node": "n2", "decision": "dial rounded", "rule": "overlays check for an open dialog", "tags": ["dialog"], "affects": ["HelpPanel.tsx"]},
            {"node": "n2", "decision": "theme", "tags": ["colour"], "affects": ["theme.ts"]},
        ])
        def score(path, text):
            return guard.score(st, *guard.edit_text({"file_path": path, "new_string": text}))

        s = score("/x/src/components/ContactList.tsx", "nothing relevant")
        self.assertEqual(s["r1"][0], 3 + 2)  # file, plus its full path and basename as terms; no rule
        s = score("/x/Crosshair.tsx", "if (document.querySelector('dialog[open]')) return")
        self.assertEqual(s["r2"][0], 2)  # one term + rule
        s = score("/x/Other.tsx", "the colour is red")
        self.assertNotIn("r3", s)  # one term, no rule: below threshold

    def test_path_suffix_must_match(self):
        st = self.make([{"node": "n1", "decision": "x", "affects": ["src/a/Thing.tsx"]}])
        self.assertIn("r1", guard.score(st, "/root/src/a/Thing.tsx", ""))
        self.assertNotIn("r1", guard.score(st, "/root/src/b/Thing.tsx", ""))

    def test_inactive_never_shown(self):
        st = self.make([
            {"node": "n1", "decision": "old", "affects": ["a.ts"]},
            {"node": "n1", "decision": "new", "affects": ["a.ts"], "supersedes": ["r1"]},
        ])
        self.assertEqual([r for r, _ in guard.check(st, {"file_path": "/p/a.ts"}, "s")], ["r2"])

    def test_top_three(self):
        st = self.make([{"node": "n1", "decision": f"d{i}", "affects": ["a.ts"]} for i in range(6)])
        self.assertEqual(len(guard.check(st, {"file_path": "/p/a.ts"}, "s")), 3)

    def test_multiedit_and_write_text(self):
        path, text = guard.edit_text({"file_path": "/a.ts", "edits": [{"old_string": "x", "new_string": "dialog one"}, {"new_string": "two"}]})
        self.assertIn("dialog one", text)
        self.assertIn("two", text)
        self.assertEqual(guard.edit_text({"file_path": "/a.ts", "content": "z" * 50_000})[1].__len__(), guard.MAX_TEXT)


class GuardHookTest(HookCase):
    def test_cooldown_15_turns_and_reset_on_compact(self):
        self.hook("stop", last_assistant_message="creek: node Keys\ncreek: resolve 1 | decision: ignore repeats | rule: claim keys first | affects: src/components/ContactList.tsx")
        edit = {"file_path": str(self.tmp / "src/components/ContactList.tsx"), "old_string": "a", "new_string": "b"}
        out = self.hook("pre-tool-use", tool_name="Edit", tool_input=edit)
        self.assertIn("r1 (n1): ignore repeats. Rule: claim keys first", self.context(out))
        # Shown at turn 0, so suppressed through turn 15 (injected >= turn - 15).
        for _ in range(16):
            with ledger.transaction(self.cdir, self.session) as tx:
                tx.emit({"type": "turn"})
            if tx.state["turn"][self.session] <= 15:
                self.assertIsNone(self.hook("pre-tool-use", tool_name="Edit", tool_input=edit))
        self.assertIsNotNone(self.hook("pre-tool-use", tool_name="Edit", tool_input=edit))  # turn 16
        self.assertIsNone(self.hook("pre-tool-use", tool_name="Edit", tool_input=edit))
        with ledger.transaction(self.cdir, self.session) as tx:
            tx.emit({"type": "compact", "trigger": "auto"})
        self.assertIsNotNone(self.hook("pre-tool-use", tool_name="Edit", tool_input=edit))
        st = self.state()
        self.assertEqual(st["cost"]["pre-tool-use"]["count"], 3)

    def test_post_tool_use_logs_edits_with_focus(self):
        self.hook("stop", last_assistant_message="creek: node Keys\ncreek: on 1")
        self.hook("post-tool-use", tool_name="Write", tool_input={"file_path": str(self.tmp / "src/x.ts"), "content": "x"}, tool_response={})
        st = self.state()
        self.assertEqual(st["nodes"]["n1"]["edits"], {"src/x.ts": 1})

    def test_post_tool_use_pr_events(self):
        self.hook("stop", last_assistant_message="creek: node Ship\ncreek: on 1")
        self.hook("post-tool-use", tool_name="Bash", tool_input={"command": "gh pr create --fill"}, tool_response={"stdout": "https://github.com/me/site/pull/42\n", "stderr": ""})
        self.hook("post-tool-use", tool_name="Bash", tool_input={"command": "gh pr merge 42 --squash"}, tool_response={"stdout": "✓ Squashed and merged pull request me/site#42 (Add thing)", "stderr": ""})
        st = self.state()
        self.assertEqual(st["prs"]["42"]["action"], "merged")
        self.assertEqual(st["nodes"]["n1"]["prs"], [42])
        # A merged PR with no record after it is flagged at the next Stop.
        self.hook("stop", last_assistant_message="Merged.")
        self.assertTrue(self.state()["nodes"]["n1"]["unrecorded"])
        self.hook("stop", last_assistant_message="creek: resolve 1 | decision: shipped")
        self.assertFalse(self.state()["nodes"]["n1"]["unrecorded"])


class PrParseTest(unittest.TestCase):
    def test_actions(self):
        self.assertEqual(pr_events("gh pr create --draft --fill", "https://github.com/a/b/pull/7")[0]["action"], "draft")
        self.assertEqual(pr_events("gh pr ready 7", "")[0], {"type": "pr", "number": 7, "action": "opened"})
        self.assertEqual(pr_events("gh pr ready 7 --undo", "")[0]["action"], "draft")
        self.assertEqual(pr_events("cd x && gh pr merge 9 --merge", "Merged pull request #9")[0]["action"], "merged")
        self.assertEqual(pr_events("gh pr close 3", "")[0]["action"], "closed")
        self.assertEqual(pr_events("git push", "remote: https://github.com/a/b/pull/new/feature"), [])
        self.assertEqual(pr_events("gh pr view 12", "url: https://github.com/a/b/pull/12")[0]["number"], 12)
        self.assertEqual(pr_events("ls", "nothing"), [])

    def test_review_score(self):
        self.assertEqual(review_score("greptile-apps commented: Confidence score: 4/5"), (4, "greptile"))
        self.assertEqual(review_score("Greptile Summary\n...\n**Confidence Score: 5/5**"), (5, "greptile"))
        self.assertEqual(review_score("tests 4/5 passed"), (None, None))


class WebsiteReplayTest(ProjectCase):
    """M2 acceptance on a replay of the website session."""

    def setUp(self):
        super().setUp()
        self.fx = replay.load(FIXTURES / "website-session.json")
        self.ann = replay.load(FIXTURES / "website-annotations.json")
        self.rep = replay.run(self.fx, self.cdir, self.ann)
        self.state = ledger.load_state(self.cdir)
        self.rec = self.rep["record_of"]

    def lost(self, bid):
        return next(d for d in self.rep["lost_detail"] if d["branch"] == bid)

    def test_b7_case_contactlist_edit_injects_b5_rule(self):
        d = self.lost("b5")
        self.assertEqual(d["variants"][0]["verdict"], "injected")
        # And the injected text carried b5's rule.
        rid = self.rec["b5"]
        shown = [i for i in self.rep["injections"] if i["msg"] == 7 and rid in i["records"]]
        self.assertTrue(shown)
        self.assertIn("before the key is marked handled", self.state["records"][rid]["rule"])

    def test_top_layer_case_crosshair_with_dialog_injects_b2(self):
        rid = self.rec["b2"]
        edit = {"file_path": "/site/src/components/Crosshair.tsx", "new_string": "const open = document.querySelector('dialog[open]');"}
        hits = [r for r, _ in guard.check(self.state, edit, "fresh-session")]
        self.assertIn(rid, hits)
        self.assertTrue(self.lost("b2")["first_touch_recovered"])

    def test_not_injected_twice_within_15_turns(self):
        rid = self.rec["b2"]
        msgs = [i["msg"] for i in self.rep["injections"] if rid in i["records"]]
        for a, b in zip(msgs, msgs[1:]):
            self.assertGreater(b - a, guard.COOLDOWN, msgs)

    def test_lost_detail_report(self):
        rep = self.rep
        self.assertEqual(rep["total_lost"], 8)
        recovered = {d["branch"] for d in rep["lost_detail"] if d["recovered"]}
        self.assertEqual(recovered, {"b2", "b3", "b5", "b6", "b13", "b20"})
        misses = {d["branch"]: d["how"] for d in rep["lost_detail"] if not d["recovered"]}
        for bid in ("b9", "b11"):
            self.assertTrue(misses[bid].startswith("known miss:"))

    def test_decision_only_baseline_recovers_less(self):
        tmp = self.tmp / "raw"
        (tmp / ".git").mkdir(parents=True)
        from creek import paths

        raw = replay.run(self.fx, paths.creek_dir(tmp), self.ann, use_markers=False)
        self.assertLess(raw["recovered"], self.rep["recovered"])


class LatencyTest(unittest.TestCase):
    def test_p95_under_100ms_with_500_events(self):
        res = doctor.bench(n_events=500, runs=15)
        errors = res.pop("_errors")
        self.assertEqual(errors, "")
        for event, p95 in res.items():
            self.assertLess(p95, 100, f"{event}: {p95:.1f} ms")


if __name__ == "__main__":
    unittest.main()
