import json
import unittest
from pathlib import Path

from creek import graph, ledger, replay

from helpers import ProjectCase

FIXTURES = Path(__file__).parent / "fixtures"


def ev(t, **kw):
    return {"v": 1, "ts": "t", "session": "s", "type": t, **kw}


class GraphTest(unittest.TestCase):
    def test_brief_forks_from_its_prompt_and_merges_when_settled(self):
        g = graph.build([
            ev("turn", preview="1. a 2. b 3. c"),
            ev("node.create", title="a", origin="brief", node="n1"),
            ev("node.create", title="b", origin="brief", node="n2"),
            ev("record.add", node="n1", decision="did a", record="r1"),
            ev("question.ask", text="Which b?", node="n2", question="q1"),
            ev("turn", preview="carry on"),
        ])
        segs = {s["node"]: s for s in g["segments"]}
        prompt1 = next(d["col"] for d in g["dots"] if d["kind"] == "prompt")
        self.assertEqual(segs["n1"]["from"], prompt1)
        self.assertEqual(segs["n2"]["from"], prompt1)
        self.assertIsNotNone(segs["n1"]["merge"])  # settled: merged back
        self.assertIsNone(segs["n2"]["merge"])  # waiting on a question: still open
        self.assertIn("open_to", segs["n2"])
        self.assertEqual([d["kind"] for d in g["dots"]].count("question"), 1)

    def test_old_order_brief_before_turn(self):
        g = graph.build([
            ev("node.create", title="a", origin="brief", node="n1"),
            ev("turn"),
            ev("record.add", node="n1", decision="x", record="r1"),
        ])
        self.assertEqual(g["segments"][0]["from"], 1)

    def test_reopen_forks_again_and_reminders_link_decisions(self):
        g = graph.build([
            ev("turn"), ev("node.create", title="a", node="n1"),
            ev("record.add", node="n1", decision="x", record="r1", affects=["a.ts"]),
            ev("turn"),
            ev("inject", hook="pre-tool-use", records=["r1"], chars=10),
            ev("record.add", node="n1", decision="y", record="r2", affects=["a.ts"]),
            ev("node.create", title="b", node="n2"),
            ev("record.add", node="n2", decision="z", record="r3", affects=["a.ts"]),
            ev("conflict.flag", records=["r1", "r3"], overlap=["a.ts"], conflict="c1"),
        ])
        n1 = [s for s in g["segments"] if s["node"] == "n1"]
        self.assertEqual(len(n1), 2)
        self.assertEqual({a["kind"] for a in g["arcs"]}, {"remind", "conflict"})

    def test_parked_branch_ends_without_merge(self):
        g = graph.build([ev("turn"), ev("node.create", title="p", node="n1"),
                         ev("record.add", node="n1", decision="built it", record="r1"),
                         ev("node.status", node="n1", status="open"),
                         ev("node.status", node="n1", status="tabled", reason="too big")])
        self.assertTrue(any(d["kind"] == "tabled" for d in g["dots"]))


class WebsiteGraphTest(ProjectCase):
    def test_fixture_graph(self):
        replay.run(replay.load(FIXTURES / "website-session.json"), self.cdir, replay.load(FIXTURES / "website-annotations.json"))
        g = graph.build(ledger.read_events(self.cdir)[0])
        self.assertEqual(len(g["rows"]), 20)  # main + 19 points
        self.assertEqual(sum(1 for d in g["dots"] if d["kind"] == "prompt"), 16)
        self.assertTrue(any(d["kind"] == "tabled" for d in g["dots"]))  # the custom pointer
        json.dumps(g)


if __name__ == "__main__":
    unittest.main()
