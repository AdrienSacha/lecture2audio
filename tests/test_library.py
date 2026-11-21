"""The reference library: ingestion, search, checked quotes from books, and their use in research."""
import contextlib
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

TEMP = tempfile.TemporaryDirectory()
os.environ["L2A_LIBRARY"] = os.path.join(TEMP.name, "default", "library.db")  # never the real one
os.environ["L2A_KEYS_FILE"] = "off"

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from lecture2audio import cli, enrich, library, llm
from lecture2audio.library import Library
from lecture2audio.llm import FakeLLM, _between
from lecture2audio.pipeline import TextPipeline, load_prompts
from lecture2audio.report import write_reference

from test_llm import FakeServer, chat_stream
from test_pipeline import UNITS, silent

PAGES = [
    "Chapter 3. Reliable transport. A retransmission timeout is the time a sender waits for an "
    "acknowledgement before it sends the segment again.",
    "The retransmission timeout is computed from the smoothed round-trip time and its variation, "
    "so that it adapts to the",
    "path. Congestion control is a different mechanism: it limits the sending rate of a host.",
]


def make_pdf(pages: list[str]) -> bytes:
    """A minimal PDF with one line of Helvetica text per page, readable by pypdf."""
    objects = ["<< /Type /Catalog /Pages 2 0 R >>", None,
               "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for text in pages:
        safe = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = f"BT /F1 9 Tf 20 700 Td ({safe}) Tj ET"
        objects.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources "
                       f"<< /Font << /F1 3 0 R >> >> /Contents {len(objects)} 0 R >>")
        kids.append(f"{len(objects)} 0 R")
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    out, offsets = b"%PDF-1.4\n", []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
            ).encode()
    return out


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = pathlib.Path(tmp.name)
        self.pdf = self.dir / "Computer_Networking.pdf"
        self.pdf.write_bytes(make_pdf(PAGES))
        self.shelf = Library(self.dir / "lib" / "library.db")

    def add(self):
        return self.shelf.add([self.pdf], log=None)[0]


class Ingest(Base):
    def test_add_is_idempotent_by_hash_list_and_remove(self):
        first = self.add()
        self.assertEqual((first["book"], first["title"], first["pages"], first["status"]),
                         ("computer-networking", "Computer Networking", 3, "added"))
        copy = self.dir / "copy.pdf"
        copy.write_bytes(self.pdf.read_bytes())
        again = self.shelf.add([copy], log=None)[0]
        self.assertEqual((again["book"], again["status"]), ("computer-networking", "already there"))
        self.assertEqual([b["book"] for b in self.shelf.list()], ["computer-networking"])
        self.assertTrue(self.shelf.remove("computer-networking"))
        self.assertEqual((self.shelf.list(), self.shelf.search("timeout")), ([], []))
        self.assertFalse(self.shelf.remove("computer-networking"))

    def test_search_ranks_pages_and_read_pages_gives_the_text(self):
        self.add()
        hits = self.shelf.search("retransmission timeout round-trip", 5)
        self.assertEqual(hits[0]["page"], 2)  # the page with the most matching words
        self.assertEqual(hits[0]["source"], "library:computer-networking:p2")
        self.assertIn("smoothed round-trip time", hits[0]["text"])
        self.assertEqual([h["page"] for h in self.shelf.search("congestion")], [3])
        text = self.shelf.read_pages("computer-networking", [3, 1])
        self.assertTrue(text.startswith("[library:computer-networking:p1]\nChapter 3."))
        self.assertIn("[library:computer-networking:p3]\npath.", text)
        # operators and quotes of the query are taken as words, never as FTS5 syntax
        for query in ('timeout AND "', "NOT * (", "", "-"):
            self.shelf.search(query)

    def test_empty_pages_go_through_ocr_when_it_is_installed(self):
        self.pdf.write_bytes(make_pdf(["x", PAGES[0]]))
        with mock.patch.object(library.ocr, "available", return_value=True), \
                mock.patch.object(library.ocr, "render_page", return_value="image") as render, \
                mock.patch.object(library.ocr, "read_image",
                                  return_value="A scanned page about sliding windows."):
            item = self.add()
        render.assert_called_once_with(self.pdf, 0)
        self.assertEqual(item["ocr_pages"], 1)
        self.assertEqual(self.shelf.search("sliding windows")[0]["page"], 1)

    def test_a_missing_or_empty_library_changes_nothing(self):
        self.assertEqual(self.shelf.size(), 0)
        self.assertFalse(self.shelf.file.exists())  # reading never creates the database
        self.assertEqual(library.passages(None, ["timeout"]), [])
        with mock.patch.dict(os.environ, {"L2A_LIBRARY": str(self.shelf.file)}):
            self.assertIsNone(Library.existing())
        with mock.patch.dict(os.environ, {"L2A_LIBRARY": "off"}):
            self.assertIsNone(Library.default())
            self.assertEqual(library.search("timeout"), [])
        self.assertNotEqual(library.path(), pathlib.Path("~/.lecture2audio/library/library.db")
                            .expanduser())


