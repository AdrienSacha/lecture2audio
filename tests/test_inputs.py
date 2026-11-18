import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from lecture2audio.assemble import chunk_text
from lecture2audio.llm import dry_run_handler, extract_json
from lecture2audio.segment import split_units
from lecture2audio.sources import (detect_language, discover_inputs, load_glossary,
                                   load_transcript, merge_overlays, select_slides)


def words(n, prefix="w"):
    return " ".join(f"{prefix}{i}" for i in range(n))


class Chunking(unittest.TestCase):
    def test_chunks_respect_the_limit_and_keep_all_words(self):
        text = " ".join(f"Sentence number {i} says something useful." for i in range(60))
        chunks = chunk_text(text, max_chars=200)
        self.assertTrue(all(len(c) <= 200 for c in chunks))
        self.assertEqual(" ".join(chunks).split(), text.split())

    def test_a_very_long_sentence_is_cut_at_word_ends(self):
        text = " ".join(["longword"] * 100)
        chunks = chunk_text(text, max_chars=100)
        self.assertTrue(all(len(c) <= 100 for c in chunks))
        self.assertEqual(" ".join(chunks), text)


class Sources(unittest.TestCase):
    def test_srt_is_reduced_to_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "a.srt"
            path.write_text("1\n00:00:00,000 --> 00:00:02,000\nHello there.\n\n"
                            "2\n00:00:02,000 --> 00:00:04,000\n<i>General</i> Kenobi.\n", encoding="utf-8")
            self.assertEqual(load_transcript(path), "Hello there. General Kenobi.")

    def test_glossary_skips_comments_and_blank_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "g.txt"
            path.write_text("# course terms\nTCP\n\nsubnet mask: splits an address\n", encoding="utf-8")
            self.assertEqual(load_glossary(path), "TCP\nsubnet mask: splits an address")

    def test_slide_selection_prefers_matching_slides(self):
        pages = ["tcp handshake syn ack flags", "udp datagram checksum", "routing table ospf cost",
                 "dns resolver cache ttl", "http request methods get post"]
        out = select_slides(pages, "the syn and ack flags open a tcp handshake", k=2)
        self.assertIn("[Slide 1]", out)
        self.assertNotIn("[Slide 3]", out)

    def test_slide_selection_returns_nothing_when_nothing_matches(self):
        pages = [f"alpha{i} beta{i}" for i in range(6)]
        self.assertEqual(select_slides(pages, "completely unrelated words here", k=2), "")

    def test_language_detection(self):
        self.assertEqual(detect_language("Alors le paquet est envoyé dans le réseau et il passe par une passerelle"), "fr")
        self.assertEqual(detect_language("So the packet is sent into the network and it goes through a gateway"), "en")


