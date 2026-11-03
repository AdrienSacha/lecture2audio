"""Speech to text. Optional: used for audio input and for the round-trip check."""
from __future__ import annotations

_CACHE: dict = {}


class STTError(RuntimeError):
    pass


def _mlx(path: str, language: str | None, prompt: str | None, model: str | None,
         progress: bool = False) -> str:
    import mlx_whisper
    # verbose=True prints each recognised segment with its time, so a long recording shows
    # progress; verbose=None prints nothing (used by the round trip).
    options = {"verbose": True if progress else None}
    if language:
        options["language"] = language
    if prompt:
        options["initial_prompt"] = prompt
    result = mlx_whisper.transcribe(
        path, path_or_hf_repo=model or "mlx-community/whisper-large-v3-mlx", **options)
    return result["text"].strip()


def _faster(path: str, language: str | None, prompt: str | None, model: str | None,
            progress: bool = False) -> str:
    from faster_whisper import WhisperModel
    key = ("faster", model)
    if key not in _CACHE:
        _CACHE[key] = WhisperModel(model or "large-v3", device="auto", compute_type="default")
    segments, _info = _CACHE[key].transcribe(path, language=language, initial_prompt=prompt,
                                             vad_filter=True)
    texts = []
    for segment in segments:
        texts.append(segment.text.strip())
        if progress:
            print(f"[{segment.start / 60:5.1f} min] {texts[-1]}", flush=True)
    return " ".join(texts)


_BACKENDS = {"mlx": _mlx, "faster": _faster}


def transcribe_audio(path, backend: str = "auto", language: str | None = None,
                     prompt: str | None = None, model: str | None = None,
                     progress: bool = False) -> str:
    """Transcribe an audio file. `prompt` (e.g. glossary terms) helps with technical words."""
    order = ["mlx", "faster"] if backend == "auto" else [backend]
    problems = []
    for name in order:
        try:
            return _BACKENDS[name](str(path), language, prompt, model, progress)
        except ImportError as exc:
            problems.append(f"{name}: {exc}")
    raise STTError("No speech-to-text backend is installed. On a Mac with Apple Silicon: "
                   "pip install mlx-whisper. Elsewhere: pip install faster-whisper.\n"
                   + "\n".join(problems))