class Facts(Base):
    def fact(self, quote, url="library:computer-networking:p2"):
        return {"text": "The timeout adapts to the round-trip time.", "url": url, "quote": quote}

    def test_a_book_quote_is_checked_against_the_stored_page(self):
        self.add()
        kept, rejected = enrich.check_facts(
            [self.fact("is computed from the smoothed round-trip time and its variation"),
             self.fact("the timeout is always exactly three seconds on every host"),
             self.fact("is computed from the smoothed round-trip time", "library:other-book:p2"),
             # across the page break between pages 2 and 3
             self.fact("so that it adapts to the path. Congestion control is a different")],
            [], fetch=lambda url: self.fail("a book page is never fetched"), library=self.shelf)
        self.assertEqual([k["title"] for k in kept],
                         ["Computer Networking, p. 2", "Computer Networking, p. 2"])
        self.assertEqual(kept[0]["url"], "library:computer-networking:p2")
        self.assertIn("word for word", rejected[0]["reason"])
        self.assertIn("not in the reference library", rejected[1]["reason"])

    def test_without_a_library_a_book_fact_is_rejected(self):
        kept, rejected = enrich.check_facts(
            [self.fact("is computed from the smoothed round-trip time and its variation")], [])
        self.assertEqual(kept, [])
        self.assertIn("reference library", rejected[0]["reason"])


