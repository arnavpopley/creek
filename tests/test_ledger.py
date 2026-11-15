import json
import multiprocessing as mp
import os
import random
import unittest
from collections import Counter

from creek import ledger, model, paths
from creek.synth import random_events

from helpers import ProjectCase, comparable


def _append_worker(root, worker, n):
    cdir = paths.creek_dir(paths.project_root(root))
    for i in range(n):
        ledger.append_event(cdir, {"type": "edit", "file": f"f{worker}.ts", "tool": "Edit", "i": i, "pad": "x" * random.randint(0, 3000)}, session=f"w{worker}")


def _tx_worker(root, worker, n):
    cdir = paths.creek_dir(paths.project_root(root))
    for i in range(n):
        with ledger.transaction(cdir, session=f"w{worker}") as tx:
            tx.emit({"type": "node.create", "title": f"w{worker}-{i}", "origin": "user"})


class FoldTest(ProjectCase):
    def test_10k_random_events_fold_correctly(self):
        events = random_events(10_000, seed=42)
        self.write_events(events)
        state = ledger.rebuild(self.cdir)
        types = Counter(e["type"] for e in events)

        self.assertEqual(state["seq"], 10_000)
        self.assertEqual(state["bad_lines"], 0)
        self.assertEqual(len(state["nodes"]), types["node.create"])
        self.assertEqual(len(state["branches"]), types["branch.open"])
        self.assertEqual(len(state["records"]), types["record.add"])
        self.assertEqual(len(state["conflicts"]), types["conflict.flag"])
        # Every ask either creates a question or bumps an existing one.
        self.assertEqual(sum(q["asks"] for q in state["questions"].values()), types["question.ask"])
        # Ids are dense and in order.
        for coll, p in (("nodes", "n"), ("branches", "b"), ("records", "r"), ("conflicts", "c")):
            self.assertEqual(sorted(state[coll], key=model.id_num), [f"{p}{i}" for i in range(1, len(state[coll]) + 1)])
        # The deliberately bad node.status events were counted, not fatal.
        self.assertGreater(state["errors"], 0)
        for rid, r in state["records"].items():
            self.assertIn(rid, state["nodes"][r["node"]]["records"])
            if not r["active"]:
                self.assertIsNotNone(r["superseded_by"])
            for f in r["files"]:
                self.assertIn(rid, state["index"]["file"][f])
        for s, t in state["turn"].items():
            self.assertEqual(t, sum(1 for e in events if e["type"] == "turn" and e["session"] == s))

    def test_incremental_equals_rebuild(self):
        events = random_events(3_000, seed=3)
        rng = random.Random(9)
        i = 0
        while i < len(events):
            j = min(len(events), i + rng.randint(1, 250))
            self.write_events(events[i:j])
            ledger.load_state(self.cdir)
            i = j
        cached = json.loads(paths.state_path(self.cdir).read_text())
        self.assertEqual(comparable(cached), comparable(ledger.rebuild(self.cdir)))

    def test_partial_trailing_line_is_left_for_later(self):
        self.write_events(random_events(50, seed=5))
        line = json.dumps({"v": 1, "ts": "t", "session": "s", "type": "node.create", "title": "late"}).encode()
        with open(paths.events_path(self.cdir), "ab") as f:
            f.write(line[:20])
        before = ledger.load_state(self.cdir)
        with open(paths.events_path(self.cdir), "ab") as f:
            f.write(line[20:] + b"\n")
        after = ledger.load_state(self.cdir)
        self.assertEqual(after["seq"], before["seq"] + 1)
        self.assertEqual(comparable(after), comparable(ledger.rebuild(self.cdir)))

    def test_rebuild_when_state_missing_or_schema_changes(self):
        self.write_events(random_events(200, seed=1))
        a = ledger.load_state(self.cdir)
        paths.state_path(self.cdir).unlink()
        self.assertEqual(comparable(ledger.load_state(self.cdir)), comparable(a))
        broken = dict(a, schema=999, nodes={})
        paths.state_path(self.cdir).write_text(json.dumps(broken))
        self.assertEqual(comparable(ledger.load_state(self.cdir)), comparable(a))

    def test_transaction_allocates_ids_in_log_order(self):
        with ledger.transaction(self.cdir, session="s1") as tx:
            n = tx.emit({"type": "node.create", "title": "One"})
            b = tx.emit({"type": "branch.open", "node": n["node"], "kind": "fork"})
            r = tx.emit({"type": "record.add", "node": n["node"], "branch": b["branch"], "decision": "Use a dialog", "affects": ["src/HelpPanel.tsx"]})
        self.assertEqual((n["node"], b["branch"], r["record"]), ("n1", "b1", "r1"))
        self.assertEqual(comparable(ledger.load_state(self.cdir)), comparable(ledger.rebuild(self.cdir)))

    def test_supersede_deactivates(self):
        with ledger.transaction(self.cdir, session="s") as tx:
            n = tx.emit({"type": "node.create", "title": "x"})["node"]
            tx.emit({"type": "record.add", "node": n, "decision": "a", "affects": ["a.ts"]})
            tx.emit({"type": "record.add", "node": n, "decision": "b", "affects": ["a.ts"], "supersedes": ["r1"]})
            st = tx.state
        self.assertFalse(st["records"]["r1"]["active"])
        self.assertEqual(st["records"]["r1"]["superseded_by"], "r2")
        self.assertTrue(st["records"]["r2"]["active"])


class ConcurrencyTest(ProjectCase):
    def test_parallel_appends_lose_nothing(self):
        ctx = mp.get_context("spawn")
        procs = [ctx.Process(target=_append_worker, args=(str(self.tmp), w, 300)) for w in range(8)]
        for p in procs:
            p.start()
        for p in procs:
            p.join()
            self.assertEqual(p.exitcode, 0)
        lines = paths.events_path(self.cdir).read_bytes().splitlines()
        self.assertEqual(len(lines), 8 * 300)
        seen = Counter()
        for line in lines:
            ev = json.loads(line)  # every line intact
            seen[(ev["session"], ev["i"])] += 1
        self.assertEqual(len(seen), 8 * 300)
        self.assertEqual(ledger.load_state(self.cdir)["edit_count"], 8 * 300)

    def test_parallel_transactions_allocate_unique_ids(self):
        ctx = mp.get_context("spawn")
        procs = [ctx.Process(target=_tx_worker, args=(str(self.tmp), w, 40)) for w in range(6)]
        for p in procs:
            p.start()
        for p in procs:
            p.join()
            self.assertEqual(p.exitcode, 0)
        state = ledger.load_state(self.cdir)
        self.assertEqual(sorted(state["nodes"], key=model.id_num), [f"n{i}" for i in range(1, 241)])
        self.assertEqual(len({n["title"] for n in state["nodes"].values()}), 240)
        self.assertEqual(comparable(state), comparable(ledger.rebuild(self.cdir)))
        # Each event carries the id the fold gives it.
        for line in paths.events_path(self.cdir).read_text().splitlines():
            ev = json.loads(line)
            self.assertEqual(state["nodes"][ev["node"]]["title"], ev["title"])


if __name__ == "__main__":
    unittest.main()
