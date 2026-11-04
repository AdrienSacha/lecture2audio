"""Local web interface: a form for `build` and `text`, a live log, and the results.

Run with `python3 -m lecture2audio.gui` (add `--no-browser` to skip opening the page).
The server binds to 127.0.0.1 only, refuses requests whose Host is not local (DNS
rebinding), and asks for a per-session token on every POST. API keys typed in the
page are passed to the job as environment variables only: never written to disk,
never logged, never sent back to the page.
"""
from __future__ import annotations

import argparse
import hmac
import html
import json
import mimetypes
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from .sources import slugify

SRC_DIR = Path(__file__).resolve().parents[1]
KEY_NAMES = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "MOONSHOT_API_KEY",
             "OPENROUTER_API_KEY", "L2A_COMPAT_API_KEY", "TAVILY_API_KEY", "BRAVE_API_KEY")
DOCS = ("report", "script", "questions", "glossary", "index", "reference")
AUDIO_FORMATS = ("m4a", "mp3", "wav")
PATH_FIELDS = ("input", "slides", "glossary", "prompts_dir", "out")
UPLOAD_FIELDS = ("input", "slides", "glossary")
MAX_UPLOAD = 4 * 1024 ** 3
MIME = {".m4a": "audio/mp4", ".mp3": "audio/mpeg", ".wav": "audio/wav", ".md": "text/plain",
        ".txt": "text/plain", ".json": "application/json"}


# ---------------------------------------------------------------- options

def option_spec() -> dict:
    """Describe the options of `build` and `text`, read from the command line parser."""
    from .cli import build_parser

    parser = build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    spec = {}
    for command, subparser in sub.choices.items():
        if command not in ("build", "text"):  # e.g. `series` takes several inputs: CLI only
            continue
        groups = []
        for group in subparser._action_groups:
            fields = []
            for action in group._group_actions:
                if isinstance(action, argparse._HelpAction):
                    continue
                if isinstance(action, (argparse._StoreTrueAction,)):
                    kind = "flag"
                elif action.choices:
                    kind = "choice"
                elif action.type is int:
                    kind = "int"
                elif action.type is float:
                    kind = "float"
                else:
                    kind = "text"
                if action.dest in PATH_FIELDS:
                    kind = "path"
                default = action.default
                if default is argparse.SUPPRESS or kind == "flag":
                    default = None
                fields.append({
                    "dest": action.dest,
                    "flag": max(action.option_strings, key=len) if action.option_strings else None,
                    "kind": kind,
                    "choices": list(action.choices) if action.choices else None,
                    "default": default,
                    "help": (action.help or "").replace("%%", "%"),
                    "required": not action.option_strings,
                    "upload": action.dest in UPLOAD_FIELDS,
                })
            if fields:
                title = group.title
                if title in ("positional arguments", "options", "optional arguments"):
                    title = "lecture"
                if groups and groups[-1]["title"] == title:
                    groups[-1]["fields"].extend(fields)
                else:
                    groups.append({"title": title, "fields": fields})
        spec[command] = {"help": subparser.description, "groups": groups}
    return spec


def _fields(spec: dict, command: str) -> dict:
    return {f["dest"]: f for g in spec[command]["groups"] for f in g["fields"]}


def build_argv(spec: dict, command: str, values: dict, out_root: Path, cwd: Path) -> list[str]:
    """Turn the form values into command line arguments; raise ValueError on bad input."""
    if command not in spec:
        raise ValueError(f"unknown command: {command}")
    fields = _fields(spec, command)
    values = dict(values)
    values["out"] = str(out_root)
    source = str(values.get("input") or "").strip()
    if not source:
        raise ValueError("Choose an input file.")
    argv = [command, str((cwd / Path(source).expanduser()).resolve())]
    for dest, value in values.items():
        if dest == "input":
            continue
        field = fields.get(dest)
        if field is None:
            raise ValueError(f"unknown option for {command}: {dest}")
        if value is None or value == "" or value is False:
            continue
        flag = field["flag"]
        kind = field["kind"]
        if kind == "flag":
            if value is not True:
                raise ValueError(f"{flag} is a yes/no option")
            argv.append(flag)
            continue
        value = str(value).strip()
        if "\n" in value or "\r" in value or "\0" in value:
            raise ValueError(f"{flag}: one line only")
        if kind == "choice" and value not in field["choices"]:
            raise ValueError(f"{flag}: choose one of {', '.join(field['choices'])}")
        if kind == "int":
            int(value)
        if kind == "float":
            float(value)
        if kind == "path":
            value = str((cwd / Path(value).expanduser()).resolve())
        argv.append(f"{flag}={value}")
    return argv


# ---------------------------------------------------------------- markdown

def _inline(text: str) -> str:
    parts = re.split(r"(`[^`]+`)", text)
    out = []
    for part in parts:
        if len(part) > 1 and part.startswith("`") and part.endswith("`"):
            out.append(f"<code>{html.escape(part[1:-1])}</code>")
            continue
        s = html.escape(part)
        s = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", r"\1", s)
        s = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"(?<![\w*])\*(?=\S)(.+?)(?<=\S)\*(?![\w*])", r"<em>\1</em>", s)
        s = re.sub(r"(?<![\w])_(?=\S)(.+?)(?<=\S)_(?![\w])", r"<em>\1</em>", s)
        out.append(s)
    return "".join(out)


LIST_ITEM = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _cells(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [c.strip() for c in line.split("|")]


def render_markdown(text: str) -> str:
    """Small Markdown subset to HTML. Every piece of text is escaped."""
    lines = text.splitlines()
    out: list[str] = []
    para: list[str] = []
    i, n = 0, len(lines)

    def flush():
        if para:
            out.append("<p>" + _inline(" ".join(s.strip() for s in para)) + "</p>")
            para.clear()

    while i < n:
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("```"):
            flush()
            i += 1
            code = []
            while i < n and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            out.append("<pre><code>" + html.escape("\n".join(code)) + "</code></pre>")
            i += 1
            continue
        if not stripped:
            flush()
            i += 1
            continue
        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading:
            flush()
            level = len(heading.group(1))
            out.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            i += 1
            continue
        if re.match(r"^(-{3,}|\*{3,}|_{3,})$", stripped):
            flush()
            out.append("<hr>")
            i += 1
            continue
        if stripped.startswith("|") and i + 1 < n and TABLE_RULE.match(lines[i + 1]):
            flush()
            head = _cells(lines[i])
            i += 2
            rows = []
            while i < n and lines[i].strip().startswith("|"):
                rows.append(_cells(lines[i]))
                i += 1
            cells = "".join(f"<th>{_inline(c)}</th>" for c in head)
            body = "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>"
                           for r in rows)
            out.append(f'<div class="table"><table><thead><tr>{cells}</tr></thead>'
                       f"<tbody>{body}</tbody></table></div>")
            continue
        if stripped.startswith(">"):
            flush()
            quote_lines = []
            while i < n and lines[i].strip().startswith(">"):
                quote_lines.append(lines[i].strip()[1:].strip())
                i += 1
            out.append("<blockquote>" + render_markdown("\n".join(quote_lines)) + "</blockquote>")
            continue
        if LIST_ITEM.match(line):
            flush()
            items = []
            while i < n:
                m = LIST_ITEM.match(lines[i])
                if m:
                    tag = "ol" if m.group(2)[0].isdigit() else "ul"
                    items.append([len(m.group(1).expandtabs(4)), tag, m.group(3)])
                elif lines[i].strip() and lines[i][:1].isspace() and items:
                    items[-1][2] += " " + lines[i].strip()
                else:
                    break
                i += 1
            stack: list[list] = []
            for indent, tag, content in items:
                if stack and indent > stack[-1][0]:
                    out.append(f"<{tag}>")
                    stack.append([indent, tag])
                else:
                    while len(stack) > 1 and indent < stack[-1][0]:
                        out.append(f"</li></{stack.pop()[1]}>")
                    if stack and stack[-1][1] != tag:
                        out.append(f"</li></{stack.pop()[1]}>")
                    if stack:
                        out.append("</li>")
                    else:
                        out.append(f"<{tag}>")
                        stack.append([indent, tag])
                out.append("<li>" + _inline(content))
            while stack:
                out.append(f"</li></{stack.pop()[1]}>")
            continue
        para.append(line)
        i += 1
    flush()
    return "\n".join(out)


