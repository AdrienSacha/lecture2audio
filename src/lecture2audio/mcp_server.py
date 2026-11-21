"""`lecture2audio mcp`: the program as a Model Context Protocol server, so that any MCP host
(Claude Code, Claude Desktop, Cursor, Codex, Gemini CLI, Goose) can run a lecture and read its
results.

Like the app, the server never runs the pipeline itself: `build` starts the command line
(`python -m lecture2audio build|text <input> ...`) as a subprocess, so an agent can never do
more than the command line, and every check of the harness runs unchanged. The other tools
only read files under the output folder.

- Transport: stdio (the host starts `lecture2audio mcp` and talks over its stdin and stdout).
  The run's own output goes to `out/<name>/mcp-run.log`, never to the server's stdout, which
  carries the protocol.
- `build` returns at once with a run handle (`wait=False`, the default): a run takes tens of
  minutes, longer than hosts let a tool call block. The agent then polls `status`. The run is
  started in its own session, so it survives the host or the server stopping; its state is
  read from `events.jsonl` and `calls.jsonl` on disk. The MCP tasks extension
  (`io.modelcontextprotocol/tasks`) is the standard form of this handle, but the official
  Python SDK does not implement it yet (mcp 2.3.0); see ADR-0008. `wait=True` blocks until the
  run ends and reports progress per stage.
- Safety: a run name and a file name are checked against fixed patterns and lists, and the
  resolved path must lie under the output folder, so a request cannot read anything else.
  API keys are never accepted as arguments nor returned: the subprocess inherits the server's
  environment, and the command line loads the key file itself, as it does in a shell. Any key
  value that would appear in a returned text is replaced by "[key]".

The functions below take the output folder as an argument and do not need the `mcp` package,
so they are tested directly; `make_server` imports the SDK only when the server starts.
"""
import argparse
import asyncio
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import stages
from .sources import slugify

# Files an agent may read, by name, from out/<run>/ (tool read_result and the resources).
READABLE = ("script.md", "report.md", "glossary.md", "questions.md", "ideas.csv",
            "termbase.json")
MIME = {".md": "text/markdown", ".csv": "text/csv", ".json": "application/json"}
RUN_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
LLMS = ("deepseek", "anthropic", "openai", "kimi", "openrouter", "compat", "local", "dry")
TTS = ("auto", "ming", "qwen3", "openai", "say", "fake")
LANGUAGES = ("en", "fr", "auto")
REDO = tuple(sorted(stages.redo_table()))
PID_FILE = "mcp-run.pid"
LOG_FILE = "mcp-run.log"
# Variables whose values are API keys (keys.KEY_ENV, llm.PRESETS, web.py). Kept here as well,
# so a returned text is cleaned even before the key module exists in this checkout.
KEY_VARS = {"DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "MOONSHOT_API_KEY",
            "OPENROUTER_API_KEY", "L2A_COMPAT_API_KEY", "L2A_LOCAL_API_KEY", "TAVILY_API_KEY",
            "BRAVE_API_KEY"}
try:  # the key module, when this checkout has it
    from .keys import KNOWN as _KNOWN  # type: ignore[import-not-found]
    KEY_VARS |= set(_KNOWN)
except ImportError:  # pragma: no cover - depends on the checkout
    pass

_procs: dict[str, subprocess.Popen] = {}  # runs started by this server, by name


class ToolError(ValueError):
    """A request the server refuses; the message is safe to show to the agent."""


# --- paths --------------------------------------------------------------------------------

def run_dir(out: Path, run: str) -> Path:
    """The folder of a run, refused unless it is a direct child of the output folder."""
    if not isinstance(run, str) or not RUN_NAME.match(run) or ".." in run:
        raise ToolError(f"invalid run name {run!r}: letters, digits, '.', '_' and '-' only")
    root = Path(out).resolve()
    work = (root / run).resolve()
    if work.parent != root:
        raise ToolError(f"run {run!r} is not under the output folder")
    if not work.is_dir():
        raise ToolError(f"unknown run {run!r}; call list_runs to see the runs in {root}")
    return work


