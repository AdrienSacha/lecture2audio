import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from lecture2audio import ledger
from lecture2audio.outline import parse_outline
from lecture2audio.report import ledger_lines
from test_pipeline import LEDGER, OUTLINE, UNITS


def outline():
    result, errors = parse_outline(OUTLINE, UNITS, 1000)
    assert not errors, errors
    return result


def course(*ideas):
    """Course results of two parts: card 1.1 and 2.1, with the judge's verdict on ideas."""
    return {"parts": [
        {"part": "1", "cards": [{"id": "1.1", "title": "The window", "checks": [
            {"question": "What does the window hold?", "answer": "At most $64$ packets."}]},
            {"id": "1.2", "title": "The timeout", "checks": []}],
         "ideas": [i for i in ideas if i["idea"] in ("T1", "T2")]},
        {"part": "2", "cards": [{"id": "2.1", "title": "Acknowledgements", "checks": []}],
         "ideas": [i for i in ideas if i["idea"] == "T3"]}]}


class Seed(unittest.TestCase):
    def test_every_transcript_idea_is_a_pending_row_with_its_units(self):
        rows = ledger.seed(outline(), LEDGER)
        self.assertEqual([r["idea_id"] for r in rows], ["T1", "T2", "T3"])
        self.assertEqual({r["status"] for r in rows}, {"pending"})
        self.assertEqual(rows[0]["source_units"], "1")
        self.assertEqual(rows[1]["source_units"], "3-5")
        self.assertEqual(rows[2]["source_units"], "6-7")
        self.assertEqual(rows[1]["node_id"], "1.2")

    def test_units_cited_by_the_idea_win_over_the_units_of_its_node(self):
        coverage = {"transcript_ideas": [{"id": "T1.2", "kind": "condition", "idea": "x",
                                          "nodes": ["1.2"], "units": [4, 5]}]}
        self.assertEqual(ledger.seed(outline(), coverage)[0]["source_units"], "4-5")

    def test_the_csv_has_the_ledger_columns_and_round_trips(self):
        rows = ledger.seed(outline(), LEDGER)
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "ideas.csv"
            ledger.write(path, rows)
            self.assertEqual(path.read_text().splitlines()[0],
                             "idea_id,node_id,kind,statement,source_units,covered_in_card,status")
            self.assertEqual(ledger.read(path), rows)

    def test_ranges(self):
        self.assertEqual(ledger._ranges([7, 3, 4, 5, 9, 10]), "3-5;7;9-10")
        self.assertEqual(ledger._ranges([]), "")


class Stages(unittest.TestCase):
    def test_the_verifier_then_the_judge_check_off_each_idea(self):
        rows = ledger.seed(outline(), LEDGER)
        records = [{"part": "1", "ideas": [{"idea": "T1", "status": "taught"},
                                           {"idea": "T2", "status": "missing"}]},
                   {"part": "2", "ideas": [{"idea": "T3", "status": "taught"}]}]
        ledger.apply_script(rows, records)
        self.assertEqual([r["status"] for r in rows], ["scripted", "missing_script", "scripted"])
        self.assertEqual([r["idea_id"] for r in ledger.open_rows(rows, False)], ["T2"])
        ledger.apply_cards(rows, course({"idea": "T1", "status": "taught", "cards": ["1.1"]},
                                        {"idea": "T3", "status": "missing", "cards": []}))
        self.assertEqual(rows[0]["covered_in_card"], "1.1")
        self.assertEqual([r["status"] for r in rows], ["verified", "missing_script",
                                                       "missing_cards"])
        self.assertEqual([r["idea_id"] for r in ledger.open_rows(rows, True)], ["T2", "T3"])

    def test_a_judge_and_code_that_disagree_give_a_disputed_idea(self):
        rows = ledger.seed(outline(), LEDGER)
        ledger.apply_script(rows, [{"part": "1", "ideas": [
            {"idea": "T1", "status": "taught", "lost": ["64"]},
            {"idea": "T2", "status": "taught", "lost": []}]}])
        self.assertEqual([r["status"] for r in rows[:2]], ["disputed", "scripted"])
        self.assertEqual([r["idea_id"] for r in ledger.open_rows(rows, False)], ["T1", "T3"])
        ledger.apply_cards(rows, course({"idea": "T1", "status": "taught", "cards": ["1.1"]},
                                        {"idea": "T2", "status": "taught", "cards": ["1.2"],
                                         "lost": ["5"]}))
        self.assertEqual([r["status"] for r in rows[:2]], ["verified", "disputed"])
        self.assertEqual(rows[1]["covered_in_card"], "1.2")

    def test_an_idea_split_over_two_parts_is_scripted_when_one_part_teaches_it(self):
        rows = ledger.seed(outline(), LEDGER)
        records = [{"part": "1", "ideas": [{"idea": "T3", "status": "missing"}]},
                   {"part": "2", "ideas": [{"idea": "T3", "status": "taught"}]}]
        ledger.apply_script(rows, records)
        self.assertEqual(rows[2]["status"], "scripted")

    def test_a_part_without_ideas_is_reported(self):
        rows = ledger.seed(outline(), dict(LEDGER, transcript_ideas=LEDGER["transcript_ideas"][:2]))
        self.assertEqual(ledger.parts_without_ideas(outline(), rows), ["2"])

    def test_the_report_lists_every_idea_that_did_not_arrive(self):
        rows = ledger.seed(outline(), LEDGER)
        rows[0]["status"] = "verified"
        text = "\n".join(ledger_lines({"rows": rows, "course": True, "empty_parts": []}))
        self.assertIn("## Idea ledger (ideas.csv)", text)
        self.assertIn("**2 idea(s) did not reach the cards.**", text)
        self.assertIn("- T2 (condition, node 1.2, units 3-5, not checked)", text)
        self.assertNotIn("- T1 ", text)
        empty = "\n".join(ledger_lines({"rows": [], "course": True}))
        self.assertIn("listed no atomic idea", empty)