# ---------------------------------------------------------------- results

def find_audio(run_dir: Path) -> Path | None:
    found = [run_dir / f"{run_dir.name}.{ext}" for ext in AUDIO_FORMATS]
    found = [p for p in found if p.is_file()]
    return max(found, key=lambda p: p.stat().st_mtime) if found else None


def read_chapters(audio: Path) -> list[dict]:
    if not shutil.which("ffprobe"):
        return []
    try:
        done = subprocess.run(["ffprobe", "-v", "error", "-show_chapters", "-of", "json",
                               str(audio)], capture_output=True, text=True, timeout=30)
        data = json.loads(done.stdout or "{}")
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    return [{"title": c.get("tags", {}).get("title", ""), "start": float(c["start_time"]),
             "end": float(c["end_time"])} for c in data.get("chapters", [])]


def list_runs(out_root: Path) -> list[dict]:
    runs = []
    if not out_root.is_dir():
        return runs
    for d in out_root.iterdir():
        if not d.is_dir() or d.name[:1] in "._":
            continue
        if not ((d / "transcript.txt").exists() or (d / "report.md").exists()):
            continue
        audio = find_audio(d)
        runs.append({"name": d.name, "mtime": d.stat().st_mtime,
                     "docs": [doc for doc in DOCS if (d / f"{doc}.md").is_file()],
                     "audio": audio.name if audio else None})
    runs.sort(key=lambda r: r["mtime"], reverse=True)
    return runs


# ---------------------------------------------------------------- job

class Job:
    def __init__(self, argv: list[str], env: dict, out_root: Path, name: str, secrets_: list[str]):
        self.argv = argv
        self.out = str(out_root)
        self.name = name
        self.lines: list[str] = []
        self.state = "running"
        self.returncode: int | None = None
        self.started = time.time()
        self.cancelled = False
        self._secrets = [s for s in secrets_ if s]
        self._lock = threading.Lock()
        kwargs = {}
        if os.name == "posix":
            kwargs["start_new_session"] = True
        self.proc = subprocess.Popen([sys.executable, "-m", "lecture2audio", *argv], env=env,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, text=True, bufsize=1,
                                     errors="replace", **kwargs)
        self.add("$ lecture2audio " + " ".join(argv))
        threading.Thread(target=self._read, daemon=True).start()

    def add(self, line: str) -> None:
        for s in self._secrets:
            line = line.replace(s, "[hidden]")
        with self._lock:
            self.lines.append(line)

    def _read(self) -> None:
        for line in self.proc.stdout:
            self.add(line.rstrip("\n"))
        code = self.proc.wait()
        with self._lock:
            self.returncode = code
            self.state = "cancelled" if self.cancelled else ("done" if code == 0 else "failed")
        self.add(f"[gui] finished: {self.state} (exit code {code})")

    def cancel(self) -> None:
        if self.proc.poll() is not None:
            return
        self.cancelled = True
        self.add("[gui] cancelling")

        def stop():
            try:
                if os.name == "posix":
                    os.killpg(self.proc.pid, signal.SIGINT)
                else:
                    self.proc.terminate()
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                if os.name == "posix":
                    os.killpg(self.proc.pid, signal.SIGKILL)
                else:
                    self.proc.kill()
            except (ProcessLookupError, PermissionError):
                pass

        threading.Thread(target=stop, daemon=True).start()

    def snapshot(self, since: int) -> dict:
        with self._lock:
            return {"state": self.state, "returncode": self.returncode, "out": self.out,
                    "name": self.name, "lines": self.lines[since:], "next": len(self.lines)}


# ---------------------------------------------------------------- server

class GuiServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int, out_root: Path, cwd: Path | None = None):
        super().__init__(("127.0.0.1", port), Handler)
        self.cwd = (cwd or Path.cwd()).resolve()
        self.out_root = (self.cwd / out_root).resolve()
        self.roots = {self.out_root}
        self.token = secrets.token_urlsafe(24)
        self.spec = option_spec()
        self.job: Job | None = None
        self.lock = threading.Lock()

    @property
    def port(self) -> int:
        return self.server_address[1]

    def stop_job(self) -> None:
        if self.job:
            self.job.cancel()


