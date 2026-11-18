"""Reading slides by sight.

The text layer of a PDF loses what slides are made of: a sum sign comes out as "L", a fraction
as two stray lines, a figure as nothing, and some fonts map every letter to another one. When
the model can read images, each page is rendered and transcribed by the model (formulas as
LaTeX, figures described), with the text layer beside it for the spelling of the words. Code
then checks the transcription against the text layer: a reading that lost most of the words
the page really holds is refused, asked again once, and replaced by the text layer if it
still fails. One call per slide, cached in slides/.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .llm import LLM, LLMError
from .sources import garbled_share, load_slides

PROMPT_DIR = Path(__file__).parent / "prompts"
MIN_KEPT = 0.6  # share of the page's words that a reading must keep
_WORD = re.compile(r"[^\W\d_]{4,}")


def render_pages(path: str | Path, dpi: int = 110) -> list[bytes] | None:
    """Each page of a PDF as a PNG image (poppler's pdftoppm), or None when poppler is not
    installed or the file cannot be rendered."""
    if not shutil.which("pdftoppm"):
        return None
    with tempfile.TemporaryDirectory() as folder:
        try:
            subprocess.run(["pdftoppm", "-r", str(dpi), "-png", str(path),
                            str(Path(folder) / "page")], check=True, capture_output=True,
                           timeout=600)
        except (subprocess.SubprocessError, OSError):
            return None
        return [p.read_bytes() for p in sorted(Path(folder).glob("page*.png"))]


def kept_share(layer: str, reading: str) -> float | None:
    """The share of the words of the page's text layer that the reading holds, or None when
    the layer is too short or too garbled to judge by (a page that is one picture, a font
    that maps letters wrongly)."""
    words = {w.lower() for w in _WORD.findall(layer)}
    if len(words) < 8 or garbled_share([layer]) > 0.03:
        return None
    held = {w.lower() for w in _WORD.findall(reading)}
    return len(words & held) / len(words)


def _clean(reading: str) -> str:
    """The model's transcription without a Markdown fence around it."""
    text = reading.strip()
    if text.startswith("```"):
        text = re.sub(r"^```\w*\n?|\n?```$", "", text).strip()
    return text


def read_slides(path: str | Path, cache: Path, llm: LLM, log=print, workers: int = 3,
                prompts_dir: str | Path | None = None) -> tuple[list[str], dict]:
    """(one string per slide, notes). Slides the model could not read, or read unfaithfully,
    keep their text layer and are numbered in notes["fallback"]."""
    layers = load_slides(path)
    images = render_pages(path)
    if images is None or len(images) != len(layers):
        why = ("poppler (pdftoppm) is not installed" if images is None
               else "the pages could not all be rendered")
        log(f"[slides] read from the text of the PDF: {why}")
        return layers, {"mode": "text", "why": why, "fallback": []}
    custom = Path(prompts_dir) / "slide.md" if prompts_dir else None
    prompt = (custom if custom and custom.exists() else PROMPT_DIR / "slide.md").read_text(
        encoding="utf-8")
    cache.mkdir(parents=True, exist_ok=True)
    total = len(layers)
    log(f"[slides] reading {total} slide(s) by sight with {llm.name}")

    def one(k: int) -> tuple[str, str | None]:
        saved = cache / f"page_{k + 1:03d}.md"
        if saved.exists():
            return saved.read_text(encoding="utf-8"), None
        user = (f"Slide {k + 1} of {total}.\n\n## TEXT LAYER\n"
                + (layers[k] or "(the page has no text layer)"))
        why = None
        for _ in range(2):
            try:
                reading = _clean(llm.read_image(prompt, user, images[k]))
            except LLMError as exc:
                why = str(exc)
                break
            share = kept_share(layers[k], reading)
            if reading and (share is None or share >= MIN_KEPT):
                saved.write_text(reading, encoding="utf-8")
                return reading, None
            why = ("the reading was empty" if not reading else
                   f"the reading kept {share:.0%} of the words of the page")
            user += ("\n\n## PREVIOUS ANSWER REJECTED\nYour transcription left out words that "
                     "are on the slide. Transcribe everything on it.")
        return layers[k], why  # not cached: the next run tries again

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        results = list(pool.map(one, range(total)))
    fallback = [{"slide": k + 1, "why": why} for k, (_, why) in enumerate(results) if why]
    notes = {"mode": "vision", "model": llm.name, "slides": total, "fallback": fallback}
    (cache / "notes.json").write_text(json.dumps(notes, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
    if fallback:
        log(f"[slides] {len(fallback)} slide(s) kept their text layer (see report.md)")
    return [text for text, _ in results], notes


def report_lines(notes: dict | None) -> list[str]:
    """The section of report.md about how the slides were read."""
    if not notes:
        return []
    lines = ["## Slides", ""]
    if notes["mode"] == "text":
        lines.append("The slides were read from the text of the file"
                     + (f" ({notes['why']})." if notes.get("why") else ".")
                     + " Formulas and figures of a PDF do not survive this; a model that reads "
                       "images (`--llm anthropic` or `openai`, or `--read-slides vision`) "
                       "reads them from the page.")
    else:
        lines.append(f"The {notes['slides']} slide(s) were read by sight by {notes['model']}: "
                     "formulas as LaTeX, figures described. Code compared each reading with "
                     "the text of the page.")
        lines += [f"- Slide {f['slide']} kept the text of the file: {f['why']}."
                  for f in notes["fallback"]]
    return lines + [""]
