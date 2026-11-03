"""Round-trip check: voice -> speech recognition -> compare with what the voice was given."""
from __future__ import annotations

import difflib
import re
from pathlib import Path
from typing import Callable


def _words(text: str) -> list[str]:
    return re.findall(r"[^\W_]+", text.lower())


def similarity(expected: str, heard: str) -> float:
    a, b = _words(expected), _words(heard)
    if not a and not b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def check_chunks(transcribe: Callable[[Path], str],
                 groups: list[list[tuple[Path, str]]]) -> list[dict]:
    results = []
    for s_index, group in enumerate(groups):
        for c_index, (path, text) in enumerate(group):
            heard = transcribe(path)
            results.append({"segment": s_index, "chunk": c_index,
                            "score": similarity(text, heard), "heard": heard})
    return results