class Handler(BaseHTTPRequestHandler):
    server: GuiServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):  # quiet: no query strings in the terminal
        pass

    # -- helpers
    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").lower()
        port = self.server.port
        allowed = {f"{h}:{port}" for h in ("127.0.0.1", "localhost", "[::1]")}
        if host not in allowed:
            return False
        origin = self.headers.get("Origin")
        if origin and origin.lower() not in {f"http://{h}" for h in allowed}:
            return False
        return True

    def _headers(self, status: int, ctype: str, length: int, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if "X-Frame-Options" not in (extra or {}):
            self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None):
        self._headers(status, ctype, len(body), extra)
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, data, status: int = 200):
        self._send(status, json.dumps(data).encode(), "application/json; charset=utf-8")

    def _error(self, status: int, message: str):
        # The request body may be unread: do not reuse this connection.
        self.close_connection = True
        self._send(status, json.dumps({"error": message}).encode(),
                   "application/json; charset=utf-8", {"Connection": "close"})

    def _query(self) -> dict:
        return {k: v[-1] for k, v in parse_qs(urlsplit(self.path).query).items()}

    def _root(self, q: dict) -> Path | None:
        raw = q.get("out")
        root = self.server.out_root if not raw else (self.server.cwd / raw).resolve()
        return root if root in self.server.roots else None

    def _run_dir(self, q: dict) -> Path | None:
        root = self._root(q)
        name = q.get("run", "")
        if root is None or not name or "/" in name or "\\" in name or name in (".", ".."):
            return None
        run = (root / name).resolve()
        return run if run.parent == root and run.is_dir() else None

    def _body(self, limit: int = 1 << 20) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length < 0 or length > limit:
            raise ValueError("request too large")
        return self.rfile.read(length)

    # -- verbs
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self._host_ok():
            return self._error(403, "This server only answers requests for localhost.")
        path = urlsplit(self.path).path
        q = self._query()
        if path == "/":
            nonce = secrets.token_urlsafe(16)
            page = PAGE.replace("__TOKEN__", self.server.token).replace("__NONCE__", nonce)
            csp = (f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
                   "connect-src 'self'; media-src 'self'; img-src 'self' data:; "
                   "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
            return self._send(200, page.encode(), "text/html; charset=utf-8",
                              {"Content-Security-Policy": csp})
        if path == "/api/options":
            return self._json({"spec": self.server.spec, "out": str(self.server.out_root),
                               "cwd": str(self.server.cwd),
                               "env_keys": {k: bool(os.environ.get(k)) for k in KEY_NAMES}})
        if path == "/api/runs":
            root = self._root(q)
            if root is None:
                return self._error(404, "Unknown output folder.")
            return self._json({"out": str(root), "runs": list_runs(root)})
        if path == "/api/job":
            job = self.server.job
            if job is None:
                return self._json({"state": "idle", "lines": [], "next": 0})
            try:
                since = max(0, int(q.get("since", 0)))
            except ValueError:
                since = 0
            return self._json(job.snapshot(since))
        if path == "/api/doc":
            run = self._run_dir(q)
            doc = q.get("doc")
            if run is None or doc not in DOCS:
                return self._error(404, "No such document.")
            file = run / f"{doc}.md"
            if not file.is_file():
                return self._json({"exists": False, "html": ""})
            return self._json({"exists": True,
                               "html": render_markdown(file.read_text(encoding="utf-8",
                                                                      errors="replace"))})
        if path == "/api/vault":
            given = str(q.get("path") or "").strip()
            if given:
                vault = Path(given).expanduser().resolve()
            else:
                run = self._run_dir(q)
                if run is None:
                    return self._error(404, "No such run.")
                vault = run / "vault"
            if not vault.is_dir():
                return self._json({"exists": False})
            index = next(iter(sorted(vault.glob("00 *.md"))), None) or next(iter(sorted(vault.glob("*.md"))), None)
            return self._json({"exists": True, "path": str(vault),
                               "index": str(index) if index else None,
                               "notes": sum(1 for _ in vault.rglob("*.md"))})
        if path == "/api/chapters":
            run = self._run_dir(q)
            if run is None:
                return self._error(404, "No such run.")
            audio = find_audio(run)
            return self._json({"audio": audio.name if audio else None,
                               "chapters": read_chapters(audio) if audio else []})
        if path.startswith("/files/"):
            return self._file(unquote(path[len("/files/"):]), q)
        return self._error(404, "Not found.")

    def _file(self, rel: str, q: dict):
        root = self._root(q)
        if root is None:
            return self._error(404, "Unknown output folder.")
        target = (root / rel).resolve()
        if not target.is_relative_to(root) or target == root or not target.is_file():
            return self._error(404, "Not found.")
        size = target.stat().st_size
        ctype = MIME.get(target.suffix.lower()) or mimetypes.guess_type(target.name)[0] \
            or "application/octet-stream"
        if ctype.startswith("text/"):
            ctype += "; charset=utf-8"
        start, end, status = 0, size - 1, 200
        rng = re.match(r"^bytes=(\d*)-(\d*)$", self.headers.get("Range") or "")
        if rng and size:
            a, b = rng.groups()
            if a:
                start, end = int(a), min(int(b), size - 1) if b else size - 1
            elif b:
                start, end = max(0, size - int(b)), size - 1
            if start > end or start >= size:
                return self._send(416, b"", "text/plain", {"Content-Range": f"bytes */{size}"})
            status = 206
        length = end - start + 1 if size else 0
        extra = {"Accept-Ranges": "bytes"}
        if target.suffix.lower() in (".html", ".htm"):
            # Generated pages (course.html) are shown in a sandboxed frame: their scripts run
            # in an opaque origin and cannot reach this interface, its token or its API. The
            # page's own meta CSP still applies; this header only adds the sandbox.
            extra["Content-Security-Policy"] = "sandbox allow-scripts"
            extra["X-Frame-Options"] = "SAMEORIGIN"
        if status == 206:
            extra["Content-Range"] = f"bytes {start}-{end}/{size}"
        self._headers(status, ctype, length, extra)
        if self.command == "HEAD":
            return
        with target.open("rb") as f:
            f.seek(start)
            left = length
            while left > 0:
                chunk = f.read(min(1 << 16, left))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                left -= len(chunk)

    def do_POST(self):
        if not self._host_ok():
            return self._error(403, "This server only answers requests for localhost.")
        token = self.headers.get("X-L2A-Token") or ""
        if not hmac.compare_digest(token.encode(), self.server.token.encode()):
            return self._error(403, "Missing or wrong session token. Reload the page.")
        path = urlsplit(self.path).path
        try:
            if path == "/api/upload":
                return self._upload()
            data = json.loads(self._body() or b"{}")
            if not isinstance(data, dict):
                raise ValueError("expected a JSON object")
        except (ValueError, UnicodeDecodeError) as exc:
            return self._error(400, str(exc))
        if path == "/api/run":
            return self._run(data)
        if path == "/api/cancel":
            self.server.stop_job()
            return self._json({"ok": True})
        if path == "/api/reveal":
            target = Path(str(data.get("path") or "")).expanduser().resolve()
            if not target.exists():
                return self._error(404, "This folder does not exist.")
            if sys.platform == "darwin":  # a file is selected in its folder, a folder is opened
                subprocess.Popen(["open", "-R", str(target)] if target.is_file()
                                 else ["open", str(target)])
            return self._json({"ok": True})
        if path == "/api/open-out":
            raw = str(data.get("out") or "").strip()
            if not raw:
                return self._error(400, "Give a folder.")
            root = (self.server.cwd / Path(raw).expanduser()).resolve()
            if root.exists() and not root.is_dir():
                return self._error(400, "This is not a folder.")
            self.server.roots.add(root)
            return self._json({"out": str(root), "runs": list_runs(root)})
        return self._error(404, "Not found.")

    def _upload(self):
        q = self._query()
        name = Path(q.get("name") or "upload").name
        name = re.sub(r"[^\w.\- ]+", "_", name).strip(". ") or "upload"
        length = int(self.headers.get("Content-Length") or -1)
        if length < 0:
            return self._error(411, "Content-Length required.")
        if length > MAX_UPLOAD:
            return self._error(413, "File too large.")
        folder = self.server.out_root / "_uploads" / secrets.token_hex(6)
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / name
        left = length
        with target.open("wb") as f:
            while left > 0:
                chunk = self.rfile.read(min(1 << 16, left))
                if not chunk:
                    break
                f.write(chunk)
                left -= len(chunk)
        if left:
            target.unlink(missing_ok=True)
            return self._error(400, "Upload interrupted.")
        return self._json({"path": str(target)})

    def _run(self, data: dict):
        server = self.server
        command = str(data.get("command") or "build")
        values = data.get("values") or {}
        keys = data.get("keys") or {}
        if not isinstance(values, dict) or not isinstance(keys, dict):
            return self._error(400, "Bad request.")
        raw_out = str(values.get("out") or "").strip()
        out_root = (server.cwd / Path(raw_out).expanduser()).resolve() if raw_out \
            else server.out_root
        try:
            argv = build_argv(server.spec, command, values, out_root, server.cwd)
        except ValueError as exc:
            return self._error(400, str(exc))
        source = Path(argv[1])
        if not source.is_file():
            return self._error(400, f"Input not found: {source}")
        name = str(values.get("name") or "").strip() or slugify(source.stem)
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(p for p in (str(SRC_DIR), env.get("PYTHONPATH")) if p)
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        typed = []
        for k in KEY_NAMES:
            v = keys.get(k)
            if isinstance(v, str) and v.strip():
                env[k] = v.strip()
                typed.append(v.strip())
        with server.lock:
            if server.job and server.job.state == "running":
                return self._error(409, "A job is already running. Cancel it first.")
            out_root.mkdir(parents=True, exist_ok=True)
            server.roots.add(out_root)
            try:
                server.job = Job(argv, env, out_root, name, typed)
            except OSError as exc:
                return self._error(500, f"Could not start the job: {exc}")
        return self._json({"ok": True, "name": name, "out": str(out_root)})


def make_server(port: int = 8765, out: str | Path = "out", cwd: Path | None = None) -> GuiServer:
    return GuiServer(port, Path(out), cwd)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="lecture2audio gui",
                                     description="Open the lecture2audio interface in the browser.")
    parser.add_argument("--port", type=int, default=8765, help="local port (default 8765)")
    parser.add_argument("--out", default="out", help="output folder (default: ./out)")
    parser.add_argument("--no-browser", action="store_true", help="do not open the browser")
    args = parser.parse_args(argv)
    try:
        server = make_server(args.port, args.out)
    except OSError as exc:
        raise SystemExit(f"Cannot listen on 127.0.0.1:{args.port}: {exc}. Try --port.")
    url = f"http://127.0.0.1:{server.port}/"
    print(f"lecture2audio interface: {url}  (Ctrl+C to stop)", flush=True)
    if not args.no_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping.", flush=True)
    finally:
        server.stop_job()
        server.server_close()


