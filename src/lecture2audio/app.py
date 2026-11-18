"""`lecture2audio app`: a local page in the browser to drop a lecture, follow the run and read
the results.

The server is the standard library's `ThreadingHTTPServer`, for one user on one machine. It
does not run the pipeline itself: a run is the command line (`python -m lecture2audio build
<folder> ...`) started as a subprocess, so the page can never do more than the command line,
and its log lines are what the page shows. One job at a time.

Safety of a local server that a browser can reach:
- it binds 127.0.0.1 only, and refuses a request whose Host is not 127.0.0.1 or localhost on
  its own port (DNS rebinding);
- every POST needs a token drawn at start-up, which only the served page holds (CSRF);
- every file served is resolved and must lie under the output folder; an uploaded file keeps
  only a safe base name;
- API keys typed in the page go to the subprocess environment and nowhere else: they are not
  written to disk, logged, or sent back.
"""
from __future__ import annotations

import argparse
import csv
import json
import mimetypes
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import stages
from .llm import PRESETS
from .sources import discover_inputs, slugify

ASSETS = Path(__file__).parent / "assets"
MAX_UPLOAD = 2 * 1024 ** 3  # per file
CHUNK = 1 << 16
UPLOADS = "_uploads"
JOB_ID = re.compile(r"^[0-9a-f]{12}$")

# The log tag of each line -> the stage it belongs to. The stages themselves, their order and
# their descriptions come from stages.STAGES; a tag that names no stage is only a log line.
TAG_STAGE = {"inputs": "transcribe", "transcribe": "transcribe", "outline": "outline",
             "gaps": "gaps", "terms": "gaps", "research": "research", "ideas": "ideas",
             "rewrite": "rewrite", "verify": "rewrite", "factcheck": "factcheck",
             "spoken": "spoken", "voice": "audio", "roundtrip": "audio", "course": "course",
             "recitation": "recitation", "project": "project", "pdf": "documents",
             "anki": "ledger"}
assert set(TAG_STAGE.values()) <= set(stages.BY_NAME), "a log tag names an unknown stage"
# Short names for the page; a stage without one is shown under its own name.
LABELS = {"transcribe": "Reading the lecture", "outline": "Outline", "gaps": "Gaps and terms",
          "research": "Web research", "ideas": "Ideas", "rewrite": "Script, verified",
          "factcheck": "Fact check", "spoken": "Words for the voice", "audio": "Voice",
          "parts": "Audio of each part", "course": "Course cards", "vault": "Obsidian notes",
          "recitation": "Recitation notes", "project": "Project", "documents": "PDFs",
          "ledger": "Idea ledger"}
# The variable each provider reads its key from (cli.py, llm.py); only that one is set.
KEY_ENV = {**{name: env for name, (_, env) in PRESETS.items()},
           "anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY",
           "local": "L2A_LOCAL_API_KEY"}
PROVIDERS = [  # (--llm value, name in the page, needs a key, needs model names)
    ("deepseek", "DeepSeek", True, False), ("anthropic", "Claude (Anthropic)", True, False),
    ("openai", "OpenAI", True, False), ("kimi", "Kimi (Moonshot)", True, True),
    ("openrouter", "OpenRouter", True, True), ("compat", "Other compatible API", True, True),
    ("local", "Local server (Ollama, LM Studio)", False, True),
    ("dry", "Test run, no model", False, False)]
AUDIO_OUT = (".m4a", ".mp3", ".wav")
# What a finished run offers, in the order the page lists it.
RESULT_FILES = (("recitation.pdf", "Recitation notes", "pdf"), ("project.pdf", "Project", "pdf"),
                ("script.md", "Script", "text"), ("report.md", "Quality report", "text"),
                ("ideas.csv", "Idea ledger", "text"), ("recitation.tex", "LaTeX source", "text"),
                ("project.tex", "Project, LaTeX source", "text"),
                ("questions.md", "Questions", "text"), ("glossary.md", "Glossary", "text"))
TEXT_TYPES = {".md": "text/plain", ".tex": "text/plain", ".txt": "text/plain",
              ".csv": "text/csv", ".json": "application/json", ".pdf": "application/pdf",
              ".m4a": "audio/mp4", ".mp3": "audio/mpeg", ".wav": "audio/wav"}
