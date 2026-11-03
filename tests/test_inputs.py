import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from lecture2audio.assemble import chunk_text
from lecture2audio.llm import dry_run_handler, extract_json
from lecture2audio.segment import split_units
from lecture2audio.sources import detect_language, load_glossary, load_transcript, select_slides


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