class Live(unittest.TestCase):
    def test_the_file_follows_each_part_in_any_order_and_matches_a_full_rebuild(self):
        records = [{"part": "1", "ideas": [{"idea": "T1", "status": "taught"},
                                           {"idea": "T2", "status": "taught"}]},
                   {"part": "2", "ideas": [{"idea": "T3", "status": "missing"}]}]
        cards = course({"idea": "T1", "status": "taught", "cards": ["1.1"]},
                       {"idea": "T2", "status": "missing", "cards": []})
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "ideas.csv"
            book = ledger.Ledger(path, outline(), LEDGER)
            rows = book.rows
            self.assertEqual({r["status"] for r in ledger.read(path)}, {"pending"})
            book.script_done(records[1])  # part 2 finishes first
            self.assertEqual([r["status"] for r in ledger.read(path)],
                             ["pending", "pending", "missing_script"])
            book.script_done(records[0])
            book.cards_done(cards["parts"][0])
            self.assertIs(book.rows, rows)  # callers holding the list see every update
            expected = ledger.apply_cards(ledger.apply_script(ledger.seed(outline(), LEDGER),
                                                              records), {"parts": cards["parts"][:1]})
            self.assertEqual(ledger.read(path), expected)
            self.assertEqual([r["status"] for r in rows],
                             ["verified", "missing_cards", "missing_script"])
            self.assertEqual(sorted(p.name for p in pathlib.Path(tmp).iterdir()), ["ideas.csv"])


class Gate(unittest.TestCase):
    def test_the_idea_list_of_a_part_is_checked_by_code(self):
        from lecture2audio.enrich import unaccounted_units, validate_idea_list
        part = outline().parts[0]
        good = {"ideas": [{"kind": "claim", "idea": "a", "nodes": ["1.1"], "units": [1]},
                          {"kind": "condition", "idea": "b", "nodes": ["1.2"], "units": [3, 4]}],
                "no_content": [{"units": [5], "kind": "no_teaching", "reason": "a joke"}]}
        self.assertEqual(validate_idea_list(good, part), [])
        self.assertEqual(unaccounted_units(good, part), [])
        self.assertEqual(unaccounted_units({"ideas": good["ideas"][:1]}, part), [3, 4, 5])
        self.assertTrue(validate_idea_list({"ideas": [dict(good["ideas"][0], units=[6])]}, part))
        self.assertTrue(any("kind" in e for e in validate_idea_list(
            {"ideas": [dict(good["ideas"][1], kind="x")]}, part)))
        self.assertTrue(validate_idea_list({"items": []}, part))
        self.assertTrue(validate_idea_list(
            {"ideas": [], "no_content": [{"units": [9], "kind": "unclear"}]}, part))


class Anki(unittest.TestCase):
    def test_check_questions_are_tagged_with_their_ideas_and_untested_ideas_get_a_card(self):
        rows = ledger.seed(outline(), LEDGER)
        ledger.apply_cards(rows, course({"idea": "T1", "status": "taught", "cards": ["1.1"]},
                                        {"idea": "T2", "status": "taught", "cards": ["1.2"]}))
        notes = ledger.anki_rows("flow", rows, course(), [])
        self.assertEqual(notes[0][0], "What does the window hold?")
        self.assertEqual(notes[0][1], "At most \\(64\\) packets.")
        self.assertIn("flow::idea-T1", notes[0][2].split())
        self.assertIn("flow::part-01", notes[0][2].split())
        # T2 has a card but no check question: one recall note; T3 is not verified: none
        self.assertEqual(len(notes), 2)
        self.assertIn("The timeout", notes[1][0])
        self.assertEqual(notes[1][1], "a timeout is 5 seconds")

    def test_without_course_material_the_script_questions_are_exported(self):
        records = [{"part": "1", "questions": [{"question": "Q?", "answer": "A."}]}]
        self.assertEqual(ledger.anki_rows("flow", [], None, records),
                         [("Q?", "A.", "flow flow::part-01")])

    def test_the_file_has_the_anki_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "anki.csv"
            count = ledger.write_anki(path, "flow", [], None,
                                      [{"part": "1", "questions": [{"question": "Q, then?",
                                                                    "answer": "A."}]}])
            lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(count, 1)
        self.assertEqual(lines[:4], ["#separator:Comma", "#html:false", "#columns:Front,Back,Tags",
                                     "#tags column:3"])
        self.assertEqual(lines[4], '"Q, then?",A.,flow flow::part-01')


if __name__ == "__main__":
    unittest.main()