_TAG = re.compile(r"^\[([a-z]+)\]")
_FRACTION = re.compile(r"\b(?:part|chunk)\s+(\d+)\s*/\s*(\d+)")


class Refused(Exception):
    """A request the server will not serve: (HTTP status, sentence for the page)."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def safe_name(name: str) -> str:
    """The base name of an uploaded file, with nothing that could leave the upload folder or
    hide the file. Raises Refused when nothing usable is left."""
    base = re.split(r"[\\/]", name.replace("\x00", ""))[-1].strip()
    base = re.sub(r"[^\w.\- ()\[\]+,&'@]", "_", base).lstrip(".~ ")[:180].strip()
    if not base or base in (".", ".."):
        raise Refused(400, "This file has no usable name.")
    return base


def describe(found: dict) -> str:
    """What was dropped, in one plain sentence."""
    kind, slides, glossary = found["kind"], found["slides"], found["glossary"]
    if kind is None:
        return ("Nothing to build from yet. Add a recording, a transcript (.txt .md .srt .vtt) "
                "or slides (.pdf .pptx).")
    if kind == "slides":
        text = ("Slides only: the notes will be built from the slides, and what they state "
                "without explaining will be researched.")
    elif kind == "recording":
        text = ("Recording + slides: the recording is transcribed first, and the slides guide "
                "the notes." if slides else "Recording: it is transcribed first.")
    else:
        text = ("Transcript + slides: the slides guide the notes." if slides
                else "Transcript: the notes follow what was said.")
    return text + (" The glossary fixes the spelling of the terms." if glossary else "")


def parse_line(line: str) -> tuple[str | None, float | None]:
    """(stage, fraction done) of one log line; either may be None."""
    tag = _TAG.match(line)
    stage = TAG_STAGE.get(tag.group(1)) if tag else None
    if stage == "course" and re.search(r"card\(s\), \d+ note\(s\)", line):
        stage = "vault"  # the line written once the vault is on disk
    fraction = None
    if stage:
        hits = [(int(a), int(b)) for a, b in _FRACTION.findall(line) if int(b)]
        if hits:
            done, total = hits[0]
            # "part 3/12" is printed when part 3 starts: two parts are behind it
            fraction = max(0.0, min(1.0, (done - 1) / total))
    return stage, fraction


class Job:
    """One run: its upload folder, the subprocess, and every event sent to the page (kept, so
    a page that reconnects gets them all again)."""

    def __init__(self, job_id: str, folder: Path):
        self.id, self.folder = job_id, folder
        self.status = "new"  # new, running, done, failed, cancelled
        self.name: str | None = None
        self.mode = "build"
        self.expected: list[str] = []
        self.process: subprocess.Popen | None = None
        self.events: list[dict] = []
        self.changed = threading.Condition()
        self.cancelled = False
        self.state: dict[str, str] = {}  # stage -> running | done
        self.progress: dict[str, float] = {}
        self.current: str | None = None

    def emit(self, event: dict) -> None:
        with self.changed:
            self.events.append(event)
            self.changed.notify_all()

    def public(self) -> dict:
        return {"job": self.id, "status": self.status, "name": self.name, "mode": self.mode,
                "expected": self.expected}


class App:
    """The state of the server: the output folder, the token and the job."""

    def __init__(self, out: str | Path, command: list[str] | None = None):
        self.out = Path(out).resolve()
        self.out.mkdir(parents=True, exist_ok=True)
        self.token = secrets.token_urlsafe(32)
        self.command = command or [sys.executable, "-m", "lecture2audio"]
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()
        self.port = 0

    # ------------------------------------------------------------------ uploads
    def job(self, job_id: str | None, create: bool = False) -> Job:
        with self.lock:
            if not job_id and create:
                job_id = secrets.token_hex(6)
                folder = self.out / UPLOADS / job_id
                folder.mkdir(parents=True)
                self.jobs[job_id] = Job(job_id, folder)
            if not job_id or not JOB_ID.match(job_id) or job_id not in self.jobs:
                raise Refused(404, "This upload is not known to the server. Drop the files again.")
            return self.jobs[job_id]

    def inputs(self, job: Job) -> dict:
        found = discover_inputs(job.folder)
        files = sorted(f for f in job.folder.iterdir() if f.is_file())
        role = {found["input"]: found["kind"] if found["kind"] != "slides" else None,
                found["slides"]: "slides", found["glossary"]: "glossary"}
        return {"job": job.id, "kind": found["kind"], "summary": describe(found),
                "files": [{"name": f.name, "size": f.stat().st_size,
                           "role": role.get(f) or ("not used" if f in found["skipped"] else "")}
                          for f in files]}

    # ------------------------------------------------------------------ run
    def running(self) -> Job | None:
        return next((j for j in self.jobs.values() if j.status == "running"), None)

    def arguments(self, job: Job, options: dict) -> tuple[list[str], dict]:
        """The command line and the environment of a run, from the options of the page. Every
        option is a flag of cli.build_parser; an unknown value is refused, not passed on."""
        def text(key: str) -> str:
            value = options.get(key) or ""
            if not isinstance(value, str) or "\x00" in value or len(value) > 2000:
                raise Refused(400, f"The option {key} is not valid.")
            return value.strip()

        found = discover_inputs(job.folder)
        if not found["kind"]:
            raise Refused(400, describe(found))
        llm = text("llm") or "deepseek"
        if llm not in {p[0] for p in PROVIDERS}:
            raise Refused(400, "Unknown provider.")
        language = text("language") or "en"
        if language not in ("en", "fr", "auto"):
            raise Refused(400, "Unknown language.")
        mode = "build" if options.get("audio", True) else "text"
        source = found["input"] or found["slides"]
        name = slugify(text("name") or source.stem)
        args = [mode, str(job.folder), "--out", str(self.out), "--name", name, "--llm", llm,
                "--language", language]
        if options.get("project"):
            args.append("--project")
        if not options.get("research", True):
            args += ["--research", "off"]
        if text("vault_dir"):
            args += ["--vault-dir", str(Path(text("vault_dir")).expanduser())]
        if llm != "dry":
            for key, flag in (("rewrite_model", "--rewrite-model"),
                              ("verify_model", "--verify-model"), ("base_url", "--base-url")):
                if text(key):
                    args += [flag, text(key)]
        elif mode == "build":
            args += ["--tts", "fake"]  # a test run voices tones: no model is loaded
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        env["L2A_EVENTS"] = "1"  # the run says which stage it is in (stages.Tracker)
        package_root = str(Path(__file__).resolve().parent.parent)
        env["PYTHONPATH"] = os.pathsep.join(p for p in (package_root, env.get("PYTHONPATH")) if p)
        key = text("api_key")
        if key and llm in KEY_ENV:
            env[KEY_ENV[llm]] = key
        job.name, job.mode = name, mode
        skipped = set() if mode == "build" else {"audio", "parts"}
        skipped |= {"factcheck"}  # shown when it runs (it needs MiniCheck)
        if not options.get("project"):
            skipped.add("project")
        if not options.get("research", True):
            skipped.add("research")
        job.expected = [s.name for s in stages.STAGES if s.name not in skipped]
        return [*self.command, *args], env

    def start(self, job_id: str, options: dict) -> Job:
        job = self.job(job_id)
        with self.lock:
            if self.running():
                raise Refused(409, "A run is already going. Wait for it, or cancel it.")
            if job.status not in ("new", "failed", "cancelled"):
                raise Refused(409, "This run was already started.")
            command, env = self.arguments(job, options)
            # a run that stopped starts again from its saved stages, with a fresh event list
            job.events, job.state, job.progress = [], {}, {}
            job.current, job.cancelled, job.status = None, False, "running"
        try:
            job.process = subprocess.Popen(
                command, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                bufsize=1, start_new_session=os.name == "posix")
        except OSError as exc:
            job.status = "failed"
            raise Refused(500, f"The run could not start: {exc}") from None
        job.emit({"type": "start", **job.public()})
        threading.Thread(target=self._follow, args=(job,), daemon=True).start()
        return job

    def _stage(self, job: Job, stage: str, fraction: float | None) -> None:
        if stage != job.current:
            if job.current and job.state.get(job.current) == "running":
                job.state[job.current] = "done"
                job.emit({"type": "stage", "name": job.current, "state": "done", "progress": 1})
            job.current = stage
            job.state[stage] = "running"
        elif fraction is None:
            return
        if fraction is not None:
            # parts run in parallel and their lines interleave: never move backwards
            job.progress[stage] = max(fraction, job.progress.get(stage, 0.0))
        job.emit({"type": "stage", "name": stage, "state": "running",
                  "progress": round(job.progress.get(stage, 0.0), 3)})

    def _event(self, job: Job, event: dict) -> None:
        """One line of stages.Tracker: a stage began or ended."""
        job.structured = True
        name = event.get("stage")
        if name not in stages.BY_NAME:
            return
        if event.get("state") == "running":
            self._stage(job, name, None)
        else:
            job.state[name] = "done"
            job.emit({"type": "stage", "name": name, "state": "done", "progress": 1,
                      "seconds": event.get("seconds")})
            if job.current == name:
                job.current = None

    def _follow(self, job: Job) -> None:
        lines: list[str] = []
        process = job.process
        try:
            for raw in process.stdout:
                line = raw.rstrip("\n")
                if not line.strip():
                    continue
                if line.startswith("@@stage "):  # the run's own account of its stages
                    self._event(job, json.loads(line[len("@@stage "):]))
                    continue
                lines.append(line)
                job.emit({"type": "log", "line": line})
                stage, fraction = parse_line(line)
                if not getattr(job, "structured", False):
                    if stage:  # an older build: the stage is guessed from the log tag
                        self._stage(job, stage, fraction)
                elif stage == job.current and fraction is not None:
                    self._stage(job, stage, fraction)  # "part 3/12" inside the stage
        finally:
            code = process.wait()
            job.status = ("cancelled" if job.cancelled else "done" if code == 0 else "failed")
            work = self.out / (job.name or "")
            on_disk = stages.present(work) if job.name and work.is_dir() else set()
            final = {}
            for stage in stages.STAGES:
                seen = stage.name in job.state
                if job.status == "done":
                    final[stage.name] = "done" if seen or stage.name in on_disk else "skipped"
                else:  # the stage that was running is where the run stopped
                    final[stage.name] = ("stopped" if stage.name == job.current
                                         else "done" if seen else "skipped")
            error = next((l for l in reversed(lines) if l.startswith("error:")), None) or (
                lines[-1] if lines and job.status == "failed" else None)
            job.emit({"type": "end", "status": job.status, "code": code, "name": job.name,
                      "stages": final, "error": error if job.status == "failed" else None,
                      "summary": summary(work, lines) if job.status == "done" else []})

    def cancel(self, job_id: str) -> Job:
        job = self.job(job_id)
        process = job.process
        if job.status != "running" or process is None:
            raise Refused(409, "No run is going.")
        job.cancelled = True
        _signal(process, signal.SIGTERM)

        def make_sure():
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _signal(process, getattr(signal, "SIGKILL", signal.SIGTERM))
        threading.Thread(target=make_sure, daemon=True).start()
        return job

    def close(self) -> None:
        for job in self.jobs.values():
            if job.process and job.process.poll() is None:
                job.cancelled = True
                _signal(job.process, getattr(signal, "SIGKILL", signal.SIGTERM))

    # ------------------------------------------------------------------ results
    def inside(self, relative: str) -> Path:
        """The file under the output folder that a request names, or Refused."""
        if "\x00" in relative:
            raise Refused(400, "Bad path.")
        path = (self.out / relative).resolve()
        if path != self.out and self.out not in path.parents:
            raise Refused(403, "This file is outside the output folder.")
        if not path.is_file():
            raise Refused(404, "No such file.")
        return path

    def run_info(self, name: str) -> dict:
        work = self.inside(f"{name}/report.md").parent
        if work.parent != self.out:
            raise Refused(404, "No such run.")
        files = [{"file": f, "label": label, "kind": kind, "size": (work / f).stat().st_size,
                  "url": "/files/" + urllib.parse.quote(f"{work.name}/{f}")}
                 for f, label, kind in RESULT_FILES if (work / f).is_file()]
        audio = next((work / f"{work.name}{ext}" for ext in AUDIO_OUT
                      if (work / f"{work.name}{ext}").is_file()), None)
        if audio:
            files.insert(0, {"file": audio.name, "label": "Audio", "kind": "audio",
                             "size": audio.stat().st_size,
                             "url": "/files/" + urllib.parse.quote(f"{work.name}/{audio.name}")})
        return {"name": work.name, "title": _title(work), "files": files,
                "modified": (work / "report.md").stat().st_mtime, "summary": summary(work, [])}

    def library(self) -> list[dict]:
        runs = []
        for folder in self.out.iterdir():
            report = folder / "report.md"
            if folder.is_dir() and folder.name != UPLOADS and report.is_file():
                runs.append({"name": folder.name, "title": _title(folder),
                             "modified": report.stat().st_mtime,
                             "has": [k for k, test in (
                                 ("notes", (folder / "recitation.pdf").is_file()),
                                 ("audio", any((folder / f"{folder.name}{e}").is_file()
                                               for e in AUDIO_OUT)),
                                 ("project", (folder / "project.pdf").is_file())) if test]})
        return sorted(runs, key=lambda r: -r["modified"])


def _signal(process: subprocess.Popen, sig: int) -> None:
    """Signal the run and what it started (ffmpeg, LaTeX)."""
    try:
        if os.name == "posix":
            os.killpg(process.pid, sig)
        else:
            process.terminate()
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _title(work: Path) -> str:
    try:
        return str(json.loads((work / "outline.json").read_text(encoding="utf-8")).get("title")
                   or work.name)
    except (OSError, ValueError, AttributeError):
        return work.name


def summary(work: Path, lines: list[str]) -> list[dict]:
    """A short quality summary of a finished run: the first sentence of report.md, the idea
    ledger counted by status, and the warnings of the log ([check] lines, a PDF that did not
    build). Each item: {"label", "value", "tone": "good" | "warn" | "plain"}."""
    items: list[dict] = []
    try:
        with open(work / "ideas.csv", newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
    except OSError:
        rows = []
    if rows:
        done = sum(1 for r in rows if r.get("status") == "verified")
        if not done:  # no cards: an idea is done once the script teaches it
            done = sum(1 for r in rows if r.get("status") == "scripted")
        items.append({"label": "Ideas of the lecture that arrived", "value": f"{done} of {len(rows)}",
                      "tone": "good" if done == len(rows) else "warn"})
    try:
        report = (work / "report.md").read_text(encoding="utf-8").splitlines()
    except OSError:
        report = []
    headline = next((l.strip() for l in report[1:] if l.strip() and not l.startswith(("#", "|"))),
                    None)
    if headline:
        items.append({"label": "Script", "value": headline,
                      "tone": "good" if headline.startswith("No part has") else "warn"})
    for line in lines:
        if line.startswith("[check]"):
            items.append({"label": "Check", "value": _sentence(line[7:]), "tone": "warn"})
        elif line.startswith("[pdf]") and ("did not build" in line or "no LaTeX found" in line
                                           or "was stopped" in line):
            items.append({"label": "PDF", "value": _sentence(line[5:]), "tone": "warn"})
        elif line.startswith("[roundtrip]") and "checked" in line:
            items.append({"label": "Voice", "value": _sentence(line[11:]), "tone": "plain"})
        elif line.startswith("[gaps]") and "gap(s)" in line:
            items.append({"label": "Gaps", "value": _sentence(line[6:]), "tone": "plain"})
    return items


def _sentence(text: str) -> str:
    text = text.strip()
    return text[:1].upper() + text[1:] + ("" if text.endswith((".", ")")) else ".")


# ---------------------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "lecture2audio"
    app: App  # set by make_server

    def log_message(self, *args) -> None:  # the terminal stays quiet; nothing is logged
        pass

    # -- plumbing
    def _send(self, status: int, body: bytes, content_type: str, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, data, status: int = 200) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False).encode(), "application/json")

    def _refuse(self, refused: Refused) -> None:
        self.close_connection = True  # a body may be unread
        self._json({"error": refused.message}, refused.status)

    def _local(self) -> None:
        """Only this page, on this machine, talks to the server."""
        allowed = {f"127.0.0.1:{self.app.port}", f"localhost:{self.app.port}"}
        if (self.headers.get("Host") or "").strip().lower() not in allowed:
            raise Refused(403, "This server answers only at 127.0.0.1.")
        origin = self.headers.get("Origin")
        if origin and origin.lower() not in {f"http://{host}" for host in allowed}:
            raise Refused(403, "This request comes from another site.")

    def _token(self) -> None:
        given = self.headers.get("X-L2A-Token") or ""
        if not secrets.compare_digest(given.encode(), self.app.token.encode()):
            raise Refused(403, "The page's token is missing or wrong. Reload the page.")

    def _body(self, limit: int = 1 << 20) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise Refused(400, "Bad request.") from None
        if not 0 <= length <= limit:
            raise Refused(413, "The request is too large.")
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            raise Refused(400, "The request is not JSON.") from None
        if not isinstance(data, dict):
            raise Refused(400, "The request is not a JSON object.")
        return data

    def _route(self, routes: dict) -> None:
        url = urllib.parse.urlsplit(self.path)
        query = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        try:
            self._local()
            if self.command == "POST":
                self._token()
            path = urllib.parse.unquote(url.path)
            for prefix, action in routes.items():
                if path == prefix or (len(prefix) > 1 and prefix.endswith("/")
                                      and path.startswith(prefix)):
                    return action(path[len(prefix):], query)
            raise Refused(404, "Not found.")
        except Refused as refused:
            self._refuse(refused)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def do_GET(self) -> None:
        self._route({"/": self._page, "/api/config": self._config, "/api/inputs": self._inputs,
                     "/api/events": self._events, "/api/runs": self._runs,
                     "/api/runs/": self._run, "/files/": self._file})

    do_HEAD = do_GET

    def do_POST(self) -> None:
        self._route({"/api/upload": self._upload, "/api/remove": self._remove,
                     "/api/run": self._start, "/api/cancel": self._cancel})

    # -- GET
    def _page(self, _rest: str, _query: dict) -> None:
        nonce = secrets.token_urlsafe(16)
        html = (ASSETS / "app.html").read_text(encoding="utf-8")
        html = html.replace("__L2A_TOKEN__", self.app.token).replace("__L2A_NONCE__", nonce)
        self._send(200, html.encode(), "text/html; charset=utf-8", {
            "Content-Security-Policy":
                f"default-src 'self'; script-src 'nonce-{nonce}'; style-src 'unsafe-inline'; "
                "img-src 'self' data:; object-src 'self'; frame-src 'self'; "
                "frame-ancestors 'self'; base-uri 'none'; form-action 'none'"})

    def _config(self, _rest: str, _query: dict) -> None:
        job = self.app.running() or next(
            (j for j in reversed(list(self.app.jobs.values())) if j.status != "new"), None)
        self._json({
            "stages": [{"name": s.name, "label": LABELS.get(s.name, s.name.capitalize()),
                        "about": s.about} for s in stages.STAGES],
            "providers": [{"id": pid, "label": label, "key": needs_key, "models": needs_models,
                           # whether a key is already set, never the key itself
                           "has_key": bool(os.environ.get(KEY_ENV.get(pid, "")))}
                          for pid, label, needs_key, needs_models in PROVIDERS],
            "vault_dir": os.environ.get("L2A_VAULT_DIR") or "",
            "out": str(self.app.out), "job": job.public() if job else None})

    def _inputs(self, _rest: str, query: dict) -> None:
        self._json(self.app.inputs(self.app.job(query.get("job"))))

    def _runs(self, _rest: str, _query: dict) -> None:
        self._json({"runs": self.app.library()})

    def _run(self, rest: str, _query: dict) -> None:
        if "/" in rest or rest in ("", ".", "..", UPLOADS):
            raise Refused(404, "No such run.")
        self._json(self.app.run_info(rest))

    def _events(self, _rest: str, query: dict) -> None:
        """Server-Sent Events: every event of the job from `Last-Event-ID` on, then the new
        ones as they come, until the run ends."""
        job = self.app.job(query.get("job"))
        try:
            sent = int(self.headers.get("Last-Event-ID") or query.get("from") or 0)
        except ValueError:
            sent = 0
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        while True:
            with job.changed:
                if len(job.events) <= sent:
                    job.changed.wait(timeout=15)
                batch = job.events[sent:]
            if not batch:
                self.wfile.write(b": waiting\n\n")  # keeps the connection, finds a closed page
                self.wfile.flush()
                continue
            for event in batch:
                sent += 1
                self.wfile.write(f"id: {sent}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                                 .encode())
            self.wfile.flush()
            if batch[-1]["type"] == "end":
                return

    def _file(self, rest: str, query: dict) -> None:
        """A file of the output folder, with Range support so a player can seek."""
        if rest.split("/")[0] == UPLOADS:
            raise Refused(403, "Uploads are not served.")
        path = self.app.inside(rest)
        size = path.stat().st_size
        start, end, status = 0, size - 1, 200
        spec = self.headers.get("Range")
        if spec:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", spec.strip())
            if not match or not (match.group(1) or match.group(2)):
                raise Refused(416, "Bad range.")
            if match.group(1):
                start = int(match.group(1))
                end = min(int(match.group(2)), size - 1) if match.group(2) else size - 1
            else:  # the last N bytes
                start = max(0, size - int(match.group(2)))
            if start > end or start >= size:
                self.close_connection = True
                return self._send(416, b"", "text/plain", {"Content-Range": f"bytes */{size}"})
            status = 206
        kind = TEXT_TYPES.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or (
            "application/octet-stream")
        if kind.startswith("text/"):
            kind += "; charset=utf-8"
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; "
                                                    "frame-ancestors 'self'")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        quoted = urllib.parse.quote(path.name)
        self.send_header("Content-Disposition",
                         f"{'attachment' if query.get('download') else 'inline'}; "
                         f"filename*=UTF-8''{quoted}")
        self.end_headers()
        if self.command == "HEAD":
            return
        left = end - start + 1
        with open(path, "rb") as handle:
            handle.seek(start)
            while left > 0:
                block = handle.read(min(CHUNK, left))
                if not block:
                    break
                self.wfile.write(block)
                left -= len(block)

    # -- POST
    def _upload(self, _rest: str, query: dict) -> None:
        """One file per request, its bytes as the body, streamed to disk."""
        name = safe_name(query.get("name") or "")
        try:
            length = int(self.headers.get("Content-Length") or "")
        except ValueError:
            raise Refused(411, "The size of the file is missing.") from None
        if length < 0 or length > MAX_UPLOAD:
            raise Refused(413, f"{name} is larger than 2 GB.")
        job = self.app.job(query.get("job"), create=True)
        if job.status in ("running", "done"):
            raise Refused(409, "This run was already started.")
        target = job.folder / name
        if target.resolve().parent != job.folder.resolve():
            raise Refused(400, "This file has no usable name.")
        partial = job.folder / f".{secrets.token_hex(4)}.part"  # dot files are not inputs
        left = length
        try:
            with open(partial, "wb") as handle:
                while left > 0:
                    block = self.rfile.read(min(CHUNK, left))
                    if not block:
                        raise Refused(400, f"The upload of {name} was cut short.")
                    handle.write(block)
                    left -= len(block)
            os.replace(partial, target)
        finally:
            partial.unlink(missing_ok=True)
        self._json(self.app.inputs(job))

    def _remove(self, _rest: str, _query: dict) -> None:
        data = self._body()
        job = self.app.job(data.get("job"))
        if job.status in ("running", "done"):
            raise Refused(409, "This run was already started.")
        target = job.folder / safe_name(str(data.get("name") or ""))
        if target.resolve().parent == job.folder.resolve() and target.is_file():
            target.unlink()
        self._json(self.app.inputs(job))

    def _start(self, _rest: str, _query: dict) -> None:
        data = self._body()
        options = data.get("options")
        job = self.app.start(data.get("job"), options if isinstance(options, dict) else {})
        self._json(job.public())

    def _cancel(self, _rest: str, _query: dict) -> None:
        self._json(self.app.cancel(self._body().get("job")).public())


def make_server(out: str | Path = "out", port: int = 8765,
                command: list[str] | None = None) -> ThreadingHTTPServer:
    """The server, bound to 127.0.0.1 (port 0 = any free port). Its state is `server.app`."""
    app = App(out, command)
    handler = type("AppHandler", (Handler,), {"app": app})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    app.port = server.server_address[1]
    server.app = app
    return server


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="lecture2audio app",
        description="Open the lecture2audio page in the browser: drop a lecture, follow the "
                    "run, read the notes and listen. The server answers only on this machine.")
    parser.add_argument("--port", type=int, default=8765, help="port on 127.0.0.1 (default 8765)")
    parser.add_argument("--out", default="out", help="output folder (default: ./out)")
    parser.add_argument("--no-open", action="store_true", help="do not open the browser")
    args = parser.parse_args(argv)
    try:
        server = make_server(args.out, args.port)
    except OSError as exc:
        print(f"error: port {args.port} cannot be used ({exc}). Try --port {args.port + 1}.",
              file=sys.stderr)
        return 1
    address = f"http://127.0.0.1:{server.app.port}/"
    print(f"lecture2audio is at {address} (results in {server.app.out}). Ctrl+C stops it.",
          flush=True)
    if not args.no_open:
        threading.Timer(0.3, webbrowser.open, [address]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.app.close()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