def result_path(out: Path, run: str, file: str) -> Path:
    """A readable result of a run; anything outside READABLE or the run folder is refused."""
    if file not in READABLE:
        raise ToolError(f"file {file!r} cannot be read; one of: {', '.join(READABLE)}")
    work = run_dir(out, run)
    path = (work / file).resolve()
    if path.parent != work:  # a symbolic link pointing elsewhere
        raise ToolError(f"{file} of run {run!r} points outside the run folder")
    if not path.is_file():
        raise ToolError(f"run {run!r} has no {file} yet; call status to see its stages")
    return path


def redact(text: str) -> str:
    """The text with the value of every API key variable of this environment replaced."""
    for var in KEY_VARS:
        key = os.environ.get(var)
        if key and len(key) >= 8:
            text = text.replace(key, "[key]")
    return text


# --- reading runs -------------------------------------------------------------------------

def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def running(work: Path) -> bool:
    """Whether a run started by an MCP server is still going in this folder."""
    proc = _procs.get(work.name)
    if proc is not None:
        return proc.poll() is None
    try:
        pid = int((work / PID_FILE).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    return _alive(pid)


def read_events(work: Path) -> dict:
    """The stages of a run from `events.jsonl`: state and seconds per stage, in order seen."""
    seen: dict[str, dict] = {}
    path = work / "events.jsonl"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict) or event.get("stage") not in stages.BY_NAME:
                continue
            entry = seen.setdefault(event["stage"], {"stage": event["stage"]})
            entry["state"] = event.get("state", "running")
            if "seconds" in event:
                entry["seconds"] = event["seconds"]
    current = next((e["stage"] for e in reversed(seen.values()) if e["state"] == "running"), None)
    return {"stages": list(seen.values()), "current": current}


def _tokens(record: dict) -> dict[str, int]:
    usage = (record.get("answer") or {}).get("usage") if isinstance(record.get("answer"), dict) \
        else None
    if not isinstance(usage, dict):
        return {}
    pairs = {"input": ("prompt_tokens", "input_tokens"),
             "output": ("completion_tokens", "output_tokens")}
    return {name: next((int(usage[k]) for k in keys if isinstance(usage.get(k), int)), 0)
            for name, keys in pairs.items()}


def read_calls(work: Path) -> dict:
    """Counts from `calls.jsonl` (one line per network request): by kind, errors, seconds and
    tokens. The messages themselves are not returned."""
    path = work / "calls.jsonl"
    counts = {"requests": 0, "errors": 0, "seconds": 0.0, "by_kind": {},
              "input_tokens": 0, "output_tokens": 0}
    if not path.exists():
        return counts
    with path.open(encoding="utf-8") as lines:
        for line in lines:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            kind = str(record.get("kind", "other"))
            counts["requests"] += 1
            counts["by_kind"][kind] = counts["by_kind"].get(kind, 0) + 1
            counts["errors"] += bool(record.get("error"))
            if isinstance(record.get("seconds"), (int, float)):
                counts["seconds"] += record["seconds"]
            used = _tokens(record)
            counts["input_tokens"] += used.get("input", 0)
            counts["output_tokens"] += used.get("output", 0)
    counts["seconds"] = round(counts["seconds"], 1)
    return counts


def _state(work: Path, events: dict) -> str:
    if running(work):
        return "running"
    if events["current"]:
        return "stopped"  # a stage was entered and never closed: the run ended early
    return "finished" if (work / "report.md").exists() else "incomplete"