# ---------------------------------------------------------------- page

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<meta name="l2a-token" content="__TOKEN__">
<title>lecture2audio</title>
<style nonce="__NONCE__">
/* Flat, dark, one accent for actions. Nothing is loaded from outside this page. */
:root {
  color-scheme: dark;
  --bg: #0a1114; --surface: #101a1e; --line: #1f3035; --line-strong: #2c454b;
  --ink: #e7f0ee; --muted: #8fa7a3; --faint: #5f7774;
  --primary: #14b8a6; --primary-ink: #04201c; --primary-soft: rgba(20, 184, 166, 0.12);
  --action: #fb8a3c; --action-ink: #1d0d02; --bad: #f87171; --warn: #fbbf24; --ok: #34d399;
  --radius: 12px; --font: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  --mono: "JetBrains Mono", "SF Mono", ui-monospace, Menlo, Consolas, monospace;
}
* { box-sizing: border-box; }
[hidden] { display: none !important; }
body { margin: 0; background: var(--bg); color: var(--ink); font: 15px/1.55 var(--font);
  -webkit-font-smoothing: antialiased; }
button, input, select { font: inherit; color: inherit; }
button { cursor: pointer; }
:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
@media (prefers-reduced-motion: no-preference) {
  button, .chip, .drop, .stage { transition: background-color .15s ease, border-color .15s ease, color .15s ease; }
}

/* Top bar */
.top { position: sticky; top: env(safe-area-inset-top, 0px); z-index: 5; background: rgba(10, 17, 20, .92);
  backdrop-filter: blur(8px); border-bottom: 1px solid var(--line); }
.top-in { max-width: 1280px; margin: 0 auto; padding: 12px 20px; display: flex; align-items: center; gap: 12px; }
.brand { display: flex; align-items: center; gap: 10px; font-weight: 700; letter-spacing: -0.01em; font-size: 17px; }
.brand svg { color: var(--primary); }
.pill { margin-left: auto; font-size: 13px; padding: 3px 10px; border-radius: 99px; border: 1px solid var(--line-strong);
  color: var(--muted); display: inline-flex; align-items: center; gap: 6px; }
.pill::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: var(--faint); }
.pill.running { color: var(--primary); border-color: var(--primary); }
.pill.running::before { background: var(--primary); }
.pill.done { color: var(--ok); } .pill.done::before { background: var(--ok); }
.pill.failed, .pill.cancelled { color: var(--bad); } .pill.failed::before, .pill.cancelled::before { background: var(--bad); }

main { max-width: 1280px; margin: 0 auto; padding: 20px; display: grid; gap: 20px; align-items: start;
  grid-template-columns: minmax(320px, 460px) minmax(0, 1fr); }
@media (max-width: 900px) { main { grid-template-columns: 1fr; padding: 16px; } }
.col { display: grid; gap: 16px; min-width: 0; }
.card { background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius); padding: 18px; min-width: 0; }
.card > h2, .step-head h2 { font-size: 15px; margin: 0; font-weight: 650; }
.card-head { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }
.card-head h2 { margin: 0; font-size: 15px; font-weight: 650; }
.card-head .spacer { flex: 1; }

/* Steps */
.step + .step { margin-top: 22px; padding-top: 18px; border-top: 1px solid var(--line); }
.step-head { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }
.num { width: 24px; height: 24px; border-radius: 50%; display: grid; place-items: center; flex: none;
  font-size: 13px; font-weight: 700; background: var(--primary-soft); color: var(--primary); }

/* Fields */
.field { margin: 10px 0; }
.field > label, .label { display: block; font-size: 13px; font-weight: 600; margin-bottom: 5px; color: var(--ink); }
.help { font-size: 13px; color: var(--muted); margin-top: 4px; }
input[type=text], input[type=password], input[type=number], select {
  width: 100%; min-width: 0; min-height: 40px; padding: 8px 11px; background: var(--bg); border: 1px solid var(--line-strong);
  border-radius: 8px; }
