import os
os.environ["L2A_KEYS_FILE"] = "off"  # never read or write the real key file
os.environ["L2A_LIBRARY"] = os.path.join(  # never read or write the real library
    __import__("tempfile").gettempdir(), "l2a-tests-no-library", "library.db")
import contextlib
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from lecture2audio import ocr
from lecture2audio.cli import main
from lecture2audio.report import slide_lines
from lecture2audio.sources import load_slides

try:
    from pypdf import PdfReader, PdfWriter
except ImportError:  # the slide readers need pypdf; so do these tests
    PdfReader = PdfWriter = None
try:
    from PIL import Image, ImageDraw
except ImportError:
    Image = None

SAMPLE = pathlib.Path(__file__).resolve().parents[1] / "examples" / "sample_transcript.txt"
TEXT = "Link-state routing: every router knows the whole graph."


def text_pdf(text: str) -> bytes:
    """A one-page PDF with a real text layer, written by hand (no PDF library needed)."""
    stream = f"BT /F1 12 Tf 20 100 Td ({text}) Tj ET".encode()
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
               b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 400 200] /Contents 4 0 R "
               b"/Resources << /Font << /F1 5 0 R >> >> >>",
               b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = b"%PDF-1.4\n", []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    return out + b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1, xref)


def mixed_pdf(path: pathlib.Path) -> pathlib.Path:
    """Page 1 has text; page 2 is a picture only (no text layer)."""
    first = path.with_name("text.pdf")
    first.write_bytes(text_pdf(TEXT))
    writer = PdfWriter()
    writer.append(PdfReader(str(first)))
    writer.add_blank_page(width=400, height=200)
    with open(path, "wb") as handle:
        writer.write(handle)
    return path


def fake_ocr(text="Scanned: the path u-x-y-z costs 4"):
    """OCR stand-ins: the engine is installed, reads `text`, and a page renders to nothing."""
    return (mock.patch.object(ocr, "available", return_value=True),
            mock.patch.object(ocr, "render_page", return_value=object()),
            mock.patch.object(ocr, "read_image", return_value=text))