def status(out: Path, run: str) -> dict:
    """What a run has done: its state, the stages from `events.jsonl`, the cached stages on
    disk, the request counts from `calls.jsonl`, the results it can offer, and the last lines
    of its log when the server started it."""
    work = run_dir(out, run)
    events = read_events(work)
    result = {"run": run, "folder": str(work), "state": _state(work, events),
              "current_stage": events["current"], "stages": events["stages"],
              "cached_stages": [s.name for s in stages.STAGES if s.name in stages.present(work)],
              "calls": read_calls(work),
              "results": [f for f in READABLE if (work / f).is_file()],
              "audio": sorted(p.name for p in work.glob("*.m4a")) + sorted(
                  p.name for p in work.glob("*.mp3"))}
    log = work / LOG_FILE
    if log.exists():
        tail = log.read_text(encoding="utf-8", errors="replace").splitlines()[-15:]
        result["log_tail"] = [redact(line) for line in tail]
    proc = _procs.get(run)
    if proc is not None and proc.poll() is not None:
        result["exit_code"] = proc.returncode
    return result


def list_runs(out: Path) -> list[dict]:
    """Every run under the output folder, newest first."""
    root = Path(out)
    if not root.is_dir():
        return []
    runs = []
    for work in root.iterdir():
        if not work.is_dir() or not RUN_NAME.match(work.name) or work.name.startswith(("_", ".")):
            continue
        marks = [work / "events.jsonl", work / "report.md", work / "outline.json"]
        if not any(m.exists() for m in marks):
            continue
        events = read_events(work)
        changed = max(m.stat().st_mtime for m in marks if m.exists())
        runs.append({"run": work.name, "state": _state(work, events),
                     "current_stage": events["current"],
                     "results": [f for f in READABLE if (work / f).is_file()],
                     "updated": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(changed)),
                     "_t": changed})
    runs.sort(key=lambda r: r.pop("_t"), reverse=True)
    return runs


def read_result(out: Path, run: str, file: str, max_chars: int = 200_000) -> str:
    """One result file of a run, as text (cut at max_chars, with a note)."""
    text = redact(result_path(out, run, file).read_text(encoding="utf-8", errors="replace"))
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n[cut at {max_chars} of {len(text)} characters]"
    return text


# --- starting a run -----------------------------------------------------------------------

def run_name(source: Path, name: str | None) -> str:
    """The run's folder name, as the command line computes it (cli.run, _resolve_folder)."""
    if name:
        slug = slugify(name)
    else:
        slug = slugify(source.resolve().name if source.is_dir() else source.stem)
    if not RUN_NAME.match(slug):
        raise ToolError(f"cannot make a run name from {name or source.name!r}")
    return slug


def build_command(out: Path, source: Path, name: str, *, llm: str = "deepseek",
                  language: str = "en", audio: bool = True, research: bool = True,
                  tts: str | None = None, redo: str | None = None) -> list[str]:
    """The command line of a run; every value is checked against the options it may take."""
    for value, allowed, what in ((llm, LLMS, "llm"), (language, LANGUAGES, "language")):
        if value not in allowed:
            raise ToolError(f"{what} must be one of {', '.join(allowed)}, not {value!r}")
    if tts is not None and tts not in TTS:
        raise ToolError(f"tts must be one of {', '.join(TTS)}, not {tts!r}")
    if redo is not None and redo not in REDO:
        raise ToolError(f"redo must be one of {', '.join(REDO)}, not {redo!r}")
    command = [sys.executable, "-m", "lecture2audio", "build" if audio else "text",
               str(source), "--name", name, "--out", str(out), "--llm", llm,
               "--language", language, "--no-open-obsidian"]
    if not research:
        command += ["--research", "off"]
    if tts and audio:
        command += ["--tts", tts]
    if redo:
        command += ["--redo", redo]
    return command


