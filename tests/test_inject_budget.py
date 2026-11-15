import unittest

from creek import inject, ledger, model
from creek.synth import big_project

from helpers import ProjectCase


def state_of(events):
    st = model.empty_state()
    for i, ev in enumerate(events):
        ev.setdefault("v", 1)
        ev.setdefault("ts", f"t{i}")
        ev.setdefault("session", "main")
        ledger.apply(st, ev)
    return st


class BudgetTest(unittest.TestCase):
    def test_protocol_under_180_tokens(self):
        self.assertLessEqual(inject.tokens(inject.PROTOCOL), 180)

    def test_state_block_capped_on_300_nodes(self):
        st = state_of(big_project(300))
        self.assertEqual(len(st["nodes"]), 300)
        for source in ("startup", "resume", "compact", "clear", "fork"):
            block, shown = inject.render_state(st, "main", source=source)
            self.assertLessEqual(inject.tokens(block), inject.STATE_TOKENS, source)
            self.assertTrue(block)

    def test_all_open_nodes_overflow_is_summarised(self):
        events = [{"type": "node.create", "title": f"a fairly long node title number {i} with words"} for i in range(1, 400)]
        st = state_of(events)
        block, _ = inject.render_state(st, "main")
        self.assertLessEqual(inject.tokens(block), inject.STATE_TOKENS)
        self.assertIn("more open items not shown", block)

    def test_truncation_order(self):
        events = [{"type": "node.create", "title": "Open thing"}, {"type": "node.create", "title": "Done thing"}]
        events.append({"type": "question.ask", "text": "Which key should 'more' use?"})
        # Many long rules on the resolved node and decisions, few on the open one.
        for i in range(60):
            events.append({"type": "record.add", "node": "n2", "decision": f"decision {i} " + "d" * 120, "rule": f"resolved rule {i} " + "r" * 150, "affects": ["a.ts"]})
        for i in range(60):
            events.append({"type": "record.add", "node": "n2", "decision": f"plain decision {i} " + "p" * 120, "affects": ["b.ts"]})
        events.append({"type": "node.create", "title": "Reopened"})
        events.append({"type": "record.add", "node": "n1", "decision": "x", "rule": "open-node rule", "affects": ["c.ts"]})
        events.append({"type": "node.status", "node": "n1", "status": "open"})
        st = state_of(events)
        block, shown = inject.render_state(st, "main", source="compact")
        self.assertLessEqual(inject.tokens(block), inject.STATE_TOKENS)
        self.assertIn("Which key should 'more' use?", block)
        self.assertIn("n1 open: Open thing", block)
        self.assertIn("open-node rule", block)  # rules for unresolved nodes survive
        self.assertNotIn("plain decision", block)  # decisions were dropped first
        self.assertIn("resolved rule 59", block)  # newest resolved rules kept while room

    def test_compact_adds_decisions_only_on_compact(self):
        events = [{"type": "node.create", "title": "a"}, {"type": "record.add", "node": "n1", "decision": "keep the dial rounded", "affects": ["x.ts"]}]
        st = state_of(events)
        self.assertNotIn("keep the dial rounded", inject.render_state(st, "main", source="startup")[0])
        self.assertIn("keep the dial rounded", inject.render_state(st, "main", source="compact")[0])

    def test_guard_lines_capped(self):
        rec = {"node": "n1", "turn": 12, "decision": "d" * 400, "rule": "r" * 400}
        self.assertLessEqual(len(inject.guard_line("r7", rec)), inject.GUARD_LINE)
        rec = {"node": "n1", "turn": 12, "decision": "Rebuilt the panel as a native <dialog>" + " long" * 40, "rule": "overlays must check for an open dialog"}
        line = inject.guard_line("r7", rec)
        self.assertLessEqual(len(line), inject.GUARD_LINE)
        self.assertTrue(line.endswith("Rule: overlays must check for an open dialog"))

    def test_notes_capped(self):
        text, n = inject.render_notes([f"note {i} " + "x" * 300 for i in range(20)])
        self.assertLessEqual(inject.tokens(text), inject.NOTES_TOKENS)
        self.assertGreater(n, 0)
        self.assertLess(n, 20)


if __name__ == "__main__":
    unittest.main()
