"""Reading the inputs: transcript, glossary, slides (PDF, PowerPoint, text), a folder that
holds them; language detection."""
from __future__ import annotations

import math
import re
from pathlib import Path

from . import ocr

AUDIO_EXT = {".mp3", ".m4a", ".wav", ".flac", ".ogg", ".webm", ".mp4", ".mov", ".mkv", ".aac"}
TRANSCRIPT_EXT = (".srt", ".vtt", ".txt", ".md")  # in order of preference
SLIDE_EXT = (".pdf", ".pptx", ".md", ".txt")  # in order of preference
_GLOSSARY_NAME = re.compile(r"gloss|lexi|terms?\b|termin|vocab", re.I)
_SLIDE_NAME = re.compile(r"slide|diapo|deck|present|cours[-_ ]?notes|handout", re.I)
_TRANSCRIPT_NAME = re.compile(r"transcri|caption|subtit|sous[-_ ]?titre", re.I)
PREFIX_MIN = 5  # shortest line that may be covered by a longer line beginning with it


def slugify(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").lower()
    return slug or "lecture"


def discover_inputs(folder: str | Path) -> dict:
    """The inputs of a lecture found in one folder, by extension and name, so that
    `lecture2audio ./course_folder/` needs no flag:

    - glossary: a .txt, .md or .csv file whose name says glossary, terms, lexique, vocabulary;
    - slides: a .pdf or .pptx file, or a .md/.txt file whose name says slides, deck, diapo;
    - transcript: a .srt, .vtt, .txt or .md file (a name saying transcript wins, then the
      largest); it is preferred to a recording, because it needs no speech recognition;
    - recording: else the largest audio or video file (.m4a .mp3 .wav .mp4 ...).

    Returns {"input", "slides", "glossary", "skipped"}: paths or None, and the candidates that
    were not chosen, so the caller can say which file it used."""
    root = Path(folder)
    files = sorted(f for f in root.iterdir() if f.is_file() and not f.name.startswith((".", "~$")))
    suffix = {f: f.suffix.lower() for f in files}
    glossary = [f for f in files if suffix[f] in (".txt", ".md", ".csv")
                and _GLOSSARY_NAME.search(f.stem)]
    rest = [f for f in files if f not in glossary]
    slides = [f for f in rest if suffix[f] in (".pdf", ".pptx")
              or (suffix[f] in (".md", ".txt") and _SLIDE_NAME.search(f.stem))]
    rest = [f for f in rest if f not in slides]
    transcripts = [f for f in rest if suffix[f] in TRANSCRIPT_EXT]
    audio = [f for f in rest if suffix[f] in AUDIO_EXT]

    def best(candidates, order):
        return sorted(candidates, key=lambda f: (order.index(suffix[f]) if suffix[f] in order
                                                 else len(order),
                                                 not _TRANSCRIPT_NAME.search(f.stem),
                                                 -f.stat().st_size))

    transcripts = best(transcripts, list(TRANSCRIPT_EXT))
    audio = sorted(audio, key=lambda f: -f.stat().st_size)
    slides = best(slides, list(SLIDE_EXT))
    chosen = (transcripts or audio or [None])[0]
    found = {"input": chosen, "slides": slides[0] if slides else None,
             "glossary": glossary[0] if glossary else None}
    found["skipped"] = [f for f in transcripts + audio + slides + glossary
                        if f not in found.values()]
    return found


def _strip_subtitles(raw: str) -> str:
    lines = []
    for line in raw.splitlines():
        s = line.strip()
        if not s or s.upper().startswith("WEBVTT") or "-->" in s or re.fullmatch(r"\d+", s):
            continue
        lines.append(re.sub(r"<[^>]+>", "", s))
    return " ".join(lines)


def load_transcript(path: str | Path) -> str:
    p = Path(path)
    raw = p.read_text(encoding="utf-8", errors="replace")
    if p.suffix.lower() in (".srt", ".vtt"):
        return _strip_subtitles(raw).strip()
    return raw.strip()


def load_glossary(path: str | Path | None) -> str:
    if not path:
        return ""
    lines = [l.rstrip() for l in Path(path).read_text(encoding="utf-8").splitlines()]
    return "\n".join(l for l in lines if l.strip() and not l.lstrip().startswith("#"))


def merge_overlays(pages: list[str]) -> tuple[list[str], list[int]]:
    """Animation steps of a slide deck exported as one page per step (LaTeX Beamer overlays,
    PowerPoint builds): a page whose content is all on the next page is one step of that
    slide, and only the last step, the complete slide, is kept. The earlier page becomes an
    empty string, so the page numbers of the others do not move and `select_slides` skips it.

    A line counts as on the next page when it is there as it is, or when a longer line there
    begins with it (a table row filled in step by step: "add t2, t0, t1" then "add t2, t0, t1
    0xE0 240 + (-16) = 224"). A page is dropped only when every one of its lines is covered, so
    a step that replaces text instead of adding to it is kept, and nothing is lost. Returns the
    pages and the numbers (from 1) of those emptied."""
    lines = [[line.strip() for line in page.splitlines() if line.strip()] for page in pages]

    def covered(earlier: list[str], later: list[str]) -> bool:
        later_set = set(later)
        return all(line in later_set or (len(line) >= PREFIX_MIN
                                         and any(other.startswith(line) for other in later))
                   for line in earlier)

    merged = [k + 1 for k in range(len(pages) - 1) if lines[k] and covered(lines[k], lines[k + 1])]
    return [("" if k + 1 in merged else page) for k, page in enumerate(pages)], merged


def _read_picture(read, key: str, cache: ocr.Cache) -> str:
    """OCR of one picture, from the cache when it was read before. A picture the engine cannot
    decode (a vector format, a damaged file) gives no text rather than stopping the run."""
    try:
        return cache.get(key, read)
    except Exception:
        return ""


def load_slides(path: str | Path | None, notes: list[dict] | None = None,
                cache: str | Path | None = None, use_ocr: bool = True) -> list[str]:
    """One string per slide. PDF needs pypdf, PowerPoint needs python-pptx; text/markdown
    files use '---' lines or form feeds.

    A PDF page with (almost) no text layer, and every picture of a PowerPoint slide, is read
    by OCR when it is installed (`ocr.py`, `uv pip install -e ".[ocr]"`). `notes`, when given,
    receives one {"slide", "how"} for each slide that needed it: "ocr" (text found), "empty"
    (nothing found, even by OCR), "no_ocr" (no text and OCR is not installed) or "overlay"
    (an animation step left out because the next page repeats it, see `merge_overlays`), for
    the report. `cache` is a JSON file that keeps the OCR results between runs."""
    if not path:
        return []
    p = Path(path)
    notes = notes if notes is not None else []
    reading = use_ocr and ocr.available()
    store = ocr.Cache(cache)

    def note(number: int, found: str, existing: str) -> None:
        if found:
            notes.append({"slide": number, "how": "ocr"})
        elif not existing:
            notes.append({"slide": number, "how": "empty" if reading else "no_ocr"})

    if p.suffix.lower() == ".pptx":
        try:
            from pptx import Presentation
            from pptx.enum.shapes import MSO_SHAPE_TYPE
        except ImportError as exc:
            raise SystemExit("Reading a .pptx needs python-pptx (uv pip install "
                             "'python-pptx>=1.0.2'), or export the slides as PDF.") from exc
        slides = []
        for number, slide in enumerate(Presentation(str(p)).slides, 1):
            texts = [shape.text_frame.text.strip() for shape in slide.shapes
                     if shape.has_text_frame and shape.text_frame.text.strip()]
            pictures = [shape for shape in slide.shapes
                        if shape.shape_type == MSO_SHAPE_TYPE.PICTURE]
            seen = "\n".join(texts)
            found = []
            for picture in pictures if reading else []:
                blob = picture.image.blob
                text = _read_picture(lambda blob=blob: ocr.read_image(blob), ocr.digest(blob),
                                     store)
                if text:
                    found.append(text)
            texts += found
            if pictures and use_ocr:
                note(number, "\n".join(found), seen)
            if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
                spoken = slide.notes_slide.notes_text_frame.text.strip()
                if spoken:
                    texts.append(spoken)
            slides.append("\n".join(texts))
        return slides
    if p.suffix.lower() == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise SystemExit("Reading a PDF needs pypdf (pip install pypdf), "
                             "or give the slides as a .txt/.md file.") from exc
        pages = [(page.extract_text() or "").strip() for page in PdfReader(str(p)).pages]
        key = ocr.digest(p.read_bytes()) if reading else ""
        for index, text in enumerate(pages):
            if len(text) >= ocr.MIN_CHARS or not use_ocr:
                continue
            found = ""
            if reading:
                found = _read_picture(lambda index=index: ocr.read_image(ocr.render_page(p, index)),
                                      f"{key}:{index}", store)
                pages[index] = (text + "\n" + found).strip()
            note(index + 1, found, text)
        pages, merged = merge_overlays(pages)
        notes.extend({"slide": number, "how": "overlay"} for number in merged)
        return pages
    raw = p.read_text(encoding="utf-8", errors="replace")
    return [x.strip() for x in re.split(r"\n-{3,}\n|\f", raw) if x.strip()]


def _tokens(text: str) -> list[str]:
    return re.findall(r"[^\W\d_]{3,}", text.lower())


def select_slides(pages: list[str], segment: str, k: int = 4, max_chars: int = 6000) -> str:
    """The slides that share the most (rare) words with the segment, in slide order."""
    numbered = [(i + 1, p) for i, p in enumerate(pages) if p.strip()]
    if not numbered:
        return ""
    if len(numbered) > k:
        token_sets = [set(_tokens(p)) for _, p in numbered]
        document_frequency: dict[str, int] = {}
        for tokens in token_sets:
            for token in tokens:
                document_frequency[token] = document_frequency.get(token, 0) + 1
        segment_tokens = set(_tokens(segment))
        scores = [
            sum(math.log(1 + len(numbered) / document_frequency[t]) for t in tokens & segment_tokens)
            for tokens in token_sets
        ]
        if max(scores) == 0:
            return ""
        best = sorted(range(len(numbered)), key=lambda i: scores[i], reverse=True)[:k]
        numbered = [numbered[i] for i in sorted(best)]
    text = "\n\n".join(f"[Slide {num}]\n{page}" for num, page in numbered)
    return text[:max_chars]


_FR_WORDS = {"le", "la", "les", "des", "est", "et", "que", "une", "dans", "pour", "pas", "sur",
             "avec", "qui", "nous", "vous", "ce", "cette", "sont", "mais"}
_EN_WORDS = {"the", "and", "is", "of", "to", "that", "in", "for", "with", "this", "are", "you",
             "we", "it", "on", "as", "be"}


def detect_language(text: str) -> str:
    words = re.findall(r"[a-zàâçéèêëîïôûùüÿœ']+", text.lower())[:5000]
    french = sum(w in _FR_WORDS for w in words)
    english = sum(w in _EN_WORDS for w in words)
    return "fr" if french > english else "en"
