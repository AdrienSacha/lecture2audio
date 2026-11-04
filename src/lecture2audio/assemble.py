"""Chunking, per-chunk synthesis (cached), joining, loudness, chapters."""
from __future__ import annotations

import hashlib
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


def synth_blocks(backend: TTS, parts: list[list[dict]], audio_dir: Path, chunk_chars: int,
                 quiz: bool = True, quiz_pause: float = 8.0,
                 log: Callable[[str], None] = print) -> list[list[dict]]:
    """Voice every speech block. Returns, per part, entries {chapter, kind, path, text}.

    kind is "chunk" (speech) or "pause" (silence after a quiz question). Each chunk is cached
    under a name derived from its text, so editing one sentence re-voices only that chunk.
    """
    audio_dir.mkdir(parents=True, exist_ok=True)
    pause_file = audio_dir / f"pause_{quiz_pause:.1f}.wav"
    if quiz:
        write_silence(pause_file, quiz_pause)
    result = []
    keep = lambda b: quiz or b["role"] not in ("quiz_intro", "question", "answer")
    total = sum(len(chunk_text(b["text"], chunk_chars)) for blocks in parts for b in blocks if keep(b))
    done = 0
    for p_index, blocks in enumerate(parts):
        entries = []
        chosen = [b for b in blocks if keep(b)]
        count = 0
        for b_index, block in enumerate(chosen):
            for chunk in chunk_text(block["text"], chunk_chars):
                done += 1
                final = audio_dir / _cache_name(backend, chunk)
                log(f"[voice] chunk {done}/{total}, part {p_index + 1}/{len(parts)}"
                    + (" (already recorded)" if final.exists() else ""))
                if not final.exists():
                    raw = audio_dir / (final.stem + ".raw" + backend.ext)
                    backend.synth(chunk, raw)
                    run_ffmpeg("-i", str(raw), "-ar", str(RATE), "-ac", "1", "-c:a", "pcm_s16le",
                               str(final))
                    raw.unlink()
                entries.append({"chapter": block["chapter"], "kind": "chunk", "path": final,
                                "text": chunk, "block": b_index})
                count += 1
            if block["role"] == "question" and quiz:
                entries.append({"chapter": block["chapter"], "kind": "pause", "path": pause_file,
                                "text": None, "block": b_index})
        log(f"[voice] part {p_index + 1}/{len(parts)}: {count} chunk(s)")
        result.append(entries)
    return result


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
