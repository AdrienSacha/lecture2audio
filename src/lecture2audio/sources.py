"""Reading the inputs: transcript, glossary, slides; language detection."""
from __future__ import annotations

import math
import re
from pathlib import Path

AUDIO_EXT = {".mp3", ".m4a", ".wav", ".flac", ".ogg", ".webm", ".mp4", ".mov", ".mkv", ".aac"}


def slugify(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").lower()
    return slug or "lecture"


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


def load_slides(path: str | Path | None) -> list[str]:
    """One string per slide. PDF needs pypdf; text/markdown files use '---' lines or form feeds."""
    if not path:
        return []
    p = Path(path)
    if p.suffix.lower() == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise SystemExit("Reading a PDF needs pypdf (pip install pypdf), "
                             "or give the slides as a .txt/.md file.") from exc
        return [(page.extract_text() or "").strip() for page in PdfReader(str(p)).pages]
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
