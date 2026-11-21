import os
os.environ["L2A_KEYS_FILE"] = "off"  # never read or write the real key file
os.environ["L2A_PDF"] = "0"
import contextlib
import io
import json
import pathlib
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from lecture2audio import cli, stages, trace
from lecture2audio.flow import Flow
from lecture2audio.llm import dry_run_handler


class Graph(unittest.TestCase):
    def test_part_needs_come_from_the_stage_graph(self):
        self.assertEqual(stages.part_needs("rewrite"), (["ideas"], ["outline", "gaps", "research"]))
        self.assertEqual(stages.part_needs("audio"), (["spoken"], []))
        self.assertEqual(stages.part_needs("recitation"), (["rewrite", "ideas", "course"], []))
        # a whole-lecture stage waits for every part of what it needs
        self.assertEqual(stages.part_needs("factcheck"), ([], ["rewrite", "ideas"]))

    def test_a_slow_part_does_not_hold_back_the_next_stage_of_the_others(self):
        flow, release = Flow(workers=8), threading.Event()
        flow.add("gaps", lambda: None, stage="gaps")
        for part in ("1", "2", "3"):
            flow.add_part("ideas", part, lambda: None)
            flow.add_part("rewrite", part, (lambda: release.wait(5)) if part == "1"
                          else (lambda: None))
            flow.add_part("spoken", part, (lambda: release.set()) if part == "3"
                          else (lambda: None))
        flow.run()
        order = flow.order
        # part 1's script waited until part 3 had its spoken form: no wave between stages
        self.assertLess(order.index(("spoken", "3")), order.index(("rewrite", "1")))
        self.assertLess(order.index(("spoken", "2")), order.index(("spoken", "1")))
        self.assertLess(order.index(("rewrite", "1")), order.index(("spoken", "1")))

    def test_needs_are_waited_for_and_main_nodes_run_in_the_calling_thread(self):
        flow, seen = Flow(workers=4), {}
        flow.add("a", lambda: (time.sleep(0.05), 1)[1])
        flow.add("b", lambda: seen.setdefault("b", threading.current_thread().name) and 2,
                 ["a"], main=True)
        flow.add("c", lambda: flow["a"] + flow["b"], ["a", "b"])
        self.assertEqual(flow.run()["c"], 3)
        self.assertEqual(seen["b"], threading.current_thread().name)
        with self.assertRaises(ValueError):
            flow.add("d", lambda: 0, ["unknown"])

    def test_a_failure_stops_new_nodes_and_is_raised(self):
        flow, ran = Flow(workers=2), []

        def boom():
            raise RuntimeError("bad part")
        flow.add("a", boom)
        flow.add("b", lambda: ran.append("b"), ["a"])
        flow.add("m", lambda: ran.append("m"), ["a"], main=True)
        with self.assertRaises(RuntimeError):
            flow.run()
        self.assertEqual(ran, [])


class Tracking(unittest.TestCase):
    def test_a_stage_runs_while_any_part_is_in_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            tracker = stages.Tracker(pathlib.Path(tmp))
            flow, gate = Flow(workers=4, tracker=tracker), threading.Event()
            for part in ("1", "2"):
                flow.add_part("ideas", part, (lambda: gate.wait(5)) if part == "1"
                              else (lambda: None))
                flow.add_part("rewrite", part, (lambda: gate.set()) if part == "2"
                              else (lambda: None))
            flow.run()
            events = [json.loads(line) for line in
                      (pathlib.Path(tmp) / "events.jsonl").read_text().splitlines()]
        pairs = [(e["stage"], e["state"]) for e in events]
        # one running and one done event per stage, rewrite running while ideas still runs
        self.assertEqual(sorted(pairs), sorted([("ideas", "running"), ("ideas", "done"),
                                                ("rewrite", "running"), ("rewrite", "done")]))
        self.assertLess(pairs.index(("rewrite", "running")), pairs.index(("ideas", "done")))
        self.assertEqual(tracker.running, [])


class Streaming(unittest.TestCase):
    TRANSCRIPT = ("Today we look at routing. A router forwards packets between networks. "
                  "It reads the destination address of each packet.\n\n"
                  "Next, the routing table. Each entry maps a prefix to a next hop. The "
                  "longest matching prefix wins.\n\n"
                  "Finally, costs. Each link has a cost of 1 or more, and the protocol adds "
                  "the costs along a path. The cheapest path wins.")

    def setUp(self):
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("L2A_VAULT_DIR", None)
        self.addCleanup(trace.close)  # the run's calls.jsonl lives in a folder deleted below

    def test_a_slow_part_does_not_hold_back_the_other_parts_in_a_build(self):
        def slow(system, user):  # every model call about part 1, after its ideas, is slow
            if ("You list the atomic ideas" not in system
                    and any(line.startswith("1 ") and "the part you are working on" in line
                            for line in user.splitlines())):
                time.sleep(0.4)
            return dry_run_handler(system, user)

        with tempfile.TemporaryDirectory() as tmp:
            source = pathlib.Path(tmp) / "lecture.txt"
            source.write_text(self.TRANSCRIPT, encoding="utf-8")
            out = pathlib.Path(tmp) / "out"
            with mock.patch.object(cli, "dry_run_handler", slow), \
                    contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as end:
                cli.main(["text", str(source), "--llm", "dry", "--out", str(out), "--language",
                          "en", "--max-part-words", "25", "--factcheck", "off"])
            self.assertEqual(end.exception.code, 0)
            work = out / "lecture"
            parts = len(json.loads((work / "outline.json").read_text())["parts"])
            self.assertGreaterEqual(parts, 2)
            done = lambda path: (work / path).stat().st_mtime_ns
            # the last part had its spoken form and its cards before part 1 had its script
            self.assertLess(done(f"spoken/part_{parts:03d}.json"), done("rewrite/part_001.json"))
            self.assertLess(done(f"course/part_{parts:03d}.json"), done("rewrite/part_001.json"))
            self.assertTrue((work / "report.md").exists())


if __name__ == "__main__":
    unittest.main()
