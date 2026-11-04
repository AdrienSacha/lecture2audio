"""Speech to text. Optional: used for audio input and for the round-trip check of the voice.

Whisper writes words over silence and music (on room tone, whisper-large-v3 emits words on
62% of clips: Zolkepli, 2026) and can loop on one sentence. Three guards follow the measured
mitigations: decoding without conditioning on the previous text and skipping silent stretches
where the decoder is unsure (mlx-whisper), voice activity detection (faster-whisper), and a
code filter that drops segments that are exactly a known hallucination phrase or the third
repeat of the same segment. Every dropped segment is reported, never silently lost.
Parakeet (NVIDIA, transducer decoder, 25 languages) is offered as an alternative: it has no
language-model decoder that can invent sentences over silence, but takes no glossary prompt.
"""
from __future__ import annotations

import re
import sys

_CACHE: dict = {}

# Phrases Whisper emits over silence or music, from public lists of its hallucinations
# (Barański et al., 2025; Zolkepli, 2026). Matched against a whole segment only, so a teacher
# who says one of them inside a sentence is never cut.
HALLUCINATIONS = {
    "thank you for watching", "thanks for watching", "thank you for watching this video",
    "please subscribe", "subscribe to my channel", "like and subscribe",
    "subtitles by the amara org community", "transcription by castingwords",
    "thank you", "you", "bye", "the end",
    "sous titres réalisés par la communauté d amara org", "sous titrage st 501",
    "merci d avoir regardé cette vidéo", "merci d avoir regardé", "abonnez vous",
    "sous titres par amara org", "merci", "au revoir",
}


class STTError(RuntimeError):
    pass


def _normal(text: str) -> str:
    return " ".join(re.findall(r"[^\W_]+", text.lower()))


def clean_segments(texts: list[str]) -> tuple[list[str], list[dict]]:
    """Segments without known hallucination phrases and without the third and later repeats
    of one segment; the removed ones, with the reason, for the report."""
    kept, removed = [], []
    for text in texts:
        norm = _normal(text)
        if not norm:
            continue
        if norm in HALLUCINATIONS:
            removed.append({"text": text, "reason": "a phrase speech recognition writes over "
                                                    "silence"})
            continue
        if len(kept) >= 2 and _normal(kept[-1]) == norm == _normal(kept[-2]):
            removed.append({"text": text, "reason": "the same segment a third time in a row"})
            continue
        kept.append(text.strip())
    return kept, removed


def _mlx(path: str, language: str | None, prompt: str | None, model: str | None,
         progress: bool = False) -> list[str]:
    import mlx_whisper
    # verbose=True prints each recognised segment with its time, so a long recording shows
    # progress; verbose=None prints nothing (used by the round trip).
    options = {"verbose": True if progress else None, "condition_on_previous_text": False}
    if language:
        options["language"] = language
    if prompt:
        options["initial_prompt"] = prompt
    repo = model or "mlx-community/whisper-large-v3-mlx"
    try:  # skip silent stretches where the decoder is unsure (needs word timestamps)
        result = mlx_whisper.transcribe(path, path_or_hf_repo=repo, word_timestamps=True,
                                        hallucination_silence_threshold=2.0, **options)
    except TypeError:  # an mlx-whisper without these options
        result = mlx_whisper.transcribe(path, path_or_hf_repo=repo, **options)
    return [s["text"] for s in result.get("segments") or []] or [result["text"]]


def _faster(path: str, language: str | None, prompt: str | None, model: str | None,
            progress: bool = False) -> list[str]:
    from faster_whisper import WhisperModel
    key = ("faster", model)
    if key not in _CACHE:
        _CACHE[key] = WhisperModel(model or "large-v3", device="auto", compute_type="default")
    segments, _info = _CACHE[key].transcribe(path, language=language, initial_prompt=prompt,
                                             vad_filter=True, condition_on_previous_text=False)
    texts = []
    for segment in segments:
        texts.append(segment.text.strip())
        if progress:
            print(f"[{segment.start / 60:5.1f} min] {texts[-1]}", flush=True)
    return texts


def _parakeet(path: str, language: str | None, prompt: str | None, model: str | None,
              progress: bool = False) -> list[str]:
    from parakeet_mlx import from_pretrained
    key = ("parakeet", model)
    if key not in _CACHE:
        _CACHE[key] = from_pretrained(model or "mlx-community/parakeet-tdt-0.6b-v3")
    if prompt and progress:
        print("[transcribe] Parakeet takes no glossary prompt; check the technical terms in "
              "transcript.txt", file=sys.stderr)
    result = _CACHE[key].transcribe(path, chunk_duration=120.0, overlap_duration=15.0)
    texts = []
    for sentence in result.sentences:
        texts.append(sentence.text.strip())
        if progress:
            print(f"[{sentence.start / 60:5.1f} min] {texts[-1]}", flush=True)
    return texts or [result.text]


_BACKENDS = {"mlx": _mlx, "faster": _faster, "parakeet": _parakeet}


def available() -> bool:
    """Whether any speech-to-text backend is installed (for the automatic round trip)."""
    from importlib.util import find_spec
    return any(find_spec(m) for m in ("mlx_whisper", "faster_whisper", "parakeet_mlx"))


def transcribe_audio(path, backend: str = "auto", language: str | None = None,
                     prompt: str | None = None, model: str | None = None,
                     progress: bool = False, removed: list | None = None) -> str:
    """Transcribe an audio file. `prompt` (e.g. glossary terms) helps Whisper with technical
    words. Segments dropped by `clean_segments` are appended to `removed` when given."""
    order = ["mlx", "faster", "parakeet"] if backend == "auto" else [backend]
    problems = []
    for name in order:
        try:
            texts = _BACKENDS[name](str(path), language, prompt, model, progress)
        except ImportError as exc:
            problems.append(f"{name}: {exc}")
            continue
        kept, dropped = clean_segments(texts)
        if removed is not None:
            removed.extend(dropped)
        return " ".join(kept).strip()
    raise STTError("No speech-to-text backend is installed. On a Mac with Apple Silicon: "
                   "uv pip install mlx-whisper (or parakeet-mlx). Elsewhere: uv pip install "
                   "faster-whisper.\n" + "\n".join(problems))