class Folder(unittest.TestCase):
    def make(self, tmp, *names):
        root = pathlib.Path(tmp)
        for name in names:
            (root / name).write_bytes(b"x" * (100 if name.endswith((".m4a", ".mp4")) else 10))
        return root

    def test_a_folder_gives_transcript_slides_and_glossary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make(tmp, "lecture.m4a", "lecture.srt", "slides.pdf", "glossary.txt",
                             ".DS_Store", "notes.docx")
            found = discover_inputs(root)
        self.assertEqual(found["input"].name, "lecture.srt")  # a transcript needs no recognition
        self.assertEqual(found["slides"].name, "slides.pdf")
        self.assertEqual(found["glossary"].name, "glossary.txt")
        self.assertEqual([f.name for f in found["skipped"]], ["lecture.m4a"])

    def test_a_recording_alone_is_the_input_and_markdown_slides_are_recognised(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make(tmp, "week3.mp4", "week3-slides.md", "terms.md")
            found = discover_inputs(root)
        self.assertEqual(found["input"].name, "week3.mp4")
        self.assertEqual(found["slides"].name, "week3-slides.md")
        self.assertEqual(found["glossary"].name, "terms.md")

    def test_pptx_slides_win_over_text_slides_and_an_empty_folder_has_no_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make(tmp, "talk.txt", "deck.pptx", "slides.txt")
            found = discover_inputs(root)
            self.assertEqual(found["input"].name, "talk.txt")
            self.assertEqual(found["slides"].name, "deck.pptx")
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(discover_inputs(tmp)["input"])


class Overlays(unittest.TestCase):
    """A deck exported as one page per animation step lists each slide several times."""

    FOOTER = "S. Taktak OSCA 14/44"

    def page(self, *lines):
        return "\n".join([*lines, self.FOOTER])

    def test_a_step_that_adds_lines_is_merged_into_the_last_step(self):
        pages = [self.page("Title", "add"), self.page("Title", "add", "sub"),
                 self.page("Title", "add", "sub", "and"), self.page("Other slide")]
        merged, numbers = merge_overlays(pages)
        self.assertEqual(numbers, [1, 2])
        self.assertEqual(merged, ["", "", pages[2], pages[3]])  # page numbers do not move

    def test_a_table_row_filled_in_step_by_step_is_merged(self):
        pages = [self.page("Results", "add t2, t0, t1", "sub t2, t0, t1"),
                 self.page("Results", "add t2, t0, t1 0xE0 240 + (-16) = 224", "sub t2, t0, t1"),
                 self.page("Results", "add t2, t0, t1 0xE0 240 + (-16) = 224",
                           "sub t2, t0, t1 0x100 240 - (-16) = 256")]
        self.assertEqual(merge_overlays(pages)[1], [1, 2])

    def test_a_step_that_replaces_text_is_kept_so_nothing_is_lost(self):
        pages = [self.page("Question", "What is 2 + 2?"), self.page("Question", "The answer is 4"),
                 self.page("Identical", "same"), self.page("Identical", "same")]
        merged, numbers = merge_overlays(pages)
        self.assertEqual(numbers, [3])  # only the exact repeat
        self.assertEqual(merged[0:2], pages[0:2])

    def test_short_lines_are_matched_exactly_not_as_prefixes(self):
        pages = [self.page("A", "x = 1"), self.page("Another", "x = 1")]
        self.assertEqual(merge_overlays(pages)[1], [])
        self.assertEqual(merge_overlays([]), ([], []))
        self.assertEqual(merge_overlays(["", "text"]), (["", "text"], []))

    def test_the_selection_skips_a_merged_page_and_keeps_the_numbers(self):
        pages = ["Store encoding\nrs2 value", "Store encoding\nrs2 value\nimm split in two",
                 "Branch\nbeq label", "Jump\njal ra"]
        merged, _ = merge_overlays(pages)
        chosen = select_slides(merged, "the store immediate is split in two", k=1)
        self.assertEqual(chosen.splitlines()[0], "[Slide 2]")
        self.assertNotIn("[Slide 1]", chosen)


class JsonExtraction(unittest.TestCase):
    def test_fenced_and_chatty_answers(self):
        self.assertEqual(extract_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(extract_json('Here you go: {"a": {"b": 2}} done'), {"a": {"b": 2}})

    def test_no_json(self):
        with self.assertRaises(ValueError):
            extract_json("sorry, no")

    def test_dry_outline_covers_every_unit(self):
        user = "## LIMITS\nAt most 3 words per part.\n\n## UNITS\n[1] One two.\n[2] Three four.\n[3] Five."
        out = extract_json(dry_run_handler("You build the outline", user))
        self.assertEqual(len(out["parts"]), 2)


class Units(unittest.TestCase):
    def test_sentences_become_units_and_long_ones_are_cut(self):
        text = "First sentence. Second one.\n\n" + " ".join(["word"] * 90)
        units = split_units(text, max_words=40)
        self.assertEqual(units[:2], ["First sentence.", "Second one."])
        self.assertEqual([len(u.split()) for u in units[2:]], [40, 40, 10])


if __name__ == "__main__":
    unittest.main()
