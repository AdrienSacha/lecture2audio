import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from lecture2audio.cli import main
from lecture2audio.course_render import merge_terms, write_vault
from lecture2audio.series import load_lecture, merge_termbases
from test_course import TERM, lecture, stage


def termbase(path, *terms):
    path.mkdir(parents=True, exist_ok=True)
    (path / "termbase.json").write_text(json.dumps({"terms": list(terms), "error": None}))


def entry(term, definition, part=1):
    return {"term": term, "definition": definition, "depends_on": [], "confusable": [],
            "part": part, "define_at": f"{part}", "in_lecture": True}


class Merge(unittest.TestCase):
    def test_the_first_lecture_owns_a_term_and_the_later_ones_borrow_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b = pathlib.Path(tmp, "a"), pathlib.Path(tmp, "b")
            termbase(a, entry("window", "The packets in flight."))
            termbase(b, entry("Window", "The packets in flight."), entry("timeout", "A delay.", 2))
            self.assertEqual(merge_termbases([a, b], ["a", "b"]), [])
            terms = json.loads((b / "termbase.json").read_text())["terms"]
            self.assertEqual(terms[0]["part"], 0)
            self.assertEqual(terms[0]["from_series"], "a")
            self.assertEqual(terms[1]["part"], 2)  # not shared: stays where it is defined
            self.assertNotIn("from_series", json.loads((a / "termbase.json").read_text())["terms"][0])

    def test_two_different_definitions_are_a_conflict_and_the_first_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b = pathlib.Path(tmp, "a"), pathlib.Path(tmp, "b")
            termbase(a, entry("rate", "Bits per second."))
            termbase(b, entry("rate", "Packets per second."))
            conflicts = merge_termbases([a, b], ["a", "b"])
            self.assertEqual([(c["term"], c["kept"], c["dropped"]) for c in conflicts],
                             [("rate", "a", "b")])
            terms = json.loads((b / "termbase.json").read_text())["terms"]
            self.assertEqual(terms[0]["definition"], "Bits per second.")

    def test_merging_twice_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b = pathlib.Path(tmp, "a"), pathlib.Path(tmp, "b")
            termbase(a, entry("rate", "Bits per second."))
            termbase(b, entry("rate", "Packets per second."))
            merge_termbases([a, b], ["a", "b"])
            before = (b / "termbase.json").read_text()
            self.assertEqual(merge_termbases([a, b], ["a", "b"]), [])
            self.assertEqual((b / "termbase.json").read_text(), before)


class Vault(unittest.TestCase):
    def two(self):
        first = stage(terms=[TERM])
        second = stage(terms=[dict(TERM, part=0, from_series="one")])
        for run in (first, second):
            self.addCleanup(run[4].cleanup)
        return [lecture(first[0], first[1], first[2], [TERM], "one"),
                lecture(second[0], second[1], second[2], [dict(TERM, part=0, from_series="one")],
                        "two")]

    def test_the_owner_lecture_defines_the_term_and_both_use_one_note(self):
        lectures = self.two()
        self.assertEqual(len(merge_terms(lectures)), 1)
        self.assertEqual(merge_terms(lectures)[0]["lecture"], 0)
        with tempfile.TemporaryDirectory() as tmp:
            stats = write_vault(pathlib.Path(tmp), lectures, "en", "Networks")
            root = pathlib.Path(tmp)
            self.assertTrue((root / "one" / "one – 01 Sending.md").exists())
            self.assertTrue((root / "two" / "two – 01 Sending.md").exists())
            self.assertEqual(sorted(p.name for p in (root / "Terms").glob("*.md")), ["window.md"])
            course = (root / "00 Course.md").read_text()
            self.assertIn("[[one – Index]]", course)
            self.assertIn("[[two – Index]]", course)
            note = (root / "Terms" / "window.md").read_text()
            self.assertIn("[[one – 01 Sending#", note)  # defined there
            self.assertEqual(stats["terms"], 1)

    def test_a_borrowing_lecture_does_not_define_the_term_again(self):
        run = stage(terms=[dict(TERM, part=0, from_series="one")])
        self.addCleanup(run[4].cleanup)
        for part in run[2]["parts"]:
            self.assertTrue(all(not c["terms"] for c in part["cards"]))

    def test_the_notation_of_a_lecture_is_read_back_from_its_caches(self):
        run = stage(terms=[TERM])
        self.addCleanup(run[4].cleanup)
        loaded = load_lecture(pathlib.Path(run[4].name), "x")
        self.assertEqual(len(loaded["course"]["parts"]), 2)
        self.assertEqual(len(loaded["records"]), 2)
        self.assertEqual(loaded["terms"], [])


class Series(unittest.TestCase):
    def test_the_series_command_builds_one_vault_from_two_lectures(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = pathlib.Path(tmp, "in")
            src.mkdir()
            for name in ("a.txt", "b.txt"):
                (src / name).write_text(pathlib.Path("examples/sample_transcript.txt").read_text())
            out, vault = pathlib.Path(tmp, "out"), pathlib.Path(tmp, "obsidian")
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    main(["series", str(src / "a.txt"), str(src / "b.txt"), "--llm", "dry",
                          "--name", "net", "--max-part-words", "60", "--out", str(out),
                          "--vault-dir", str(vault)])
            self.assertEqual(raised.exception.code, 0)
            self.assertTrue((vault / "net" / "00 Course.md").exists())
            self.assertTrue((vault / "net" / "a").is_dir() and (vault / "net" / "b").is_dir())
            self.assertTrue((out / "net" / "series.md").exists())
            self.assertFalse((out / "net" / "vault").exists())  # written to --vault-dir only


if __name__ == "__main__":
    unittest.main()