def start_build(out: Path, path: str, *, name: str | None = None, llm: str = "deepseek",
                language: str = "en", audio: bool = True, research: bool = True,
                tts: str | None = None, redo: str | None = None) -> dict:
    """Starts a run as a subprocess and returns its handle (the run name) at once."""
    source = Path(path).expanduser()
    if not source.exists():
        raise ToolError(f"input not found: {source}")
    source = source.resolve()
    out = Path(out).resolve()
    run = run_name(source, name)
    command = build_command(out, source, run, llm=llm, language=language, audio=audio,
                            research=research, tts=tts, redo=redo)
    work = out / run
    if work.is_dir() and running(work):
        raise ToolError(f"run {run!r} is already going; poll status, do not start it twice")
    work.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k != "L2A_EVENTS"}
    with (work / LOG_FILE).open("w", encoding="utf-8") as log:
        proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log,
                                stderr=subprocess.STDOUT, env=env, cwd=str(out.parent),
                                start_new_session=True)
    _procs[run] = proc
    (work / PID_FILE).write_text(str(proc.pid), encoding="utf-8")
    shown = command[3:]  # without the interpreter: no environment, no key, ever
    return {"run": run, "state": "running", "folder": str(work), "pid": proc.pid,
            "command": "lecture2audio " + " ".join(shown),
            "next": f"call status with run={run!r} until state is finished, then read_result "
                    "report.md first"}


def result_paths(out: Path, run: str) -> dict:
    """The files a finished run offers, as absolute paths."""
    work = run_dir(out, run)
    names = [*READABLE, "index.md", "recitation.pdf", "project.pdf", "anki.csv"]
    found = {n: str(work / n) for n in names if (work / n).is_file()}
    found.update({p.name: str(p) for p in sorted(work.glob("*.m4a"))})
    if (work / "vault").is_dir():
        found["vault"] = str(work / "vault")
    return found


async def wait_for(out: Path, run: str, progress=None, poll: float = 2.0) -> dict:
    """Waits until a run started by this server ends; `progress(done, total, message)` is
    awaited at each change of stage."""
    work = run_dir(out, run)
    total, last = len(stages.STAGES), None
    while running(work):
        events = read_events(work)
        done = sum(e["state"] == "done" for e in events["stages"])
        if progress is not None and (done, events["current"]) != last:
            last = (done, events["current"])
            await progress(done, total, f"stage {events['current'] or '...'}")
        await asyncio.sleep(poll)
    result = status(out, run)
    result["paths"] = result_paths(out, run)
    return result


def stop(out: Path, run: str) -> dict:
    """Stops a run that an MCP server started (its whole process group)."""
    work = run_dir(out, run)
    if not running(work):
        raise ToolError(f"run {run!r} is not running")
    proc = _procs.get(run)
    pid = proc.pid if proc else int((work / PID_FILE).read_text(encoding="utf-8").strip())
    os.killpg(os.getpgid(pid), signal.SIGINT)  # the command line keeps its caches on Ctrl-C
    return {"run": run, "state": "stopping",
            "next": "start build again with the same input to continue where it stopped"}


# --- the server ---------------------------------------------------------------------------

INSTRUCTIONS = """\
lecture2audio turns a lecture (recording or transcript, optional slides and glossary, in one
folder) into a verified spoken script, audio with chapters, course notes and a quality report.
It is a fixed chain of model calls with code checks, not a summarizer. Start a run with build
(it returns at once), poll status until state is finished, then read report.md first: it lists
open problems and every idea that did not reach the notes. A run takes tens of minutes and
spends API credit; use llm="dry" to check the setup for free. Never pass API keys: they come
from the environment or ~/.lecture2audio.env of the user who started this server."""


