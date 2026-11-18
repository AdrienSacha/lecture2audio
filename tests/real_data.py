"""Real lecture material for `test_real.py`: slides, transcripts and captions of MIT
OpenCourseWare lectures, downloaded once into `tests/.cache/` (never committed).

Every file is published by MIT OpenCourseWare under CC BY-NC-SA 4.0
(https://creativecommons.org/licenses/by-nc-sa/4.0/, https://ocw.mit.edu/terms/). The URLs
were read from the resource pages named in `page`; none was guessed.
"""
from __future__ import annotations

import os
import unittest
import urllib.error
import urllib.request
from pathlib import Path

CACHE = Path(__file__).resolve().parent / ".cache"
LICENCE = "CC BY-NC-SA 4.0"
_OCW = "https://ocw.mit.edu/courses/"
_STATS = _OCW + "18-650-statistics-for-applications-fall-2016/"
_PYTHON = _OCW + "6-0001-introduction-to-computer-science-and-programming-in-python-fall-2016/"
_FORTRAN = _OCW + "12-010-computational-methods-of-scientific-programming-fall-2024/"

# name: file name in the cache, type (checked by magic bytes), URL, the page that links it
FILES = {
    # 18.650 Statistics for Applications, Fall 2016 (Philippe Rigollet): formulas on every slide
    "stats_slides": {
        "file": "stats_mle_slides.pdf", "type": "pdf",
        "url": _STATS + "36624c2682b346b07cab3da416b752a7_MIT18_650F16_Maximum_LE.pdf",
        "page": _STATS + "resources/lecture-3-maximum-likelihood-estimation/",
        "title": "18.650 Lectures 4-5: Maximum Likelihood Estimation (slides)"},
    "stats_captions": {
        "file": "stats_lecture4.vtt", "type": "vtt",
        "url": _STATS + "f53d61d7c6c25c70b7c9331c084db148_rLlZpnT02ZU.vtt",
        "page": _STATS + "resources/lecture-4-video/",
        "title": "18.650 Lecture 4: Parametric Inference (cont.) and Maximum Likelihood "
                 "Estimation (captions)"},
    "stats_transcript_pdf": {
        "file": "stats_lecture4_transcript.pdf", "type": "pdf",
        "url": _STATS + "c4ea10c2bd797d21f8f4c5d61a8523f2_rLlZpnT02ZU.pdf",
        "page": _STATS + "resources/lecture-4-video/",
        "title": "18.650 Lecture 4 (transcript as PDF)"},
    "stats_intro_slides": {
        "file": "stats_intro_slides.pdf", "type": "pdf",
        "url": _STATS + "7836ea6d101679d495194bc3c5f227fa_MIT18_650F16_Introduction.pdf",
        "page": _STATS + "resources/lecture-1-introduction/",
        "title": "18.650 Lectures 1-2: Introduction (slides)"},
    # 6.0001 Introduction to Computer Science and Programming in Python, Fall 2016 (Ana Bell):
    # PowerPoint decks exported to PDF, code on the slides
    "python_slides": {
        "file": "python_lec2_slides.pdf", "type": "pdf",
        "url": _PYTHON + "ba2947b25b1580e4a84df0ec5dbe5cdd_MIT6_0001F16_Lec2.pdf",
        "page": _PYTHON + "resources/mit6_0001f16_lec2/",
        "title": "6.0001 Lecture 2: Branching, Iteration (slides)"},
    "python_captions": {
        "file": "python_lecture2.vtt", "type": "vtt",
        "url": _PYTHON + "8a169eb715615fa0b797f83ff9d20ab8_0jljZRnHwOI.vtt",
        "page": _PYTHON + "resources/lecture-2-branching-and-iteration/",
        "title": "6.0001 Lecture 2: Branching and Iteration (captions)"},
    "python_transcript_pdf": {
        "file": "python_lecture2_transcript.pdf", "type": "pdf",
        "url": _PYTHON + "d733a4c1bffb18f632d314f93db7cf96_0jljZRnHwOI.pdf",
        "page": _PYTHON + "resources/lecture-2-branching-and-iteration/",
        "title": "6.0001 Lecture 2 (transcript as PDF)"},
    "python_efficiency_slides": {
        "file": "python_lec10_slides.pdf", "type": "pdf",
        "url": _PYTHON + "066eba6ea6d56a88e56ae325940d4c4c_MIT6_0001F16_Lec10.pdf",
        "page": _PYTHON + "resources/mit6_0001f16_lec10/",
        "title": "6.0001 Lecture 10: Understanding Program Efficiency, Part 1 (slides)"},
    # 12.010 Computational Methods of Scientific Programming, Fall 2024 (Thomas Herring,
    # Christopher Hill): a PowerPoint file
    "fortran_pptx": {
        "file": "fortran_lec02.pptx", "type": "pptx",
        "url": _FORTRAN + "mit12_010_f24_lec02.pptx",
        "page": _FORTRAN + "resources/mit12_010_f24_lec02_pptx/",
        "title": "12.010 Lecture 2 (PowerPoint)"},
}

_MAGIC = {"pdf": (b"%PDF-",), "pptx": (b"PK\x03\x04",), "vtt": (b"WEBVTT", b"\xef\xbb\xbfWEBVTT"),
          "srt": (b"1\r\n", b"1\n", b"\xef\xbb\xbf1")}


def _valid(path: Path, kind: str) -> bool:
    if not path.is_file() or path.stat().st_size == 0:
        return False
    with path.open("rb") as handle:
        return handle.read(16).startswith(_MAGIC[kind])


def fetch(name: str) -> Path:
    """The local copy of one real file, downloaded on first use. Raises `unittest.SkipTest`
    when the file is not in the cache and the network does not give it."""
    if os.environ.get("L2A_REAL", "1") == "0":
        raise unittest.SkipTest("L2A_REAL=0: the tests on real lectures are skipped")
    entry = FILES[name]
    target = CACHE / entry["file"]
    if _valid(target, entry["type"]):
        return target
    CACHE.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")
    request = urllib.request.Request(entry["url"], headers={"User-Agent": "python-urllib"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            partial.write_bytes(response.read())
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        partial.unlink(missing_ok=True)
        raise unittest.SkipTest(f"{entry['url']} could not be downloaded ({exc})") from None
    if not _valid(partial, entry["type"]):
        size = partial.stat().st_size
        partial.unlink(missing_ok=True)
        raise AssertionError(f"{entry['url']} is not a {entry['type']} file ({size} bytes): "
                             "the address changed, read it again from " + entry["page"])
    partial.replace(target)
    return target


if __name__ == "__main__":  # python tests/real_data.py: download everything
    for key, item in FILES.items():
        print(f"{fetch(key)}  {item['title']}  [{LICENCE}]")
