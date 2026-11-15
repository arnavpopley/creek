import unittest

from creek import parse


class MarkerTest(unittest.TestCase):
    def one(self, text):
        markers, errors = parse.parse_markers(text)
        self.assertEqual(errors, [], errors)
        self.assertEqual(len(markers), 1, markers)
        return markers[0]

    def test_on(self):
        self.assertEqual(self.one("creek: on 13")["node"], "13")
        self.assertEqual(self.one("creek: on n13")["node"], "n13")
        self.assertEqual(self.one("CREEK: ON Contact keys")["node"], "Contact keys")

    def test_node(self):
        self.assertEqual(self.one("creek: node Which key 'more' uses")["title"], "Which key 'more' uses")
        self.assertEqual(self.one("creek: node h shortcut | h→/ added to the handler")["title"], "h shortcut")
        self.assertEqual(self.one("creek: on 3 | starting the header")["node"], "3")

    def test_ask(self):
        self.assertEqual(self.one("creek: ask Which key should 'more' use?")["text"], "Which key should 'more' use?")

    def test_resolve_full(self):
        m = self.one(
            "creek: resolve 1 | decision: rebuilt the help panel as a native <dialog> | "
            "rule: panel is in the top layer; overlays must check for an open dialog | "
            "affects: src/components/HelpPanel.tsx, PR #30 | tags: dialog, overlay, pointer | supersedes: r3, 4"
        )
        self.assertEqual(m["node"], "1")
        self.assertEqual(m["decision"], "rebuilt the help panel as a native <dialog>")
        self.assertEqual(m["rule"], "panel is in the top layer; overlays must check for an open dialog")
        self.assertEqual(m["affects"], ["src/components/HelpPanel.tsx", "PR #30"])
        self.assertEqual(m["tags"], ["dialog", "overlay", "pointer"])
        self.assertEqual(m["supersedes"], ["r3", "r4"])

    def test_resolve_without_node_and_optional_fields(self):
        m = self.one("creek: resolve | decision: ignore key repeats | affects: ContactList.tsx")
        self.assertIsNone(m["node"])
        self.assertIsNone(m["rule"])
        self.assertEqual(m["tags"], [])
        m = self.one("creek: resolve decision: ignore key repeats")
        self.assertIsNone(m["node"])
        self.assertEqual(m["decision"], "ignore key repeats")

    def test_unknown_keys_ignored(self):
        m = self.one("creek: resolve 11 | decision: claim the key | mood: good | affects: a.ts")
        self.assertEqual(m["affects"], ["a.ts"])
        self.assertNotIn("mood", m)

    def test_markdown_decoration(self):
        self.assertEqual(self.one("- creek: on 3")["node"], "3")
        self.assertEqual(self.one("`creek: on 3`")["node"], "3")
        self.assertEqual(self.one("**creek: on 3**")["node"], "3")
        # A trailing backtick that belongs to the content stays.
        m = self.one("creek: resolve 2 | decision: use `showModal`")
        self.assertEqual(m["decision"], "use `showModal`")

    def test_only_marker_lines(self):
        text = "I fixed it.\nThe creek: thing is great\ncreek: on 4\nmore text\ncreek: ask Is this ok?"
        markers, errors = parse.parse_markers(text)
        self.assertEqual([m["kind"] for m in markers], ["on", "ask"])
        self.assertEqual(errors, [])

    def test_malformed_skipped_with_error(self):
        markers, errors = parse.parse_markers(
            "creek: resolve 3 | rule: no decision here\ncreek: on\ncreek: merge 3\ncreek: node   \ncreek: on 5"
        )
        self.assertEqual([m["kind"] for m in markers], ["on"])
        self.assertEqual(len(errors), 4)

    def test_never_raises(self):
        for junk in [None, "", "creek:", "creek: resolve |||", "creek: resolve | decision:", "\x00creek: on 1", "creek: on " + "x" * 5000]:
            parse.parse_markers(junk)


class CommandTest(unittest.TestCase):
    def test_commands(self):
        self.assertEqual(parse.parse_command("::answer q1 o"), ("answer", "q1 o"))
        self.assertEqual(parse.parse_command("  ::status"), ("status", ""))
        self.assertEqual(parse.parse_command(":: table 9 hides the real information"), ("table", "9 hides the real information"))
        self.assertIsNone(parse.parse_command("fix the :: operator"))
        self.assertIsNone(parse.parse_command(":not a command"))


class BriefTest(unittest.TestCase):
    def test_twelve_items(self):
        prompt = "Here's the plan:\n" + "\n".join(f"{i}. item number {i}" for i in range(1, 13)) + "\nthanks"
        self.assertEqual(parse.split_brief(prompt), [f"item number {i}" for i in range(1, 13)])

    def test_nested_not_split(self):
        prompt = "1. Help panel\n   1. open with ?\n   2. add a hint\n2) Ticker tiles\n3. Rename views\n  - sub bullet"
        self.assertEqual(parse.split_brief(prompt), ["Help panel", "Ticker tiles", "Rename views"])

    def test_two_items_is_not_a_brief(self):
        self.assertEqual(parse.split_brief("1. one\n2. two"), [])

    def test_brief_prefix(self):
        self.assertEqual(parse.split_brief("brief: 1. one\n2. two"), ["one", "two"])
        self.assertEqual(parse.split_brief("brief:\n- alpha\n- beta"), ["alpha", "beta"])
        self.assertEqual(parse.split_brief("Brief: tidy the header"), ["tidy the header"])

    def test_title_cut_and_first_line(self):
        long = "x" * 200
        titles = parse.split_brief(f"1. {long}\n   more detail\n2. **bold** two\n3. three")
        self.assertEqual(len(titles[0]), 80)
        self.assertEqual(titles[1], "bold two")

    def test_fenced_code_ignored(self):
        prompt = "```\n1. a\n2. b\n3. c\n```\nplease run that"
        self.assertEqual(parse.split_brief(prompt), [])


if __name__ == "__main__":
    unittest.main()