@unittest.skipIf(PdfWriter is None, "pypdf missing")
class PdfPages(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.pdf = mixed_pdf(pathlib.Path(self.tmp.name) / "slides.pdf")

    def test_a_page_without_text_is_read_by_ocr_and_a_page_with_text_is_not(self):
        notes = []
        available, render, read = fake_ocr()
        with available, render, read as reader:
            slides = load_slides(self.pdf, notes)
        self.assertEqual(slides, [TEXT, "Scanned: the path u-x-y-z costs 4"])
        self.assertEqual(notes, [{"slide": 2, "how": "ocr"}])
        self.assertEqual(reader.call_count, 1)  # page 1 has a text layer

    def test_the_second_run_reads_the_cache_not_the_page(self):
        cache = pathlib.Path(self.tmp.name) / "slides_ocr.json"
        available, render, read = fake_ocr()
        with available, render, read:
            first = load_slides(self.pdf, [], cache)
        with mock.patch.object(ocr, "available", return_value=True), \
                mock.patch.object(ocr, "render_page", side_effect=AssertionError("not again")), \
                mock.patch.object(ocr, "read_image", side_effect=AssertionError("not again")):
            notes = []
            second = load_slides(self.pdf, notes, cache)
        self.assertEqual(second, first)
        self.assertEqual(notes, [{"slide": 2, "how": "ocr"}])
        self.assertEqual(len(json.loads(cache.read_text())), 1)

    def test_a_page_that_stays_empty_is_reported(self):
        notes = []
        available, render, read = fake_ocr("")
        with available, render, read:
            slides = load_slides(self.pdf, notes)
        self.assertEqual(slides, [TEXT, ""])
        self.assertEqual(notes, [{"slide": 2, "how": "empty"}])

    def test_without_the_engine_the_page_is_reported_and_the_run_goes_on(self):
        notes = []
        with mock.patch.object(ocr, "available", return_value=False):
            slides = load_slides(self.pdf, notes)
        self.assertEqual(slides, [TEXT, ""])
        self.assertEqual(notes, [{"slide": 2, "how": "no_ocr"}])
        notes = []
        available, render, read = fake_ocr()
        with available, render, read as reader:  # --no-ocr: no reading, nothing to report
            self.assertEqual(load_slides(self.pdf, notes, use_ocr=False), [TEXT, ""])
        self.assertEqual((notes, reader.call_count), ([], 0))

    def test_a_page_that_cannot_be_read_does_not_stop_the_run(self):
        notes = []
        with mock.patch.object(ocr, "available", return_value=True), \
                mock.patch.object(ocr, "render_page", side_effect=RuntimeError("damaged")):
            slides = load_slides(self.pdf, notes)
        self.assertEqual(slides, [TEXT, ""])
        self.assertEqual(notes, [{"slide": 2, "how": "empty"}])


class Engine(unittest.TestCase):
    def test_lines_the_engine_is_unsure_of_are_dropped(self):
        result = mock.Mock(txts=("Real line", "x", "  ", "Another line"), scores=(0.99, 0.30, 0.9, 0.8))
        with mock.patch.object(ocr, "_engine", return_value=lambda image: result):
            self.assertEqual(ocr.read_image(b"image"), "Real line\nAnother line")
        nothing = mock.Mock(txts=None, scores=None)
        with mock.patch.object(ocr, "_engine", return_value=lambda image: nothing):
            self.assertEqual(ocr.read_image(b"image"), "")

    @unittest.skipUnless(Image and ocr.available() and PdfWriter, "OCR engine or Pillow missing")
    def test_a_scanned_pdf_and_a_picture_in_a_pptx_are_really_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Image.new("RGB", (1000, 400), "white")
            draw = ImageDraw.Draw(image)
            try:
                from PIL import ImageFont
                font = ImageFont.load_default(size=48)
            except Exception:
                font = None
            draw.text((40, 60), "Link-state routing", fill="black", font=font)
            draw.text((40, 180), "The path u-x-y-z costs 4", fill="black", font=font)
            scan = pathlib.Path(tmp) / "scan.pdf"
            image.save(scan)  # an image-only PDF: pypdf finds no text
            self.assertEqual((PdfReader(str(scan)).pages[0].extract_text() or "").strip(), "")
            notes = []
            slides = load_slides(scan, notes)
            self.assertIn("link-state routing", slides[0].lower())
            self.assertIn("costs 4", slides[0])
            self.assertEqual(notes, [{"slide": 1, "how": "ocr"}])
            try:
                from pptx import Presentation
            except ImportError:
                return
            picture = pathlib.Path(tmp) / "slide.png"
            image.save(picture)
            deck = Presentation()
            slide = deck.slides.add_slide(deck.slide_layouts[6])
            slide.shapes.add_picture(str(picture), 0, 0, width=deck.slide_width)
            path = pathlib.Path(tmp) / "deck.pptx"
            deck.save(str(path))
            notes = []
            slides = load_slides(path, notes)
            self.assertIn("link-state routing", slides[0].lower())
            self.assertEqual(notes, [{"slide": 1, "how": "ocr"}])


class Report(unittest.TestCase):
    def test_the_report_says_which_slides_were_read_and_which_were_not(self):
        text = "\n".join(slide_lines([{"slide": 2, "how": "ocr"}, {"slide": 5, "how": "ocr"},
                                      {"slide": 7, "how": "empty"}, {"slide": 9, "how": "no_ocr"},
                                      {"slide": 3, "how": "overlay"}, {"slide": 4, "how": "overlay"}]))
        self.assertIn("2 page(s) repeated the next page with fewer lines (animation steps", text)
        self.assertIn("## Slides", text)
        self.assertIn("Slides 2, 5 had no text layer and were read by optical character "
                      "recognition.", text)
        self.assertIn("Slide 7 returned no text, even by OCR", text)
        self.assertIn("Its content is not used.", text)
        self.assertIn("Slide 9 has no text and OCR is not installed", text)
        self.assertIn('uv pip install -e ".[ocr]"', text)
        self.assertEqual(slide_lines([]), [])
        self.assertEqual(slide_lines(None), [])


@unittest.skipIf(PdfWriter is None, "pypdf missing")
class EndToEnd(unittest.TestCase):
    def test_the_command_line_reads_scanned_slides_and_reports_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            pdf = mixed_pdf(root / "slides.pdf")
            out = root / "out"
            env = {k: v for k, v in os.environ.items() if k != "L2A_VAULT_DIR"}
            available, render, read = fake_ocr()
            with mock.patch.dict(os.environ, env, clear=True), available, render, read:
                with contextlib.redirect_stdout(io.StringIO()) as log, \
                        self.assertRaises(SystemExit) as raised:
                    main(["text", str(SAMPLE), "--slides", str(pdf), "--llm", "dry",
                          "--language", "en", "--max-part-words", "60", "--out", str(out),
                          "--no-open-obsidian"])
            self.assertEqual(raised.exception.code, 0)
            self.assertIn("[slides] ocr: 2", log.getvalue())
            report = (out / "sample-transcript" / "report.md").read_text(encoding="utf-8")
            self.assertIn("Slide 2 had no text layer and was read by optical character "
                          "recognition.", report)
            self.assertTrue((out / "sample-transcript" / "slides_ocr.json").exists())


    def test_merged_and_unreadable_pages_do_not_break_the_gap_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            page = root / "page.pdf"
            page.write_bytes(text_pdf(TEXT))
            writer = PdfWriter()
            for _ in range(2):  # two identical pages: an animation step and its result
                writer.append(PdfReader(str(page)))
            writer.add_blank_page(width=400, height=200)  # a page nothing can read
            pdf = root / "deck.pdf"
            with open(pdf, "wb") as handle:
                writer.write(handle)
            out = root / "out"
            env = {k: v for k, v in os.environ.items() if k != "L2A_VAULT_DIR"}
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(ocr, "available", return_value=False):
                with contextlib.redirect_stdout(io.StringIO()) as log, \
                        self.assertRaises(SystemExit) as raised:
                    main(["text", str(SAMPLE), "--slides", str(pdf), "--llm", "dry",
                          "--language", "en", "--max-part-words", "60", "--out", str(out),
                          "--no-open-obsidian"])
            self.assertEqual(raised.exception.code, 0)
            self.assertNotIn("no valid answer", log.getvalue())
            report = (out / "sample-transcript" / "report.md").read_text(encoding="utf-8")
            self.assertIn("1 page(s) repeated the next page", report)
            self.assertIn("Slide 3 has no text and OCR is not installed", report)


if __name__ == "__main__":
    unittest.main()