def make_server(out: Path):
    """The MCP server over the output folder `out` (needs the `mcp` package, extra [mcp])."""
    # Annotations are evaluated here (no `from __future__ import annotations` in this module),
    # because the SDK reads the tool signatures, Context included, to build their schemas.
    from mcp.server.mcpserver import Context, MCPServer
    from mcp.server.mcpserver.exceptions import ResourceError
    from mcp.server.mcpserver.exceptions import ToolError as SdkToolError
    from mcp.types import ToolAnnotations

    out = Path(out).resolve()
    server = MCPServer("lecture2audio", instructions=INSTRUCTIONS)

    def guard(fn, *args, error=SdkToolError, **kwargs):
        """A refusal reaches the agent as its message (an is_error result), not as a crash."""
        try:
            return fn(*args, **kwargs)
        except ToolError as exc:
            raise error(str(exc)) from None

    @server.tool(annotations=ToolAnnotations(destructiveHint=False, openWorldHint=True))
    async def build(path: str, llm: str = "deepseek", language: str = "en", audio: bool = True,
                    research: bool = True, name: str | None = None, redo: str | None = None,
                    tts: str | None = None, wait: bool = False,
                    ctx: Context | None = None) -> dict:
        """Run lecture2audio on a lecture folder or file (transcript, recording or slides).

        Returns at once with a run handle; poll `status` with the returned run name. A run takes
        tens of minutes and calls paid model APIs unless llm="dry". Running it again on the
        same input continues from the cached stages.

        Args:
            path: a folder holding the recording or transcript, slides and glossary, or one file.
            llm: deepseek (default), anthropic, openai, kimi, openrouter, compat, local, or dry
                (no model, no cost: checks the setup).
            language: en, fr or auto.
            audio: false stops before the voice (script, notes and report only).
            research: false skips web research for the gaps the lecture leaves.
            name: the run name (default: the folder or file name).
            redo: discard one stage and everything after it (outline, gaps, research, ideas,
                rewrite, spoken, audio, course, ...).
            tts: voice engine; "fake" makes silent audio for tests.
            wait: true blocks until the run ends and returns the paths of its results.
        """
        handle = guard(start_build, out, path, name=name, llm=llm, language=language,
                       audio=audio, research=research, tts=tts, redo=redo)
        if not wait:
            return handle

        async def progress(done, total, message):
            if ctx is not None:
                await ctx.report_progress(done, total, message)
        return await wait_for(out, handle["run"], progress)

    @server.tool(name="status", annotations=ToolAnnotations(readOnlyHint=True))
    def status_tool(run: str) -> dict:
        """The state of a run (running, finished, stopped, incomplete), its stages from
        events.jsonl with seconds, the cached stages, request counts from calls.jsonl, the
        result files present and the last log lines."""
        return guard(status, out, run)

    @server.tool(name="list_runs", annotations=ToolAnnotations(readOnlyHint=True))
    def list_runs_tool() -> list[dict]:
        """Every run in the output folder, newest first, with its state and result files."""
        return list_runs(out)

    @server.tool(name="read_result", annotations=ToolAnnotations(readOnlyHint=True))
    def read_result_tool(run: str, file: str) -> str:
        """One result of a run: script.md (the verified script), report.md (problems, repairs,
        ideas that did not arrive: read it first), glossary.md, questions.md, ideas.csv (the
        idea ledger) or termbase.json."""
        return guard(read_result, out, run, file)

    @server.tool(annotations=ToolAnnotations(destructiveHint=True))
    def stop_run(run: str) -> dict:
        """Stop a run started through this server; its finished stages stay cached."""
        return guard(stop, out, run)

    @server.resource("lecture2audio://runs", name="runs", mime_type="application/json",
                     description="The runs in the output folder")
    def runs_resource() -> str:
        return json.dumps(list_runs(out), ensure_ascii=False, indent=2)

    @server.resource("lecture2audio://runs/{run}/{file}", name="result",
                     description="A result of a run: " + ", ".join(READABLE))
    def result_resource(run: str, file: str) -> str:
        return guard(read_result, out, run, file, error=ResourceError)

    return server


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="lecture2audio mcp",
        description="Serve lecture2audio to MCP hosts over stdio (needs the extra [mcp]).")
    parser.add_argument("--out", default=os.environ.get("L2A_OUT_DIR", "out"),
                        help="output folder the runs are written to and read from "
                             "(default: $L2A_OUT_DIR or ./out)")
    args = parser.parse_args(argv)
    try:
        server = make_server(Path(args.out))
    except ImportError:
        print("The MCP server needs the official MCP SDK: uv pip install 'lecture2audio[mcp]'",
              file=sys.stderr)
        return 1
    server.run("stdio")
    return 0
