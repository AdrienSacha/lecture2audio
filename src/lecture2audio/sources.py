"""Reading the inputs: transcript, glossary, slides (PDF, PowerPoint, text), a folder that
holds them; language detection."""
from __future__ import annotations

import math
import re
import statistics
from functools import lru_cache
from pathlib import Path

from . import ocr

AUDIO_EXT = {".mp3", ".m4a", ".wav", ".flac", ".ogg", ".webm", ".mp4", ".mov", ".mkv", ".aac"}
TRANSCRIPT_EXT = (".srt", ".vtt", ".txt", ".md", ".pdf")  # in order of preference
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
      a PDF that holds a transcript (`pdf_is_transcript`) is a transcript, not slides;
    - transcript: a .srt, .vtt, .txt or .md file, else a transcript PDF (a name saying
      transcript wins, then the largest); it is preferred to a recording, because it needs no
      speech recognition;
    - recording: else the largest audio or video file (.m4a .mp3 .wav .mp4 ...).

    Returns {"input", "slides", "glossary", "kind", "skipped"}: paths or None, what the
    lecture is built from ("transcript", "recording", "slides" when the folder holds slides
    and nothing spoken, or None), and the candidates that were not chosen, so the caller can
    say which file it used."""
    root = Path(folder)
    files = sorted(f for f in root.iterdir() if f.is_file() and not f.name.startswith((".", "~$")))
    suffix = {f: f.suffix.lower() for f in files}
    glossary = [f for f in files if suffix[f] in (".txt", ".md", ".csv")
                and _GLOSSARY_NAME.search(f.stem)]
    rest = [f for f in files if f not in glossary]
    slides = [f for f in rest if suffix[f] == ".pptx"
              or (suffix[f] == ".pdf" and not pdf_is_transcript(f))
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
    # what the lecture is built from: a folder with slides and nothing spoken is "slides"
    found["kind"] = ("transcript" if transcripts else "recording" if audio
                     else "slides" if slides else None)
    found["skipped"] = [f for f in transcripts + audio + slides + glossary
                        if f not in found.values()]
    return found


def input_kind(path: str | Path) -> str:
    """What one input file is: "recording", "slides" (a PDF, a PowerPoint, or a text file
    whose name says slides: a written source with no speech) or "transcript". A PDF is read
    to decide: a transcript published as a PDF (MIT OpenCourseWare does so) is a transcript."""
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix in AUDIO_EXT:
        return "recording"
    if suffix == ".pdf":
        return "transcript" if pdf_is_transcript(p) else "slides"
    if suffix == ".pptx" or (suffix in (".md", ".txt") and _SLIDE_NAME.search(p.stem)
                             and not _TRANSCRIPT_NAME.search(p.stem)):
        return "slides"
    return "transcript"


_CONTRACTION = re.compile(r"\b\w+['’](?:re|s|ll|ve|m|t|d)\b")
_SPEAKER = re.compile(r"^(?:[A-Z][A-Z .]{3,30}|Speaker \d+|Intervenant \d+)\s*:\s*")


def pdf_is_transcript(path: str | Path) -> bool:
    """Whether a PDF holds the transcript of a lecture rather than slides or written notes.

    Measured on real files (MIT OpenCourseWare): a transcript has a median of 500 to 600 words
    a page and 4 contractions ("we're", "that's") per 100 words; slide decks have 50 to 75
    words a page and under 0.6 contraction per 100 words. Written lecture notes are dense but
    not spoken, so both signs are asked for. A file that cannot be read is not a transcript."""
    p = Path(path)
    try:
        stat = p.stat()
        return _pdf_is_transcript(str(p.resolve()), stat.st_mtime_ns, stat.st_size)
    except OSError:
        return False


@lru_cache(maxsize=64)
def _pdf_is_transcript(path: str, _mtime: int, _size: int) -> bool:
    try:
        pages = _pdf_pages(path)
    except Exception:  # a damaged PDF: let the reader of the slides report it
        return False
    counts = [len(page.split()) for page in pages if page.strip()]
    if not counts or statistics.median(counts) < 250:
        return False
    text = " ".join(pages)
    return len(_CONTRACTION.findall(text)) >= 0.015 * sum(counts)


_LIGATURES = str.maketrans({"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st",
                            "ﬆ": "st", "\u00ad": "", "\u00a0": " "})


def _pdf_pages(path: str | Path) -> list[str]:
    """The text of each page of a PDF, ligatures written out.

    pypdf was kept after a comparison with PyMuPDF, pdfplumber and pdfium on real decks
    (November 2026): on Beamer slides with formulas it keeps a formula on its line where
    pdfplumber scatters the operators and PyMuPDF breaks every line at a subscript; on
    PowerPoint exports the four give the same text. What no reader can repair is a PDF whose
    fonts map glyphs to the wrong characters (see `garbled_share`)."""
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise SystemExit("Reading a PDF needs pypdf (pip install pypdf), "
                         "or give the slides as a .txt/.md file.") from exc
    return [(page.extract_text() or "").translate(_LIGATURES).strip()
            for page in PdfReader(str(path)).pages]


_GARBLED = re.compile(r"\ufffd|^[a-z]+[A-Z][a-z]+$|^![a-z]|[a-z]!$|[ĂŶĚƚƌŝŽƉ]")


def garbled_share(pages: list[str]) -> float:
    """The share of words that a PDF's fonts turned into something else ("notaAon" for
    "notation", "!me" for "time"): above a few percent the text needs a transcript or
    the original PowerPoint file beside it."""
    words = " ".join(pages).split()
    return sum(bool(_GARBLED.search(w)) for w in words) / max(1, len(words))


# a bullet sign (those of PowerPoint fonts are private-use characters, Beamer's are triangles)
# with or without a space after it, or a number or a letter followed by "." or ")"
_BULLET = re.compile(r"^\s*(?:[•◦▪‣·–—○●■□➢➤►▶◮▸▹◆◇§\ue000-\uf8ff]\s*|[-*+]\s+|"
                     r"\(?\d{1,2}[.)]\s+|\(?[a-zA-Z][.)]\s+)")
_PAGE_NUMBER = re.compile(r"^\s*(?:page|slide)?\s*\d{1,4}\s*(?:/|of|sur)?\s*\d{0,4}\s*$", re.I)
_TRAILING_NUMBER = re.compile(r"\s*\d{1,4}(?:\s*/\s*\d{1,4})?\s*$")
_DOTS = re.compile(r"\.(?:\s+\.){2,}")  # ". . ." of a formula, not the "..." of code
_PRIVATE = re.compile(r"[\ue000-\uf8ff]")  # signs of a symbol font: no meaning as text
# a line of program text: it is never joined to its neighbours, because a line break means
# something in code and a wrapped sentence looks the same to a PDF reader
_CODE = re.compile(
    r"^(?:(?:if|elif|else|for|while|def|return|print|import|from|class|try|except|finally|with"
    r"|break|continue|pass)\b.*[:)\]\w\"']$"
    r"|(?:else|try|finally)\s*:$"
    r"|[A-Za-z_][\w.\[\]]*\s*(?:=|\+=|-=|\*=|/=)\s*\S.*"
    r"|.*\w\s*(?:==|!=|<=|>=)\s*\w.*"
    r"|[A-Za-z_]\w*\s*[<>]\s*[A-Za-z_]\w*"
    r"|[A-Za-z_][\w.]*\(.*\)\s*:?"
    r"|<\w+>:?)$")


def _is_code(line: str) -> bool:
    return line.isascii() and bool(_CODE.match(line))


def _letters(text: str) -> int:
    return sum(c.isalnum() for c in text)


def slide_units(pages: list[str]) -> list[str]:
    """Slides as the source of a lecture with no recording and no transcript: one unit per
    bullet or sentence, in slide order.

    A line that continues the previous one (PDF text wraps mid-sentence) is joined to it: it
    starts in lower case, or the line before it is as long as the long lines of the deck, or
    it is a scrap of a formula (a bracket, a single symbol). The title of a slide (its first
    line) and lines of code are never joined to. Page numbers go, and so does any line
    repeated on more than half of the slides, with or without the page number after it (the
    header or footer of the deck); so do lines with fewer than two letters or digits. ". . ." becomes
    "…" so that "X1, . . . , Xn" is not cut into sentences. The result is what
    `segment.split_units` gives back when the units are written one per paragraph, so the
    user can correct `transcript.txt` like a transcript.
    """
    from .segment import split_units

    def clean(line: str) -> str:
        bullet = _BULLET.match(line)
        at = bullet.end() if bullet else 0
        line = line[:at] + _PRIVATE.sub(" ", line[at:])
        line = _DOTS.sub("…", re.sub(r"\s+", " ", line)).strip()
        return re.sub(r"\s+([,;])", r"\1", line)

    def key(line: str) -> str:  # a footer is the same line on every slide but for the number
        return _TRAILING_NUMBER.sub("", line) or line

    lines_of = [[clean(l) for l in page.splitlines()] for page in pages]
    lines_of = [[l for l in lines if l and not _PAGE_NUMBER.match(l)] for lines in lines_of]
    seen: dict[str, int] = {}
    for lines in lines_of:
        for line in {key(l) for l in lines}:
            seen[line] = seen.get(line, 0) + 1
    repeated = {l for l, n in seen.items() if len(pages) >= 4 and n > len(pages) / 2}
    lines_of = [[l for l in lines if key(l) not in repeated and l not in repeated]
                for lines in lines_of]
    lengths = sorted(len(l) for lines in lines_of for l in lines)
    long = 0.8 * lengths[int(0.95 * (len(lengths) - 1))] if lengths else 0
    units: list[str] = []
    for lines in lines_of:
        items: list[list] = []  # [text, is code, length of its last line, is the title]
        for line in lines:
            bullet = bool(_BULLET.match(line))
            text = _BULLET.sub("", line).strip()
            if not text:
                continue
            code = _is_code(text)
            last = items[-1] if items else None
            scrap = len(text) <= 2 or text[0] in ")]}"
            joins = (last is not None and not bullet and not code and not last[1]
                     and last[0][-1:] not in ".!?:;…"
                     and (last[0][-1:] in ",-(" or (
                         not last[3] and (text[:1].islower() or scrap or last[2] >= long))))
            if joins:
                last[0] += " " + text
                last[2] = len(line)
            else:
                # code keeps its words, but a space after "..." inside it would end a unit
                items.append([re.sub(r"(?<=[.!?…])\s+(?=\S)", "", text) if code else text,
                              code, len(line), not items])
        for item in items:
            if _letters(item[0]) >= 2:  # a lone symbol is the scrap of a formula
                units += [u for u in split_units(item[0]) if _letters(u)]
    return units


def _strip_subtitles(raw: str) -> str:
    lines = []
    for line in raw.splitlines():
        s = line.strip()
        if not s or s.upper().startswith("WEBVTT") or "-->" in s or re.fullmatch(r"\d+", s):
            continue
        lines.append(re.sub(r"<[^>]+>", "", s))
    return " ".join(lines)


_TRANSCRIPT_HEADER = re.compile(r"^(?:MITOCW\b.*|.*\bwatch\?v=\S+)$")


def _pdf_transcript(path: Path) -> str:
    """The text of a transcript published as a PDF: one paragraph per paragraph of the file
    as far as the lines show it (a short line ends one, a speaker's name starts one), without
    page numbers, the publisher's header and the speakers' names."""
    paragraphs: list[str] = []
    for page in _pdf_pages(path):
        lines = [re.sub(r"\s+", " ", l).strip() for l in page.splitlines()]
        lines = [l for l in lines if l and not _PAGE_NUMBER.match(l)
                 and not _TRANSCRIPT_HEADER.match(l)]
        width = max((len(l) for l in lines), default=0)
        open_paragraph = False
        for line in lines:
            speaker = _SPEAKER.match(line)
            text = line[speaker.end():] if speaker else line
            if speaker or not open_paragraph:
                paragraphs.append(text)
            else:
                paragraphs[-1] += " " + text
            open_paragraph = bool(text) and not (
                len(line) < 0.75 * width and text[-1:] in ".!?")
    return "\n\n".join(p.strip() for p in paragraphs if p.strip())


def load_transcript(path: str | Path) -> str:
    p = Path(path)
    if p.suffix.lower() == ".pdf":
        return _pdf_transcript(p)
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
                cache: str | Path | None = None, use_ocr: bool = True,
                merge: bool = True) -> list[str]:
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
        pages = _pdf_pages(p)
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
        if merge:
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
