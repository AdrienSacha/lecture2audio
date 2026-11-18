"""Optical character recognition for slides that are pictures.

`pypdf` reads only the text layer of a PDF, so a scanned PDF, a photo of printed slides or a
slide pasted in as an image gives an empty page, and the program would ignore it silently.
Pages with (almost) no text layer are therefore rendered to an image and read here, and every
page that stays empty is reported (`report.slide_lines`).

The engine is RapidOCR (PP-OCR models on ONNX Runtime): pure pip, no system program, the
models come inside the wheel, and it runs on a CPU, Apple Silicon included. It reads the text
in an image; it does not describe diagrams. Optional: `uv pip install -e ".[ocr]"`. Heavy
packages are imported inside the functions that need them.
"""
from __future__ import annotations

import hashlib
import json
import logging
from importlib.util import find_spec
from pathlib import Path

MIN_CHARS = 20  # a page with less text than this has no usable text layer
MIN_SCORE = 0.6  # lines the engine is less sure of are dropped: noise from logos and photos
RENDER_SCALE = 2.0  # a PDF page is drawn at 144 dpi, enough for slide text

_ENGINE = None


def available() -> bool:
    """Whether the OCR engine and the PDF renderer are installed."""
    return all(find_spec(m) for m in ("rapidocr", "onnxruntime", "pypdfium2"))


def _engine():
    global _ENGINE
    if _ENGINE is None:
        from rapidocr import RapidOCR
        _ENGINE = RapidOCR()
        logging.getLogger("RapidOCR").setLevel(logging.ERROR)  # one line per model otherwise
    return _ENGINE


def read_image(image) -> str:
    """The text of an image (a path, bytes or a BGR array), one line per line of text, in
    reading order. Empty when nothing is found."""
    result = _engine()(image)
    texts, scores = result.txts or (), result.scores or ()
    return "\n".join(t.strip() for t, s in zip(texts, scores) if s >= MIN_SCORE and t.strip())


def render_page(path: Path, index: int):
    """A page of a PDF as a BGR array, for `read_image`."""
    import pypdfium2
    document = pypdfium2.PdfDocument(str(path))
    try:
        return document[index].render(scale=RENDER_SCALE).to_numpy()
    finally:
        document.close()


class Cache:
    """OCR results kept in one JSON file, by the content of what was read, so that a second run
    on the same slides does not read them again (about two seconds a page)."""

    def __init__(self, path: Path | None):
        self.path = Path(path) if path else None
        self.data: dict[str, str] = {}
        if self.path and self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self.data = {}

    def get(self, key: str, read) -> str:
        if key not in self.data:
            self.data[key] = read()
            if self.path:
                self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=1),
                                     encoding="utf-8")
        return self.data[key]


def digest(content: bytes) -> str:
    return hashlib.sha1(content).hexdigest()