input::placeholder { color: var(--faint); }
input:hover, select:hover { border-color: #3d5c62; }
.check { display: flex; gap: 10px; align-items: flex-start; }
.check input { width: 18px; height: 18px; margin: 2px 0 0; accent-color: var(--primary); flex: none; }
.check label { font-weight: 500; margin: 0; }

/* Drop zones */
.drop { display: flex; align-items: center; gap: 12px; border: 1.5px dashed var(--line-strong); border-radius: 10px;
  padding: 14px; background: var(--bg); cursor: pointer; min-height: 64px; }
.drop:hover, .drop.over { border-color: var(--primary); background: var(--primary-soft); }
.drop.big { padding: 22px 16px; flex-direction: column; text-align: center; gap: 6px; }
.drop svg { color: var(--primary); flex: none; }
.drop .what { font-weight: 600; }
.drop .sub { font-size: 13px; color: var(--muted); }
.drop.has { border-style: solid; border-color: var(--primary); }
.path-row { display: flex; gap: 6px; margin-top: 6px; }
.path-row input { flex: 1; min-height: 36px; font-size: 13px; }

/* Chips (provider, command) */
.chips { display: flex; flex-wrap: wrap; gap: 8px; }
.chip { min-height: 36px; padding: 6px 14px; border-radius: 99px; border: 1px solid var(--line-strong);
  background: transparent; color: var(--ink); font-size: 14px; }
.chip:hover { border-color: var(--primary); }
.chip[aria-pressed=true] { background: var(--primary); border-color: var(--primary); color: var(--primary-ink); font-weight: 650; }

/* Buttons */
.btn { min-height: 40px; padding: 8px 16px; border-radius: 8px; border: 1px solid var(--line-strong);
  background: transparent; color: var(--ink); font-weight: 600; }
.btn:hover:not(:disabled) { border-color: var(--muted); }
.btn:disabled { opacity: .45; cursor: default; }
.btn.go { width: 100%; min-height: 52px; font-size: 16px; border: 0; background: var(--action); color: var(--action-ink); }
.btn.go:hover:not(:disabled) { background: #fd9d57; }
.btn.small { min-height: 34px; padding: 5px 12px; font-size: 13px; }
.row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.row > input { flex: 1; min-width: 160px; }
.error { color: var(--bad); font-size: 14px; margin: 10px 0 0; }
.error:empty { display: none; }

/* Drawer: all settings */
details.drawer { margin-top: 18px; border-top: 1px solid var(--line); padding-top: 4px; }
details.drawer > summary, details.group > summary { list-style: none; cursor: pointer; display: flex; align-items: center;
  gap: 8px; min-height: 40px; font-weight: 600; font-size: 14px; color: var(--muted); }
details > summary::-webkit-details-marker { display: none; }
details > summary::before { content: ""; width: 7px; height: 7px; border-right: 2px solid currentColor;
  border-bottom: 2px solid currentColor; transform: rotate(-45deg); margin: 0 4px 0 2px; }
details[open] > summary::before { transform: rotate(45deg); }
details > summary:hover { color: var(--ink); }
details.group { border: 1px solid var(--line); border-radius: 10px; padding: 0 12px; margin: 8px 0; }
details.group[open] { padding-bottom: 6px; }
details.group > summary { text-transform: capitalize; color: var(--ink); }

/* Progress stages */
.stages { list-style: none; margin: 0; padding: 0; display: grid; gap: 2px; }
.stage { display: flex; align-items: center; gap: 12px; padding: 7px 4px; color: var(--faint); font-size: 14px; }
.stage .dot { width: 22px; height: 22px; border-radius: 50%; border: 2px solid var(--line-strong); flex: none;
  display: grid; place-items: center; }
.stage.done { color: var(--muted); }
.stage.done .dot { background: var(--primary); border-color: var(--primary); color: var(--primary-ink); }
.stage.now { color: var(--ink); font-weight: 600; }
.stage.now .dot { border-color: var(--primary); }
.stage.now .dot::after { content: ""; width: 8px; height: 8px; border-radius: 50%; background: var(--primary); }
@media (prefers-reduced-motion: no-preference) { .stage.now .dot::after { animation: pulse 1.2s ease-in-out infinite; } }
@keyframes pulse { 50% { opacity: .3; } }
.stage.fail .dot { border-color: var(--bad); background: var(--bad); }
.stage .detail { margin-left: auto; font-weight: 400; color: var(--muted); font-size: 13px; text-align: right;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 55%; }
.idle-note { color: var(--muted); font-size: 14px; margin: 0; }
.stage-body { display: grid; gap: 6px; flex: 1; min-width: 0; }
.bar { display: flex; align-items: center; gap: 10px; }
.bar .track { flex: 1; height: 6px; border-radius: 99px; background: var(--line); overflow: hidden; }
.bar .fill { display: block; height: 100%; border-radius: 99px; background: var(--primary); }
@media (prefers-reduced-motion: no-preference) { .bar .fill { transition: width .4s ease; } }
.bar .pct { font: 12px var(--mono); color: var(--primary); min-width: 3em; text-align: right; }
pre.log { margin: 8px 0 0; max-height: 300px; overflow: auto; background: var(--bg); border: 1px solid var(--line);
  border-radius: 8px; padding: 10px 12px; font: 12.5px/1.5 var(--mono); white-space: pre-wrap; word-break: break-word; color: #b8cbc8; }
.log .err { color: var(--bad); } .log .chk { color: var(--warn); }

/* Player */
audio { width: 100%; display: block; color-scheme: dark; }
.chapters { list-style: none; margin: 12px 0 0; padding: 0; max-height: 280px; overflow: auto; border-top: 1px solid var(--line); }
.chapters button { width: 100%; display: flex; gap: 12px; align-items: center; text-align: left; min-height: 38px;
  padding: 6px 8px; border: 0; border-bottom: 1px solid var(--line); background: transparent; color: var(--ink); }
.chapters button:hover { background: var(--primary-soft); }
.chapters .now { background: var(--primary-soft); color: var(--primary); font-weight: 600; }
.chapters .t { color: var(--muted); font: 13px var(--mono); min-width: 3.6em; }
.chapters .sub { padding-left: 26px; font-size: 14px; }

/* Tabs and documents */
.tabs { display: flex; gap: 4px; flex-wrap: wrap; border-bottom: 1px solid var(--line); margin: 18px 0 14px; }
.tabs button { min-height: 40px; padding: 8px 12px; border: 0; border-bottom: 2px solid transparent; background: none;
  color: var(--muted); font-weight: 600; margin-bottom: -1px; }
.tabs button:hover { color: var(--ink); }
.tabs button[aria-selected=true] { color: var(--ink); border-bottom-color: var(--primary); }
.doc { overflow-wrap: anywhere; font-size: 15px; line-height: 1.65; }
.vault-card { display: grid; gap: 6px; }
.vault-card .path { font: 13px var(--mono); color: var(--muted); overflow-wrap: anywhere; }
.btn.go-link { display: inline-flex; align-items: center; text-decoration: none; background: var(--primary);
  color: var(--primary-ink); border-color: var(--primary); }
.doc h1 { font-size: 22px; margin: 4px 0 12px; letter-spacing: -0.01em; }
.doc h2 { font-size: 17px; margin: 24px 0 8px; } .doc h3 { font-size: 15px; margin: 18px 0 6px; }
.doc p { margin: 8px 0; max-width: 75ch; }
.doc blockquote { margin: 10px 0; padding: 6px 14px; border-left: 3px solid var(--primary); background: var(--primary-soft); border-radius: 0 8px 8px 0; }
.doc code { font: 13px var(--mono); background: var(--bg); border: 1px solid var(--line); padding: 1px 5px; border-radius: 5px; }
.doc pre { background: var(--bg); border: 1px solid var(--line); padding: 12px; border-radius: 8px; overflow-x: auto; }
.doc pre code { border: 0; padding: 0; }
.doc .table { overflow-x: auto; }
.doc table { border-collapse: collapse; font-size: 14px; }
.doc th, .doc td { border-bottom: 1px solid var(--line); padding: 7px 10px; text-align: left; vertical-align: top; }
.doc th { color: var(--muted); font-weight: 600; }

/* Library */
.runs { list-style: none; margin: 0; padding: 0; }
.runs button { width: 100%; display: flex; justify-content: space-between; gap: 12px; align-items: center; text-align: left;
  min-height: 48px; padding: 8px 10px; border: 0; border-radius: 8px; background: transparent; color: var(--ink); }
.runs button:hover { background: var(--primary-soft); }
.runs .name { font-weight: 600; }
.runs .meta { color: var(--muted); font-size: 13px; white-space: nowrap; }
.tag { font-size: 12px; padding: 1px 8px; border-radius: 99px; background: var(--primary-soft); color: var(--primary); margin-right: 8px; }
.muted { color: var(--muted); font-size: 14px; }
.gap-below { margin-bottom: 14px; }
.mt { margin-top: 8px; }
.sr { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); }
</style>
</head>
<body>
<header class="top"><div class="top-in">
  <div class="brand">
    <svg width="26" height="26" viewBox="0 0 26 26" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round">
      <path d="M3 13v0M7.5 9v8M12 5v16M16.5 8v10M21 11v4"/></svg>
    lecture2audio
  </div>
  <span class="pill" id="status">idle</span>
</div></header>

<main>
  <div class="col">
    <section class="card" aria-label="New run">
      <div class="step">
        <div class="step-head"><span class="num">1</span><h2>Your lecture</h2></div>
        <div id="step-lecture"></div>
      </div>
      <div class="step">
        <div class="step-head"><span class="num">2</span><h2>The model</h2></div>
        <div id="step-model"></div>
      </div>
      <div class="step">
        <div class="step-head"><span class="num">3</span><h2>What to make</h2></div>
        <div class="chips" role="group" aria-label="What to make">
          <button type="button" class="chip" data-cmd="build" aria-pressed="true">Audio and text</button>
          <button type="button" class="chip" data-cmd="text" aria-pressed="false">Text only</button>
        </div>
        <p class="help gap-below" id="cmd-help"></p>
                <button type="button" class="btn go" id="run">Make it</button>
        <div class="row mt">
          <button type="button" class="btn small" id="cancel" disabled>Stop</button>
          <button type="button" class="btn small" id="reset">Reset settings</button>
        </div>
        <p class="error" id="form-error" role="alert"></p>
      </div>
      <details class="drawer">
        <summary>All settings</summary>
        <p class="help">Everything else the tool can do. The defaults are good for most lectures.</p>
        <div id="advanced"></div>
      </details>
    </section>
  </div>

  <div class="col">
    <section class="card" aria-label="Progress">
      <div class="card-head"><h2>Progress</h2></div>
      <p class="idle-note" id="idle-note">Nothing running. Drop a lecture on the left and press Make it.</p>
      <ol class="stages" id="stages" hidden></ol>
      <details class="drawer" id="log-box">
        <summary>Show the log</summary>
        <pre class="log" id="log" aria-live="polite"></pre>
      </details>
    </section>

    <section class="card" id="result" hidden aria-label="Result">
      <div class="card-head"><h2 id="result-title">Result</h2></div>
      <div id="player" hidden>
        <audio id="audio" controls preload="metadata"></audio>
        <ul class="chapters" id="chapters" aria-label="Chapters"></ul>
      </div>
      <div class="tabs" role="tablist" id="tabs"></div>
      <article class="doc" id="doc"></article>
    </section>

    <section class="card" aria-label="Library">
      <div class="card-head"><h2>Library</h2><span class="spacer"></span></div>
      <ul class="runs" id="runs"></ul>
      <details class="drawer">
        <summary>Another folder</summary>
        <div class="row"><input type="text" id="out-root" aria-label="Output folder">
          <button type="button" class="btn small" id="refresh">Open</button></div>
      </details>
    </section>
  </div>
</main>

<script nonce="__NONCE__">
"use strict";
const TOKEN = document.querySelector('meta[name="l2a-token"]').content;
const $ = (id) => document.getElementById(id);
const DOC_LABELS = {course: "Course", report: "Report", script: "Script", questions: "Questions",
                    glossary: "Glossary", index: "Index", reference: "Sources & formulas"};
// Shown in steps 1 and 2; everything else goes to "All settings".
const LECTURE = ["input", "slides", "glossary"];
const MODEL = ["llm", "rewrite_model", "base_url", "language"];
const KEY_FOR = {anthropic: "ANTHROPIC_API_KEY", openai: "OPENAI_API_KEY", deepseek: "DEEPSEEK_API_KEY",
                 kimi: "MOONSHOT_API_KEY", openrouter: "OPENROUTER_API_KEY", compat: "L2A_COMPAT_API_KEY"};
const NAMES = {anthropic: "Claude", openai: "OpenAI", deepseek: "DeepSeek", kimi: "Kimi",
               openrouter: "OpenRouter", compat: "Other API", local: "On this computer", dry: "Test (no model)"};
const NEEDS_MODEL = new Set(["kimi", "openrouter", "compat", "local"]);
const NEEDS_URL = new Set(["compat", "local"]);
const STAGES = [["transcribe", "Transcribe the recording"], ["outline", "Plan the lecture"],
  ["gaps", "Find what the lecture leaves out"], ["terms", "Define every term"],
  ["research", "Research the gaps online"], ["rewrite", "Write and check each part"],
  ["spoken", "Prepare the spoken text"], ["voice", "Record the voice"], ["done", "Done"]];
const TAG_STAGE = {transcribe: "transcribe", outline: "outline", gaps: "gaps", terms: "terms", research: "research",
  rewrite: "rewrite", verify: "rewrite", audit: "rewrite", check: "rewrite", spoken: "spoken", voice: "voice",
  roundtrip: "voice"};

let spec = null, command = "build", values = {}, outRoot = "", since = 0, polling = null, current = null;
let envKeys = {}, keyVals = {}, stageAt = -1, stageNote = "", stageSince = Date.now(), jobState = "idle";
let stagePct = null, audioSeconds = 0, vaultFromLog = "", jobName = "";

function el(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "text") e.textContent = v; else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined && v !== false) e.setAttribute(k, v === true ? "" : v);
  }
  for (const k of kids) if (k) e.append(k);
  return e;
}
function svg(path, size) {
  // Built from fixed markup (icon paths are constants of this page, never data).
  const t = document.createElement("template"), n = size || 22;
  t.innerHTML = `<svg width="${n}" height="${n}" viewBox="0 0 24 24" fill="none" stroke="currentColor"
    stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${path}"/></svg>`;
  return t.content.firstChild;
}
const ICON_UP = "M12 16V4m0 0l-5 5m5-5l5 5M4 16v3a1 1 0 001 1h14a1 1 0 001-1v-3";
const ICON_CHECK = "M5 12l5 5 9-10";
function store() { try { localStorage.setItem("l2a-form", JSON.stringify({command, values})); } catch (e) {} }
function restore() {
  try { const s = JSON.parse(localStorage.getItem("l2a-form") || "{}");
        if (s.command) command = s.command; if (s.values) values = s.values; } catch (e) {}
}
async function api(path, opts) {
  opts = opts || {};
  const headers = Object.assign({}, opts.headers || {});
  if (opts.method === "POST") headers["X-L2A-Token"] = TOKEN;
  const r = await fetch(path, Object.assign({}, opts, {headers}));
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || ("HTTP " + r.status));
  return data;
}
const post = (path, body) => api(path, {method: "POST", body: JSON.stringify(body),
                                        headers: {"Content-Type": "application/json"}});
