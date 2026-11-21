"""Chunking, per-chunk synthesis (cached), joining, loudness, chapters."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import wave
from pathlib import Path
from typing import Callable

from .segment import split_sentences
from .tts import TTS

RATE = 24000


def run_ffmpeg(*args: str) -> None:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is required (macOS: brew install ffmpeg).")
    result = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args],
                            capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f"ffmpeg failed: {result.stderr.strip()[:400]}")


def chunk_text(text: str, max_chars: int = 600) -> list[str]:
    """Pieces of at most max_chars, cut at sentence ends (at word ends for a very long sentence)."""
    pieces: list[str] = []
    for sentence in split_sentences(text) or [text.strip()]:
        if len(sentence) <= max_chars:
            pieces.append(sentence)
            continue
        current = ""
        for word in sentence.split():
            if current and len(current) + 1 + len(word) > max_chars:
                pieces.append(current)
                current = word
            else:
                current = (current + " " + word).strip()
        if current:
            pieces.append(current)
    chunks, current = [], ""
    for piece in pieces:
        if current and len(current) + 1 + len(piece) > max_chars:
            chunks.append(current)
            current = piece
        else:
            current = (current + " " + piece).strip()
    if current:
        chunks.append(current)
    return chunks


def wav_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as wav:
        return wav.getnframes() / wav.getframerate()


def write_silence(path: Path, seconds: float) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(RATE)
        wav.writeframes(b"\x00\x00" * int(RATE * seconds))


def _cache_name(backend: TTS, text: str) -> str:
    key = f"{type(backend).__name__}|{getattr(backend, 'voice', '')}|{text}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16] + ".wav"


def render_chunk(backend: TTS, text: str, final: Path) -> None:
    """Voice one piece of text into `final` (mono WAV at RATE)."""
    raw = final.with_name(final.stem + ".raw" + backend.ext)
    backend.synth(text, raw)
    run_ffmpeg("-i", str(raw), "-ar", str(RATE), "-ac", "1", "-c:a", "pcm_s16le", str(final))
    raw.unlink()


def repair_chunks(backend: TTS, voiced: list[list[dict]], transcribe: Callable[[Path], str],
                  threshold: float, retries: int, cache: Path,
                  log: Callable[[str], None] = print, first: int = 0) -> list[dict]:
    """The round trip, with repair. `voiced` holds parts `first`, `first + 1`, ... (one part
    at a time when each is checked as soon as it is voiced). Every chunk is transcribed back and compared with its
    text; a chunk below `threshold` is voiced again, up to `retries` times, and the attempt
    heard best is kept in place of the first. Autoregressive voices skip or invent words on
    some samples, and resampling the failures is the standard inference-time fix (Seed-TTS,
    2024; Wang et al., 2026). Scores are cached by file, so a rerun checks only new audio."""
    from .roundtrip import similarity
    seen = json.loads(cache.read_text(encoding="utf-8")) if cache.exists() else {}
    results = []
    for p_index, entries in enumerate(voiced, first):
        chunks = [e for e in entries if e["kind"] == "chunk"]
        for c_index, entry in enumerate(chunks):
            path, stat = entry["path"], entry["path"].stat()
            key = f"{path.name}:{stat.st_size}:{stat.st_mtime_ns}"
            if key in seen:
                results.append(dict(seen[key], segment=p_index, chunk=c_index))
                continue
            heard = transcribe(path)
            score = first = similarity(entry["text"], heard)
            tries = 0
            while score < threshold and tries < retries:
                tries += 1
                trial = path.with_name(f"{path.stem}.try{tries}.wav")
                render_chunk(backend, entry["text"], trial)
                again = transcribe(trial)
                better = similarity(entry["text"], again)
                if better > score:
                    os.replace(trial, path)
                    score, heard = better, again
                else:
                    trial.unlink()
            if tries:
                log(f"[voice] part {p_index + 1}, chunk {c_index + 1}: heard {first:.2f}, "
                    f"{score:.2f} after {tries} new take(s)")
            item = {"score": score, "first": first, "tries": tries, "heard": heard}
            stat = path.stat()
            seen[f"{path.name}:{stat.st_size}:{stat.st_mtime_ns}"] = item
            results.append(dict(item, segment=p_index, chunk=c_index))
            cache.write_text(json.dumps(seen, ensure_ascii=False, indent=1), encoding="utf-8")
    return results


def synth_blocks(backend: TTS, parts: list[list[dict]], audio_dir: Path, chunk_chars: int,
                 quiz: bool = True, quiz_pause: float = 8.0,
                 log: Callable[[str], None] = print) -> list[list[dict]]:
    """Voice every speech block. Returns, per part, entries {chapter, kind, path, text}.

    kind is "chunk" (speech) or "pause" (silence after a quiz question). Each chunk is cached
    under a name derived from its text, so editing one sentence re-voices only that chunk.
    """
    return [synth_part(backend, blocks, audio_dir, chunk_chars, quiz, quiz_pause, log,
                       p_index, len(parts)) for p_index, blocks in enumerate(parts)]


def synth_part(backend: TTS, blocks: list[dict], audio_dir: Path, chunk_chars: int,
               quiz: bool = True, quiz_pause: float = 8.0,
               log: Callable[[str], None] = print, index: int = 0,
               count: int = 1) -> list[dict]:
    """Voice the speech blocks of one part (part `index` of `count`), as soon as its spoken
    form exists (flow.py). Returns its entries {chapter, kind, path, text, block}."""
    audio_dir.mkdir(parents=True, exist_ok=True)
    pause_file = audio_dir / f"pause_{quiz_pause:.1f}.wav"
    if quiz and not pause_file.exists():
        write_silence(pause_file, quiz_pause)
    chosen = [b for b in blocks
              if quiz or b["role"] not in ("quiz_intro", "question", "answer")]
    total = sum(len(chunk_text(b["text"], chunk_chars)) for b in chosen)
    entries, done = [], 0
    for b_index, block in enumerate(chosen):
        for chunk in chunk_text(block["text"], chunk_chars):
            done += 1
            final = audio_dir / _cache_name(backend, chunk)
            # the part first: the app reads the progress of the stage from the first count
            log(f"[voice] part {index + 1}/{count}, chunk {done}/{total}"
                + (" (already recorded)" if final.exists() else ""))
            if not final.exists():
                render_chunk(backend, chunk, final)
            entries.append({"chapter": block["chapter"], "kind": "chunk", "path": final,
                            "text": chunk, "block": b_index})
        if block["role"] == "question" and quiz:
            entries.append({"chapter": block["chapter"], "kind": "pause", "path": pause_file,
                            "text": None, "block": b_index})
    log(f"[voice] part {index + 1}/{count}: {done} chunk(s)")
    return entries


def join_parts(parts: list[list[dict]], audio_dir: Path, chunk_gap: float = 0.25,
               block_gap: float = 0.7, part_gap: float = 1.5,
               stem: str = "joined") -> tuple[Path, list[tuple[str, float]]]:
    """Concatenate with pauses. Returns the WAV file (`stem`.wav) and (chapter, seconds) in
    playing order. One part alone gives the recording of that part."""
    gaps = {}
    for name, seconds in (("chunk", chunk_gap), ("block", block_gap), ("part", part_gap)):
        gaps[name] = (audio_dir / f"gap_{name}.wav", seconds)
        write_silence(gaps[name][0], seconds)
    files: list[Path] = []
    chapters: list[list] = []

    def add(chapter: str, seconds: float) -> None:
        if not chapters or chapters[-1][0] != chapter:
            chapters.append([chapter, 0.0])
        chapters[-1][1] += seconds

    for p_index, entries in enumerate(parts):
        for e_index, entry in enumerate(entries):
            files.append(entry["path"])
            add(entry["chapter"], wav_seconds(entry["path"]))
            if e_index < len(entries) - 1:
                same_block = entries[e_index + 1]["block"] == entry["block"]
                gap = gaps["chunk" if same_block else "block"]
            elif p_index < len(parts) - 1:
                gap = gaps["part"]
            else:
                continue
            files.append(gap[0])
            add(entry["chapter"], gap[1])
    listing = audio_dir / ("concat.txt" if stem == "joined" else f"{stem}.concat.txt")
    listing.write_text("".join("file '" + str(p.resolve()).replace("'", "'\\''") + "'\n"
                               for p in files), encoding="utf-8")
    joined = audio_dir / f"{stem}.wav"
    run_ffmpeg("-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(joined))
    return joined, [(c, s) for c, s in chapters]


def _meta_escape(text: str) -> str:
    for char in ("\\", "=", ";", "#", "\n"):
        text = text.replace(char, "\\" + char)
    return text


def write_chapters(path: Path, titles: list[str], durations: list[float]) -> None:
    lines, start = [";FFMETADATA1"], 0
    for title, seconds in zip(titles, durations):
        end = start + int(round(seconds * 1000))
        lines += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={start}", f"END={end}",
                  f"title={_meta_escape(title)}"]
        start = end
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def encode_final(joined: Path, out_path: Path, titles: list[str], durations: list[float],
                 loudness: bool = True) -> None:
    filters = ["-af", "loudnorm=I=-18:TP=-1.5:LRA=11"] if loudness else []
    extension = out_path.suffix.lower()
    if extension == ".wav":
        run_ffmpeg("-i", str(joined), *filters, "-ar", "24000", str(out_path))
    elif extension == ".mp3":
        run_ffmpeg("-i", str(joined), *filters, "-ar", "44100", "-c:a", "libmp3lame",
                   "-q:a", "4", str(out_path))
    elif extension in (".m4a", ".mp4"):
        meta = joined.parent / "chapters.txt"
        write_chapters(meta, titles, durations)
        run_ffmpeg("-i", str(joined), "-i", str(meta), "-map", "0:a", "-map_metadata", "1",
                   "-map_chapters", "1", *filters, "-ar", "44100", "-c:a", "aac", "-b:a", "96k",
                   str(out_path))
    else:
        raise RuntimeError(f"Unsupported output format: {extension}")


def encode_part_files(voiced: list[list[dict]], part_ids: list[str], audio_dir: Path,
                      out_dir: Path, name: str, extension: str, titles: dict[str, str],
                      loudness: bool = True) -> dict[str, Path]:
    """One recording per part, with its own chapters (preview, points, summary), for the
    players of the part notes. Returns {part id: file}."""
    out_dir.mkdir(parents=True, exist_ok=True)
    files = {}
    for part_id, entries in zip(part_ids, voiced):
        if not entries:
            continue
        stem = f"part_{int(part_id):03d}"
        joined, chapters = join_parts([entries], audio_dir, stem=stem)
        target = out_dir / f"{name}-{int(part_id):02d}.{extension}"
        encode_final(joined, target, [titles.get(c, c) for c, _ in chapters],
                     [s for _, s in chapters], loudness=loudness)
        joined.unlink(missing_ok=True)
        files[part_id] = target
    return files
