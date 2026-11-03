"""Cut a transcript into sentences and numbered units."""
from __future__ import annotations

import re

_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")


def word_count(text: str) -> int:
    return len(text.split())


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_END.split(text) if s.strip()]


def _hard_split(text: str, limit: int) -> list[str]:
    words = text.split()
    return [" ".join(words[i:i + limit]) for i in range(0, len(words), limit)]


def split_units(text: str, max_words: int = 40) -> list[str]:
    """Numbered units for the outline: sentences, with very long ones cut every `max_words` words.

    A transcript without punctuation therefore still gives units of a usable size.
    """
    units: list[str] = []
    for paragraph in re.split(r"\n\s*\n", text):
        paragraph = re.sub(r"\s+", " ", paragraph).strip()
        if not paragraph:
            continue
        for sentence in split_sentences(paragraph):
            if word_count(sentence) > max_words:
                units.extend(_hard_split(sentence, max_words))
            else:
                units.append(sentence)
    return units