function q(params) { return new URLSearchParams(params).toString(); }
function fields() { return Object.fromEntries(spec[command].groups.flatMap(g => g.fields).map(f => [f.dest, f])); }
function val(f) {
  if (Object.prototype.hasOwnProperty.call(values, f.dest)) return values[f.dest];
  return f.default === null || f.default === undefined ? "" : f.default;
}
function set(dest, v) { values[dest] = v; store(); }
function label(f) {
  const special = {input: "Transcript or recording", slides: "Slides (optional)", glossary: "Glossary (optional)",
    rewrite_model: "Model name", base_url: "API address", language: "Language"};
  return special[f.dest] || (f.flag || f.dest).replace(/^--/, "").replace(/-/g, " ").replace(/^./, c => c.toUpperCase());
}

// ------------------------------------------------------------------ fields
async function upload(file, f, zone) {
  zone.querySelector(".what").textContent = "Uploading " + file.name + "…";
  try {
    const r = await api("/api/upload?" + q({name: file.name}),
      {method: "POST", body: file, headers: {"Content-Type": "application/octet-stream"}});
    set(f.dest, r.path); renderForm();
  } catch (err) { $("form-error").textContent = err.message; renderForm(); }
}
function dropField(f, big) {
  const value = val(f);
  const name = value ? String(value).split(/[\\/]/).pop() : "";
  const file = el("input", {type: "file", class: "sr", tabindex: "-1", "aria-hidden": "true",
    onchange: (e) => { if (e.target.files[0]) upload(e.target.files[0], f, zone); }});
  const zone = el("div", {class: "drop" + (big ? " big" : "") + (value ? " has" : ""), role: "button", tabindex: "0",
      "aria-label": label(f) + ": choose a file",
      onclick: () => file.click(),
      onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); file.click(); } },
      ondragover: (e) => { e.preventDefault(); zone.classList.add("over"); },
      ondragleave: () => zone.classList.remove("over"),
      ondrop: (e) => { e.preventDefault(); zone.classList.remove("over");
                       if (e.dataTransfer.files[0]) upload(e.dataTransfer.files[0], f, zone); }},
    svg(value ? ICON_CHECK : ICON_UP, big ? 28 : 22),
    el("div", {}, el("div", {class: "what", text: value ? name : (big ? "Drop your transcript or recording here" : "Drop a file or click")}),
      el("div", {class: "sub", text: value ? "Click to choose another file" :
        (big ? ".txt .md .srt .vtt, or .mp3 .m4a .wav … — or click to choose"
             : f.dest === "slides" ? "PDF or text" : "Text file, one term per line, \"term: definition\" allowed")})));
  const id = "f-" + f.dest;
  const path = el("input", {type: "text", id, spellcheck: "false", placeholder: "or type a path on this computer",
    oninput: (e) => set(f.dest, e.target.value), onchange: () => renderForm()});
  path.value = value;
  return el("div", {class: "field"}, big ? null : el("span", {class: "label", text: label(f)}), zone, file,
            el("div", {class: "path-row"}, el("label", {for: id, class: "sr", text: label(f) + " path"}), path));
}
function plainField(f) {
  const id = "f-" + f.dest, v = val(f);
  const help = f.help ? el("div", {class: "help", text: f.help}) : null;
  if (f.kind === "flag") {
    const box = el("input", {type: "checkbox", id, onchange: (e) => set(f.dest, e.target.checked)});
    box.checked = !!v;
    return el("div", {class: "field"}, el("div", {class: "check"}, box,
      el("div", {}, el("label", {for: id, text: label(f)}), help)));
  }
  let input;
  if (f.kind === "choice") {
    input = el("select", {id, onchange: (e) => set(f.dest, e.target.value)});
    if (f.default === null || f.default === undefined) input.append(el("option", {value: "", text: "(default)"}));
    for (const c of f.choices) input.append(el("option", {value: c, text: c}));
    input.value = v;
  } else {
    input = el("input", {type: f.kind === "int" || f.kind === "float" ? "number" : "text", id, spellcheck: "false",
      step: f.kind === "float" ? "any" : null, placeholder: f.dest === "out" ? outRoot : null,
      oninput: (e) => set(f.dest, e.target.value)});
    input.value = v;
  }
  return el("div", {class: "field"}, el("label", {for: id, text: label(f)}), input, help);
}
function keyField(name, help) {
  const id = "k-" + name;
  const input = el("input", {type: "password", id, autocomplete: "off", spellcheck: "false",
    placeholder: envKeys[name] ? "Already set on this computer" : "Paste your key",
    oninput: (e) => { keyVals[name] = e.target.value; }});
  input.value = keyVals[name] || "";
  return el("div", {class: "field"}, el("label", {for: id, text: help || name}), input,
    el("div", {class: "help", text: "Used for this run only. Never saved, never shown again."}));
}