class Loop(Base):
    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "dk"}, clear=False)
        env.start()
        self.addCleanup(env.stop)

    def call(self, i, name, **args):
        return {"id": f"c{i}", "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)}}

    def run_loop(self, turns, shelf):
        server = FakeServer(*[chat_stream(finish="tool_calls" if t.get("tool_calls") else "stop",
                                          **t) for t in turns])
        model = llm.HostedLLM("deepseek", "deepseek-flash", http_client=server.client())
        text, docs = llm.research_loop(model, "sys", "gap", lambda q: [], depth=10,
                                       library=shelf)
        return text, [r["body"] for r in server.requests]

    def test_library_tools_come_first_and_are_run(self):
        self.add()
        text, sent = self.run_loop([
            {"content": None, "reasoning_content": "a",
             "tool_calls": [self.call(1, "search_library", query="retransmission timeout")]},
            {"content": None, "reasoning_content": "b",
             "tool_calls": [self.call(2, "read_pages", book="computer-networking", pages=[2])]},
            {"content": '{"found": false, "facts": []}', "reasoning_content": "c"}], self.shelf)
        self.assertEqual([t["function"]["name"] for t in sent[0]["tools"]],
                         ["search_library", "read_pages", "web_search", "fetch_page"])
        hits = json.loads(sent[1]["messages"][3]["content"])
        self.assertEqual(hits[0]["source"], "library:computer-networking:p2")
        self.assertIn("[library:computer-networking:p2]\nThe retransmission timeout",
                      sent[2]["messages"][5]["content"])
        self.assertEqual(text, '{"found": false, "facts": []}')

    def test_an_empty_library_offers_only_the_web(self):
        _, sent = self.run_loop([{"content": "{}"}], self.shelf)
        self.assertEqual([t["function"]["name"] for t in sent[0]["tools"]],
                         ["web_search", "fetch_page"])


class Pipeline(Base):
    """A gap filled from a book, checked, taught and cited as "Title, p. N"."""

    def handler(self, system, user):
        from test_gaps import Loop as GapLoop
        if "You research one gap" in system:
            found = "retransmission" in _between(user, "GAP")
            facts = [{"text": "The timeout is computed from the round-trip time.",
                      "url": "library:computer-networking:p2",
                      "quote": "is computed from the smoothed round-trip time and its variation"}]
            return json.dumps({"found": found, "facts": facts if found else []})
        return GapLoop().handler()(system, user)

    def go(self, shelf, parts=True):
        fake = FakeLLM(self.handler)
        with tempfile.TemporaryDirectory() as tmp:
            p = TextPipeline(pathlib.Path(tmp), "en", fake, fake, load_prompts("en"), log=silent,
                             max_part_words=1000, workers=1, researcher=fake.research,
                             library=shelf)
            o = p.run_outline(UNITS)
            p.run_coverage(o)
            p.run_termbase(o)
            p.run_research(o)
            if not parts:  # the scripted rewrite cites E1, which exists only with the book
                return p, [], fake, ""
            records = p.run_parts(o)
            write_reference(pathlib.Path(tmp) / "reference.md", "x", o, records)
            reference = (pathlib.Path(tmp) / "reference.md").read_text(encoding="utf-8")
            return p, records, fake, reference

    def test_book_fact_is_researched_checked_taught_and_cited(self):
        self.add()
        p, records, fake, reference = self.go(self.shelf)
        self.assertEqual([(s["id"], s["title"]) for s in p.supplements],
                         [("E1", "Computer Networking, p. 2")])
        self.assertFalse(records[0]["flagged"], records[0]["issues"])
        research = next(u for s, u in fake.calls if "You research one gap" in s)
        self.assertIn("[library:computer-networking:p2] Computer Networking, p. 2",
                      _between(research, "LIBRARY"))
        termbase = next(u for s, u in fake.calls if "You build the termbase" in s)
        self.assertIn("(timeout)", _between(termbase, "REFERENCE"))
        self.assertIn("[E1] Computer Networking, p. 2. Supporting passage", reference)
        self.assertNotIn("<library:", reference)

    def test_without_a_library_the_requests_do_not_change(self):
        p, _, fake, _ = self.go(None, parts=False)
        self.assertFalse(any("## LIBRARY" in u or "## REFERENCE" in u for _, u in fake.calls))
        self.assertEqual(p.supplements, [])  # the book fact is rejected: no library


class Command(Base):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"L2A_LIBRARY": str(self.shelf.file)}), \
                mock.patch.object(cli.keys, "load", side_effect=AssertionError("keys read")), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                self.assertRaises(SystemExit) as stop:
            cli.main(["library", *argv])
        return stop.exception.code, out.getvalue(), err.getvalue()

    def test_add_list_search_remove(self):
        code, out, _ = self.run_cli("add", str(self.pdf), "--title", "Kurose and Ross")
        self.assertEqual(code, 0)
        self.assertIn('kurose-and-ross: "Kurose and Ross", 3 page(s) (added)', out)
        self.assertIn("(already there)", self.run_cli("add", str(self.pdf))[1])
        self.assertIn("1 book(s)", self.run_cli("list")[1])
        code, out, _ = self.run_cli("search", "congestion control")
        self.assertIn("[library:kurose-and-ross:p3] Kurose and Ross, p. 3", out)
        self.assertEqual(self.run_cli("remove", "kurose-and-ross")[0], 0)
        code, _, err = self.run_cli("remove", "kurose-and-ross")
        self.assertEqual(code, 1)
        self.assertIn("no book with the id", err)
        code, _, err = self.run_cli("add", str(self.dir / "notes.txt"))
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
