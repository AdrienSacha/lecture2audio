import os
os.environ["L2A_KEYS_FILE"] = "off"  # never read or write the real key file
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from lecture2audio import stages
from lecture2audio.stages import Stage

# The --redo table as it was written by hand before the stage graph (cli.py, November 2026).
# The graph must give exactly the same files for every stage.
GAPS = ["coverage.json", "coverage_audit.json", "termbase.json", "research"]
DERIVED = ["ideas.csv", "anki.csv"]
VOICE = ["spoken", "audio", "parts"]
IDEAS = ["ideas", "ideas_audit.json"]
CHECKS = ["factcheck"]
HAND_WRITTEN = {
    "transcribe": ["outline.json", *GAPS, *IDEAS, "rewrite", "audit.json", *CHECKS, *VOICE,
                   "course", "vault", *DERIVED],
    "outline": ["outline.json", *GAPS, *IDEAS, "rewrite", "audit.json", *CHECKS, *VOICE,
                "course", "vault", *DERIVED],
    "ideas": [*IDEAS, "rewrite", "audit.json", *CHECKS, *VOICE, "course", "vault", *DERIVED],
    "gaps": [*GAPS, "rewrite", "audit.json", *CHECKS, *VOICE, "course", "vault", *DERIVED],
    "research": ["research", "rewrite", "audit.json", *CHECKS, *VOICE, "course", "vault",
                 *DERIVED],
    "rewrite": ["rewrite", "audit.json", *CHECKS, *VOICE, "course", "vault", *DERIVED],
    "spoken": VOICE, "audio": ["audio", "parts"],
    "course": ["course", "vault", *DERIVED]}


class Graph(unittest.TestCase):
    def test_the_declared_graph_is_well_formed(self):
        self.assertEqual(stages.check(), [])

    def test_the_redo_table_matches_the_one_written_by_hand(self):
        table = stages.redo_table()
        # stages added since (slides, recitation, project, documents) only add files after
        # the ones the hand-written table knew
        known = {f for files in HAND_WRITTEN.values() for f in files}
        self.assertLessEqual(set(HAND_WRITTEN), set(table))
        for name, files in HAND_WRITTEN.items():
            self.assertEqual(sorted(f for f in table[name] if f in known), sorted(files), name)
            self.assertEqual(len(table[name]), len(set(table[name])), name)

    def test_the_cli_uses_the_graph(self):
        from lecture2audio.cli import LATER
        self.assertEqual(LATER, stages.redo_table())

    def test_a_correction_of_the_transcript_survives_redo_transcribe(self):
        self.assertNotIn("transcript.txt", stages.discards("transcribe"))
        self.assertEqual(stages.downstream("transcribe")[:2], ["transcribe", "outline"])

    def test_faults_of_a_graph_are_found(self):
        broken = (Stage("a", ("x.json",)), Stage("b", ("x.json",), ("c",)),
                  Stage("c", ("y.json",), ("a",)), Stage("a", ("z.json",)))
        errors = stages.check(broken)
        self.assertEqual(len(errors), 3, errors)
        self.assertTrue(any("needs c" in e for e in errors))
        self.assertTrue(any("x.json is written by both a and b" in e for e in errors))
        self.assertTrue(any("declared twice" in e for e in errors))

    def test_mermaid_has_one_edge_per_need(self):
        text = stages.mermaid()
        self.assertTrue(text.startswith("flowchart LR"))
        self.assertIn("  ideas --> rewrite", text)
        self.assertEqual(text.count("-->"), sum(len(s.needs) for s in stages.STAGES))


class Status(unittest.TestCase):
    def test_each_stage_is_reused_computed_rebuilt_or_not_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = pathlib.Path(tmp)
            (work / "outline.json").write_text("{}")
            (work / "rewrite").mkdir()  # empty: an interrupted run, not a result
            before = stages.present(work)
            self.assertEqual(before, {"outline"})
            (work / "rewrite" / "part_001.json").write_text("{}")
            (work / "ideas.csv").write_text("idea_id\n")
            status = stages.statuses(work, before, {"transcribe": "read from the input file"})
        self.assertEqual(status["outline"], stages.REUSED)
        self.assertEqual(status["rewrite"], stages.COMPUTED)
        self.assertEqual(status["ledger"], stages.REBUILT)
        self.assertEqual(status["audio"], stages.NOT_RUN)
        self.assertEqual(status["transcribe"], "read from the input file")
        text = "\n".join(stages.report_lines(status))
        self.assertIn("## Stages of this run", text)
        self.assertIn("| rewrite | outline, gaps, research, ideas | computed in this run |", text)


if __name__ == "__main__":
    unittest.main()