// ------------------------------------------------------------------ form
function renderForm() {
  const all = fields();
  $("cmd-help").textContent = command === "build"
    ? "The script, the reports and the audio file with chapters."
    : "The script and the reports only. Faster and cheaper; no voice.";
  document.querySelectorAll("[data-cmd]").forEach(b => b.setAttribute("aria-pressed", b.dataset.cmd === command));

  const lecture = $("step-lecture"); lecture.replaceChildren();
  if (all.input) lecture.append(dropField(all.input, true));
  for (const d of ["slides", "glossary"]) if (all[d]) lecture.append(dropField(all[d], false));

  const model = $("step-model"); model.replaceChildren();
  const llm = all.llm, provider = val(llm) || "deepseek";
  model.append(el("div", {class: "chips", role: "group", "aria-label": "Model provider"},
    ...llm.choices.map(c => el("button", {type: "button", class: "chip", "aria-pressed": String(c === provider),
      text: NAMES[c] || c, onclick: () => { set("llm", c); renderForm(); }}))));
  if (KEY_FOR[provider]) model.append(keyField(KEY_FOR[provider], (NAMES[provider] || provider) + " API key"));
  if (NEEDS_URL.has(provider) && all.base_url) model.append(plainField(all.base_url));
  if (NEEDS_MODEL.has(provider) && all.rewrite_model) model.append(plainField(all.rewrite_model));
  if (all.language) model.append(plainField(all.language));

  const adv = $("advanced"); adv.replaceChildren();
  const shown = new Set([...LECTURE, ...MODEL]);
  for (const g of spec[command].groups) {
    const rest = g.fields.filter(f => !shown.has(f.dest));
    if (rest.length) adv.append(el("details", {class: "group"}, el("summary", {text: g.title}), ...rest.map(plainField)));
  }
  const others = Object.keys(envKeys).filter(k => k !== KEY_FOR[provider]);
  adv.append(el("details", {class: "group"}, el("summary", {text: "Other API keys"}),
    el("p", {class: "help", text: "Search engines for the research (Tavily, Brave) and the keys of other providers."}),
    ...others.map(k => keyField(k))));
}

