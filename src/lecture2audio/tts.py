"""Voice backends. A backend turns one piece of text into one audio file."""
from __future__ import annotations

import json
import math
import os
import shlex
import shutil
import struct
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path


class TTSError(RuntimeError):
    pass


class TTS:
    name = "tts"
    ext = ".wav"  # extension of the file the backend writes; ffmpeg converts it afterwards

    def synth(self, text: str, out_path: Path) -> None:
        raise NotImplementedError


class FakeTTS(TTS):
    """A tone whose length follows the text length. For tests and dry runs."""
    name = "fake"

    def synth(self, text: str, out_path: Path) -> None:
        rate = 24000
        frames = int(rate * max(0.4, len(text) / 15.0))
        freq = 180 + (len(text) % 9) * 25
        with wave.open(str(out_path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(rate)
            wav.writeframes(b"".join(
                struct.pack("<h", int(6000 * math.sin(2 * math.pi * freq * i / rate)))
                for i in range(frames)))


class SayTTS(TTS):
    """macOS built-in voice. Nothing to install; a good first test, not the best quality."""
    name = "say"
    ext = ".aiff"

    def __init__(self, voice: str | None = None, rate: int | None = None):
        if not shutil.which("say"):
            raise TTSError("The 'say' command exists only on macOS.")
        self.voice, self.rate = voice, rate

    def synth(self, text: str, out_path: Path) -> None:
        command = ["say", "-o", str(out_path)]
        if self.voice:
            command += ["-v", self.voice]
        if self.rate:
            command += ["-r", str(self.rate)]
        subprocess.run(command, input=text.encode("utf-8"), check=True)


class MingTTS(TTS):
    """Ming-3.0-tts-0.5B (inclusionAI / Ant Group, MIT licence), local through mlx-audio.
    About real time and 4.4 GB of memory at its peak on an Apple chip; 44.1 kHz output.

    `voice` is a built-in voice (US_James, US_Emma, UK_George, UK_Charlotte, US_Stella), so the
    whole lecture keeps one voice, or a WAV file of a voice to imitate; that file is transcribed
    locally, because the model needs the exact words of its reference."""
    name = "ming"
    MODEL = "mlx-community/Ming-3.0-tts-0.5B-bf16"
    VOICES = ("US_James", "US_Emma", "UK_George", "UK_Charlotte", "US_Stella")
    DEFAULT_VOICE = "US_James"

    def __init__(self, voice: str | None = None, model: str | None = None):
        try:
            import numpy
            from mlx_audio.tts.utils import load
        except ImportError as exc:
            raise TTSError(f"Ming needs mlx-audio: pip install 'mlx-audio>=0.6.2' ({exc})") from exc
        self._np = numpy
        self.options: dict = {"voice": voice or self.DEFAULT_VOICE}
        if voice and voice.lower().endswith((".wav", ".mp3", ".m4a", ".flac")):
            if not Path(voice).is_file():
                raise TTSError(f"--voice file not found: {voice}")
            from .transcribe import transcribe_audio
            self.options = {"ref_audio": voice, "ref_text": transcribe_audio(voice, language="en")}
        elif voice and voice not in self.VOICES:
            raise TTSError(f"Unknown Ming voice {voice!r}: use one of {', '.join(self.VOICES)}, "
                           "or a WAV file of a voice")
        self.model = load(model or self.MODEL)

    def synth(self, text: str, out_path: Path) -> None:
        results = list(self.model.generate(text, lang_code="en", **self.options))
        pieces = [self._np.asarray(r.audio, dtype=self._np.float32) for r in results if r.samples]
        if not pieces:
            raise TTSError("Ming returned no audio for a piece of text.")
        audio = self._np.clip(self._np.concatenate(pieces), -1, 1)
        with wave.open(str(out_path), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(results[0].sample_rate)
            out.writeframes((audio * 32767).round().astype("<i2").tobytes())


class OpenAITTS(TTS):
    """OpenAI speech endpoint, POST {base_url}/audio/speech (or any server with that route).

    gpt-4o-mini-tts takes `instructions` and the voices marin and cedar, which the speech
    guide recommends for quality. tts-1 and tts-1-hd take neither. OpenAI announces that
    these speech models shut down on 6 January 2027.
    """
    name = "openai"
    OLD_MODELS = ("tts-1", "tts-1-hd")

    def __init__(self, voice: str | None = None, model: str = "gpt-4o-mini-tts",
                 base_url: str = "https://api.openai.com/v1", api_key: str | None = None,
                 instructions: str = "Speak as a calm, precise teacher. Steady pace, clear "
                                     "articulation, neutral tone, short pauses between ideas."):
        self.model = model
        self.voice = voice or ("alloy" if model in self.OLD_MODELS else "marin")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self.instructions = instructions
        if not self.api_key:
            raise TTSError("OPENAI_API_KEY is not set.")

    def payload(self, text: str) -> dict:
        payload = {"model": self.model, "voice": self.voice, "input": text, "response_format": "wav"}
        if self.model not in self.OLD_MODELS and self.instructions:
            payload["instructions"] = self.instructions
        return payload

    def synth(self, text: str, out_path: Path) -> None:
        from .llm import retry_wait, should_retry
        data = json.dumps(self.payload(text)).encode("utf-8")
        delay = 2.0
        for attempt in range(4):
            request = urllib.request.Request(
                f"{self.base_url}/audio/speech", data=data,
                method="POST", headers={"authorization": f"Bearer {self.api_key}",
                                        "content-type": "application/json"})
            try:
                with urllib.request.urlopen(request, timeout=300) as response:
                    audio = response.read()
                    kind = response.headers.get("content-type", "")
                if not audio or kind.startswith(("application/json", "text/")):
                    raise TTSError(f"The speech server returned no audio: {audio[:300]!r}")
                out_path.write_bytes(audio)
                return
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", "replace")
                exc.close()
                if attempt < 3 and should_retry(exc.code, exc.headers, body):
                    time.sleep(retry_wait(exc.headers, delay))
                    delay *= 2
                    continue
                raise TTSError(f"HTTP {exc.code}: {body[:300]}") from None
            except OSError as exc:  # URLError, timeouts, connection resets
                if attempt < 3:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise TTSError(f"Could not reach the speech server: {exc}") from None


class CommandTTS(TTS):
    """Any voice model you can call from a shell.

    The template may use {text_file} (a UTF-8 file holding the text), {out} (the audio file
    to write, WAV) and {language}. Example:
        python my_qwen3_tts.py --text-file {text_file} --out {out}
    """
    name = "command"

    def __init__(self, template: str, language: str):
        if not template:
            raise TTSError("--tts command needs --tts-command 'your command {text_file} {out}'")
        self.template, self.language = template, language

    def synth(self, text: str, out_path: Path) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as handle:
            handle.write(text)
        try:
            try:
                command = self.template.format(text_file=shlex.quote(handle.name),
                                               out=shlex.quote(str(out_path)),
                                               language=self.language)
            except (KeyError, IndexError) as exc:
                raise TTSError(f"Unknown placeholder in --tts-command: {exc}") from None
            subprocess.run(command, shell=True, check=True)
        finally:
            os.unlink(handle.name)


def make_tts(name: str, language: str, voice: str | None = None, command: str | None = None) -> TTS:
    if name == "fake":
        return FakeTTS()
    if name == "say":
        return SayTTS(voice=voice)
    if name == "ming":
        return MingTTS(voice=voice)
    if name == "openai":
        return OpenAITTS(voice=voice)
    if name == "command":
        return CommandTTS(command or "", language)
    raise TTSError(f"Unknown voice backend: {name}")