// ------------------------------------------------------------------ progress
function renderStages(state) {
  const list = $("stages");
  const steps = STAGES.filter(([k]) => (k !== "voice" || command === "build") && (k !== "transcribe" || stageAt === 0));
  $("idle-note").hidden = stageAt >= 0 || state === "running";
  list.hidden = !$("idle-note").hidden;
  list.replaceChildren(...steps.map(([key, text]) => {
    const i = STAGES.findIndex(s => s[0] === key);
    let cls = "stage";
    if (state === "done" || i < stageAt) cls += " done";
    else if (i === stageAt) cls += (state === "failed" || state === "cancelled") ? " fail" : " now";
    return el("li", {class: cls}, el("span", {class: "dot"}, cls.includes("done") ? svg(ICON_CHECK, 14) : null),
      el("div", {class: "stage-body"}, el("span", {text}),
        i === stageAt && state === "running" && stagePct !== null ? progressBar() : null),
      i === stageAt && state === "running"
        ? el("span", {class: "detail", text: [stageNote, fmt((Date.now() - stageSince) / 1000) + timeLeft()].filter(Boolean).join(" · ")})
        : null);
  }));
}
function timeLeft() {
  if (stagePct === null || stagePct < 0.03) return "";
  const elapsed = (Date.now() - stageSince) / 1000;
  return " · about " + fmt(elapsed * (1 - stagePct) / stagePct) + " left";
}
function progressBar() {
  const pct = Math.round(stagePct * 100);
  const fill = el("span", {class: "fill"});
  fill.style.width = (stagePct * 100).toFixed(1) + "%";  // CSSOM, allowed by the page's CSP
  const track = el("span", {class: "track"}, fill);
  return el("div", {class: "bar", role: "progressbar", "aria-valuenow": pct, "aria-valuemin": 0,
                    "aria-valuemax": 100, "aria-label": "Progress of this stage"},
            track, el("span", {class: "pct", text: pct + "%"}));
}
function addLog(lines) {
  const log = $("log");
  const atEnd = log.scrollTop + log.clientHeight >= log.scrollHeight - 8;
  for (const line of lines) {
    const tag = (line.match(/^\[(\w+)\]/) || [])[1];
    const vault = line.match(/^\[course\] .*note\(s\): (.+)$/) || line.match(/^Done\. Vault: (.+)$/);
    if (vault) vaultFromLog = vault[1].trim();
    const minutes = line.match(/^\[transcribe\] .*?, (\d+) min of audio/);
    if (minutes) audioSeconds = +minutes[1] * 60;
    const stamp = line.match(/^\[(?:(\d+):)?(\d+):(\d+(?:\.\d+)?) -->/);
    if (stamp && audioSeconds) stagePct = Math.min(1, ((+stamp[1] || 0) * 3600 + +stamp[2] * 60 + +stamp[3]) / audioSeconds);
    const count = line.match(/^\[(?:rewrite|voice|spoken)\] (?:part |chunk )?(\d+)\/(\d+)/);
    if (tag && TAG_STAGE[tag]) {
      const i = STAGES.findIndex(s => s[0] === TAG_STAGE[tag]);
      if (i > stageAt) { stageSince = Date.now(); stagePct = null; }
      if (i >= stageAt) { stageAt = i; stageNote = line.replace(/^\[\w+\]\s*/, "").split(", ")[0].slice(0, 60); }
      if (count && i === stageAt) stagePct = (+count[1] - (TAG_STAGE[tag] === "voice" ? 1 : 0.5)) / +count[2];
    } else if (/^Done\./.test(line)) { stageAt = STAGES.length - 1; stageNote = ""; }
    const cls = /^(error|Traceback|usage:)/.test(line) || /Error/.test(line) ? "err" : line.startsWith("[check]") ? "chk" : null;
    log.append(el("span", {class: cls, text: line + "\n"}));
  }
  if (atEnd) log.scrollTop = log.scrollHeight;
}
function setStatus(state) {
  jobState = state;
  const s = $("status"); s.textContent = state === "running" ? "working" : state; s.className = "pill " + state;
  const running = state === "running";
  $("run").disabled = running; $("cancel").disabled = !running;
  $("run").textContent = running ? "Working…" : "Make it";
  if (state === "failed") $("log-box").open = true;
  renderStages(state);
}
async function poll() {
  try {
    const j = await api("/api/job?" + q({since}));
    if (j.name) jobName = j.name;
    addLog(j.lines || []); since = j.next || since; setStatus(j.state);
    if (j.state === "running") { polling = setTimeout(poll, 600); return; }
    polling = null;
    if (j.name && j.out) { await loadRuns(j.out); if (j.state === "done" || j.state === "failed") openRun(j.out, j.name); }
  } catch (e) { polling = setTimeout(poll, 1500); }
}
async function run() {
  $("form-error").textContent = "";
  if (!val(fields().input)) { $("form-error").textContent = "Drop a transcript or recording first (step 1)."; return; }
  const allowed = new Set(Object.keys(fields()));
  const send = {};
  for (const [k, v] of Object.entries(values)) if (allowed.has(k)) send[k] = v;
  try {
    await post("/api/run", {command, values: send, keys: keyVals});
    $("log").replaceChildren(); since = 0; stageAt = -1; stageNote = ""; setStatus("running");
    if (!polling) poll();
  } catch (e) { $("form-error").textContent = e.message; }
}

// ------------------------------------------------------------------ results and library
function fmt(t) { t = Math.floor(t); const h = Math.floor(t / 3600), m = Math.floor(t / 60) % 60, s = t % 60;
  return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(s).padStart(2, "0"); }
async function loadRuns(out) {
  try {
    const r = out && out !== outRoot ? await post("/api/open-out", {out}) : await api("/api/runs?" + q({out: outRoot}));
    outRoot = r.out; $("out-root").value = r.out;
    const ul = $("runs"); ul.replaceChildren();
    if (!r.runs.length) ul.append(el("li", {class: "muted", text: "Nothing here yet. Your lectures will appear here."}));
    for (const item of r.runs) {
      const when = new Date(item.mtime * 1000).toLocaleString([], {dateStyle: "medium", timeStyle: "short"});
      ul.append(el("li", {}, el("button", {type: "button", onclick: () => openRun(r.out, item.name)},
        el("span", {class: "name", text: item.name}),
        el("span", {class: "meta"}, item.audio ? el("span", {class: "tag", text: "audio"}) : null, when))));
    }
  } catch (e) { $("runs").replaceChildren(el("li", {class: "error", text: e.message})); }
}
async function openRun(out, name) {
  current = {out, name};
  $("result").hidden = false; $("result-title").textContent = name;
  const tabs = $("tabs"); tabs.replaceChildren();
  for (const doc of Object.keys(DOC_LABELS))
    tabs.append(el("button", {type: "button", role: "tab", "data-doc": doc, "aria-selected": "false",
                              text: DOC_LABELS[doc], onclick: () => showDoc(doc)}));
  showDoc("script");
  const audio = $("audio"), list = $("chapters");
  list.replaceChildren(); $("player").hidden = true; audio.removeAttribute("src");
  try {
    const c = await api("/api/chapters?" + q({out, run: name}));
    if (c.audio) {
      $("player").hidden = false;
      audio.src = "/files/" + encodeURIComponent(name) + "/" + encodeURIComponent(c.audio) + "?" + q({out});
      c.chapters.forEach((ch, i) => {
        const depth = (ch.title.match(/^\d+(\.\d+)*/) || [""])[0].split(".").length - 1;
        list.append(el("li", {}, el("button", {type: "button", class: depth ? "sub" : null, "data-i": i,
          onclick: () => { audio.currentTime = ch.start + 0.01; audio.play().catch(() => {}); }},
          el("span", {class: "t", text: fmt(ch.start)}), el("span", {text: ch.title}))));
      });
      audio.ontimeupdate = () => {
        const t = audio.currentTime;
        list.querySelectorAll("button").forEach((b, i) => {
          const ch = c.chapters[i]; b.classList.toggle("now", t >= ch.start && t < ch.end);
        });
      };
    }
  } catch (e) {}
  $("result").scrollIntoView({behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start"});
}
async function showDoc(doc) {
  if (!current) return;
  document.querySelectorAll("#tabs button").forEach(b => b.setAttribute("aria-selected", b.dataset.doc === doc));
  const box = $("doc");
  if (doc === "course") return showVault(box);
  try {
    const d = await api("/api/doc?" + q({out: current.out, run: current.name, doc}));
    if (!d.exists) { box.replaceChildren(el("p", {class: "muted", text: "This file does not exist for this run."})); return; }
    box.innerHTML = d.html;  // rendered and escaped by the server
  } catch (e) { box.replaceChildren(el("p", {class: "error", text: e.message})); }
}

async function showVault(box) {
  // The vault path the run printed (it differs from <run>/vault when --vault-dir was used).
  const fromLog = current && jobName === current.name ? vaultFromLog : "";
  const params = fromLog ? {path: fromLog} : {out: current.out, run: current.name};
  const v = await api("/api/vault?" + q(params)).catch(() => ({exists: false}));
  if (!v.exists) {
    box.replaceChildren(el("p", {class: "muted", text: "No course notes for this run. Turn on Course in All settings (--course)."}));
    return;
  }
  const obsidian = "obsidian://open?path=" + encodeURIComponent(v.index || v.path);
  box.replaceChildren(el("div", {class: "vault-card"},
    el("p", {class: "label", text: v.notes + " notes, ready for Obsidian"}),
    el("code", {class: "path", text: v.path}),
    el("div", {class: "row mt"},
      el("a", {class: "btn go-link", href: obsidian, text: "Open in Obsidian"}),
      el("button", {type: "button", class: "btn", text: "Show in Finder",
        onclick: () => post("/api/reveal", {path: v.index || v.path}).catch(e => $("form-error").textContent = e.message)})),
    el("p", {class: "help", text: "The notes are rewritten on every run: link your own notes to them instead of editing them."})));
}

async function init() {
  restore();
  const o = await api("/api/options");
  spec = o.spec; outRoot = o.out; envKeys = o.env_keys;
  if (!spec[command]) command = "build";
  renderForm();
  document.querySelectorAll("[data-cmd]").forEach(b => b.addEventListener("click", () => {
    command = b.dataset.cmd; store(); renderForm(); renderStages($("status").className.split(" ")[1] || "idle"); }));
  $("run").addEventListener("click", run);
  $("cancel").addEventListener("click", () => post("/api/cancel", {}).catch(e => $("form-error").textContent = e.message));
  $("reset").addEventListener("click", () => { values = {}; store(); renderForm(); });
  $("refresh").addEventListener("click", () => loadRuns($("out-root").value.trim()));
  await loadRuns();
  poll();
  setInterval(() => { if (jobState === "running") renderStages(jobState); }, 1000);
}
init().catch(e => { $("form-error").textContent = "Could not load the options: " + e.message; });
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
