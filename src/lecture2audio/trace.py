"""`calls.jsonl`: one line per event of every request the program sends, in full, with the
context that explains it.

Each request has an `id` and is written twice: a `start` line when it is sent (what was sent:
host, model, every message) and an `end` line when it finishes (the whole answer as the
provider returned it, reasoning included, the token counts, the duration or the error). A
streamed model call also writes a small `progress` line about once a second (characters
received so far and the last words), so a long call visibly moves. Kinds: `model`, `search`
(a web search engine), `page` (a page read; the page itself is public and not copied),
`speech` (a voice server), `gate` (a code check on a model's answer: not a request, but the
reason the next one is sent) and `run` (the run opened the file).

Every line carries the context of the call, set by the code that makes it, never guessed
from the log: the stage (from `stages.Tracker`), the part (or gap, or slide) a worker thread
is busy with, the role (the prompt: writer, checker, gap search, researcher...), the seat
(the writing or the checking model), the attempt of the retry loop, the conversation and the
turn of a research tool loop, and `parent`, the call that caused this one (the model turn
that asked for a search, the previous turn of the same conversation). The context lives in a
`contextvars.ContextVar`; `pipeline.TextPipeline._map` gives each worker thread a copy, so
parallel parts never mix.

The trace only observes: nothing here changes what the program does (ADR-0003, ADR-0005).
The file is opened by `cli.run` in the run's folder and appended to, so reruns keep their
history; with no file opened (tests, the app server), nothing is written.

API keys are never written: every value of a known key variable is replaced by "[key]", and
images are replaced by their size.
"""
from __future__ import annotations

import contextlib
import contextvars
import functools
import json
import os
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .keys import KNOWN

_lock = threading.Lock()
_path: Path | None = None
_stage: str | None = None
_context: contextvars.ContextVar[dict] = contextvars.ContextVar("l2a_trace", default={})
_last: contextvars.ContextVar[str | None] = contextvars.ContextVar("l2a_trace_last",
                                                                   default=None)
PROGRESS_EVERY = 1.0  # seconds between two progress lines of one call
TAIL = 240  # characters of the latest answer text kept in a progress line

# The prompt -> the role shown on the screen. A prompt is recognised by the phrase it
# contains (the same phrases as llm.dry_run_handler), when the caller did not name it.
ROLES = {"outline": "planner", "rewrite": "writer", "verify": "checker",
         "spoken": "voice script", "coverage": "gap search", "termbase": "termbase",
         "research": "researcher", "terms": "term checker", "course": "course writer",
         "course_check": "course judge", "cold_read": "cold reader", "ideas": "idea lister",
         "recitation": "recitation writer", "project": "project designer",
         "slide": "slide reader"}
PHRASES = (("You build the outline", "outline"), ("strict reviewer", "verify"),
           ("exact words a voice should say", "spoken"), ("You find the gaps", "coverage"),
           ("You list the atomic ideas", "ideas"), ("You build the termbase", "termbase"),
           ("You research one gap", "research"), ("You check terminology", "terms"),
           ("You write the recitation notes", "recitation"),
           ("You design the project", "project"),
           ("You write the course material", "course"), ("You read as a student", "cold_read"),
           ("You check the course material", "course_check"))
CONTEXT_KEYS = ("stage", "part", "part_title", "gap", "item", "role", "prompt", "seat",
                "attempt", "repair", "try", "audit", "conversation", "turn", "tool", "parent")


def open_run(work: Path | str) -> Path:
    global _path
    _path = Path(work) / "calls.jsonl"
    write("run", phase="open", pid=os.getpid())
    return _path


def close() -> None:
    global _path, _stage
    _path, _stage = None, None


def active() -> bool:
    return _path is not None


def set_stage(name: str | None) -> None:
    """The stage the run is in (called by stages.Tracker)."""
    global _stage
    _stage = name


def new_id() -> str:
    return secrets.token_hex(6)


def current() -> dict:
    """The context of the calls made here: the stage and what the callers set."""
    out = {"stage": _stage} if _stage else {}
    out.update(_context.get())
    return out


@contextlib.contextmanager
def context(**fields):
    """Add fields to the context of every call made inside (None values are ignored)."""
    token = _context.set({**_context.get(), **{k: v for k, v in fields.items() if v is not None}})
    try:
        yield
    finally:
        _context.reset(token)


def tagged(**fields):
    """A decorator: the function's calls carry these context fields."""
    def wrap(function):
        @functools.wraps(function)
        def inner(*args, **kwargs):
            with context(**fields):
                return function(*args, **kwargs)
        return inner
    return wrap


def last_id() -> str | None:
    """The id of the last call that finished in this context (thread)."""
    return _last.get()


def item_fields(item) -> dict:
    """The context of a work item handed to a worker: a part (outline.Node or a part's
    record) or a gap of the gap search."""
    if hasattr(item, "id") and hasattr(item, "title") and hasattr(item, "children"):
        return {"part": str(item.id), "part_title": item.title}
    if isinstance(item, dict):
        if item.get("kind") == "slides" and item.get("slides"):  # a slide range of the gap search
            span = item["slides"]
            label = (f"slides {span[0]}-{span[-1]}" if isinstance(span, (list, tuple))
                     else f"slides {span}")
            return {"item": label}
        if item.get("id") and item.get("node") is not None and "query" in item:  # a gap
            return {"gap": str(item["id"]), "part": str(item["node"]).split(".")[0]}
        if item.get("part") is not None:
            return {"part": str(item["part"]), "part_title": item.get("title")}
    return {}


def prompt_of(system: str | None) -> str | None:
    """The name of the prompt (prompts/<name>.md) a system text is, by its phrase."""
    for phrase, name in PHRASES:
        if system and phrase in system:
            return name
    return None


def _system_text(request: dict) -> str:
    if isinstance(request.get("system"), str):
        return request["system"]
    if isinstance(request.get("instructions"), str):
        return request["instructions"]
    for message in request.get("messages") or []:
        if isinstance(message, dict) and message.get("role") in ("system", "developer"):
            return str(message.get("content") or "")
    return ""


def _clean(value):
    """A copy that JSON can hold: keys replaced, images reduced to their size."""
    if isinstance(value, dict):
        if value.get("type") == "image_url" or value.get("type") == "base64":
            data = str((value.get("image_url") or {}).get("url") or value.get("data") or "")
            return {"type": value.get("type"), "omitted": f"image, {len(data)} characters"}
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, (bytes, bytearray)):
        return f"[{len(value)} bytes]"
    if hasattr(value, "model_dump"):  # SDK objects (completions, messages)
        return _clean(value.model_dump(mode="json", exclude_none=True))
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


def redact(text: str) -> str:
    """The text with the value of every known key variable replaced by "[key]"."""
    for var in KNOWN:
        key = os.environ.get(var)
        if key and len(key) >= 8:
            text = text.replace(key, "[key]")
    return text


_redact = redact  # older name


def write(kind: str, **fields) -> None:
    if _path is None:
        return
    record = {"time": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
              "kind": kind, **fields}
    line = redact(json.dumps(_clean(record), ensure_ascii=False)) + "\n"
    with _lock:
        if _path is not None:
            try:
                with _path.open("a", encoding="utf-8") as out:
                    out.write(line)
            except OSError:
                pass  # the trace only observes: a file it cannot write never stops the run


def note(kind: str, **fields) -> None:
    """One line that is not a request (a code gate), with the context and an id."""
    if _path is None:
        return
    write(kind, phase="point", id=new_id(), **{**current(), **fields})


class Call(dict):
    """What the caller adds to the end line (`record["answer"] = ...`), plus `feed(event)` for
    the events of a stream. Without a run, `id` is None and nothing is written."""

    def __init__(self, kind: str, call_id: str | None, meta: dict):
        super().__init__()
        self.kind, self.id, self.meta = kind, call_id, meta
        self.began = time.monotonic()
        self.chars = self.thinking = 0
        self.tail = ""
        self._shown = self.began

    def feed(self, event) -> None:
        """Count the text a streamed event brings (answer, reasoning, tool arguments) and write
        a progress line at most once every PROGRESS_EVERY seconds."""
        if self.id is None:
            return
        text, thinking = _delta(event)
        if not text and not thinking:
            return
        self.chars += len(text)
        self.thinking += len(thinking)
        self.tail = (self.tail + (text or thinking))[-TAIL:]
        now = time.monotonic()
        if now - self._shown >= PROGRESS_EVERY:
            self._shown = now
            write(self.kind, phase="progress", id=self.id, chars=self.chars,
                  thinking=self.thinking, tail=self.tail, seconds=round(now - self.began, 1))


def _delta(event) -> tuple[str, str]:
    """(answer text, reasoning text) of one streamed event of the anthropic or openai SDK."""
    kind = getattr(event, "type", "")
    if kind == "content_block_delta":  # anthropic
        delta = event.delta
        if getattr(delta, "type", "") == "thinking_delta":
            return "", delta.thinking or ""
        return (getattr(delta, "text", None) or getattr(delta, "partial_json", None) or ""), ""
    if kind == "chunk":  # openai chat completions (the stream also sends content.delta)
        text, thinking = "", ""
        for choice in getattr(event.chunk, "choices", None) or []:
            delta = choice.delta
            text += delta.content or ""
            extra = getattr(delta, "model_extra", None) or {}
            thinking += str(extra.get("reasoning_content") or extra.get("reasoning") or "")
            for tool in delta.tool_calls or []:
                text += (tool.function.arguments or "") if tool.function else ""
        return text, thinking
    if kind == "response.output_text.delta":  # openai responses
        return str(getattr(event, "delta", "") or ""), ""
    if kind.startswith("response.reasoning") and kind.endswith(".delta"):
        return "", str(getattr(event, "delta", "") or "")
    return "", ""


@contextlib.contextmanager
def call(kind: str, **request):
    """Records one request: a start line with what was sent, then an end line with
    `record["answer"]` and the other fields the caller set, or the error, and how long it
    took. The context of the call (stage, part, role...) is on both lines."""
    if _path is None:
        yield Call(kind, None, {})
        return
    call_id = new_id()
    meta = current()
    if kind == "model":
        meta.setdefault("prompt", prompt_of(_system_text(request)))
        if meta.get("prompt") and "role" not in meta:
            meta["role"] = ROLES.get(meta["prompt"], meta["prompt"])
        if request.get("model"):
            meta["model"] = request["model"]
    meta = {k: v for k, v in meta.items() if v is not None}
    record = Call(kind, call_id, meta)
    write(kind, phase="start", id=call_id, **meta, request=request)
    try:
        yield record
    except BaseException as exc:
        write(kind, phase="end", id=call_id, **meta, error=f"{exc.__class__.__name__}: {exc}",
              seconds=round(time.monotonic() - record.began, 2))
        raise
    finally:
        _last.set(call_id)
    write(kind, phase="end", id=call_id, **meta,
          seconds=round(time.monotonic() - record.began, 2), **record)


# ---------------------------------------------------------------------------- reading

def _usage(answer) -> tuple[int | None, int | None]:
    usage = answer.get("usage") if isinstance(answer, dict) else None
    if not isinstance(usage, dict):
        return None, None
    tokens_in = usage.get("input_tokens", usage.get("prompt_tokens"))
    tokens_out = usage.get("output_tokens", usage.get("completion_tokens"))
    return tokens_in, tokens_out


def answer_parts(answer) -> dict:
    """{"text", "reasoning", "tools": [{"name", "query" or "url"}]} of a model answer, in any
    of the shapes written (Chat Completions, Messages, Responses, the dry model's text)."""
    out = {"text": "", "reasoning": "", "tools": []}
    if isinstance(answer, str):
        out["text"] = answer
        return out
    if not isinstance(answer, dict):
        return out
    for choice in answer.get("choices") or []:  # chat completions
        message = choice.get("message") or {}
        out["text"] += str(message.get("content") or "")
        out["reasoning"] += str(message.get("reasoning_content") or message.get("reasoning") or "")
        for tool in message.get("tool_calls") or []:
            function = tool.get("function") or {}
            try:
                args = json.loads(function.get("arguments") or "{}")
            except ValueError:
                args = {}
            out["tools"].append({"name": function.get("name"),
                                 **{k: str(v) for k, v in args.items() if k in ("query", "url")}})
    for block in answer.get("content") or []:  # messages
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            out["text"] += str(block.get("text") or "")
        elif kind == "thinking":
            out["reasoning"] += str(block.get("thinking") or "")
        elif kind in ("server_tool_use", "tool_use"):
            args = block.get("input") or {}
            out["tools"].append({"name": block.get("name"),
                                 **{k: str(v) for k, v in args.items() if k in ("query", "url")}})
    for item in answer.get("output") or []:  # responses
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message":
            out["text"] += "".join(str(p.get("text") or "") for p in item.get("content") or [])
        elif item.get("type") == "reasoning":
            out["reasoning"] += " ".join(str(s.get("text") or "") for s in item.get("summary") or [])
        elif item.get("type") == "web_search_call":
            action = item.get("action") or {}
            out["tools"].append({"name": "web_search", "query": str(action.get("query") or "")})
    return out


def _last_user(request: dict) -> str:
    for message in reversed(request.get("messages") or []):
        if isinstance(message, dict) and message.get("role") in ("user", "tool"):
            content = message.get("content")
            if isinstance(content, list):
                content = " ".join(str(p.get("text") or "") for p in content
                                   if isinstance(p, dict))
            return str(content or "")
    return str(request.get("input") or "")


def summary(record: dict) -> dict:
    """The small form of one line of calls.jsonl that the app's screen streams: the context,
    the timing and the sizes, a few words of what was sent and answered, never the whole."""
    keep = ("time", "kind", "phase", "id", "model", "seconds", "chars", "thinking", "tail",
            "error", "status", "bytes", "ok", "what", "errors", *CONTEXT_KEYS)
    out = {k: record[k] for k in keep if record.get(k) not in (None, "", [])}
    request = record.get("request") if isinstance(record.get("request"), dict) else {}
    kind, phase = record.get("kind"), record.get("phase")
    if kind == "model":
        if phase == "start" or phase is None:
            out["host"] = request.get("host")
            out["model"] = out.get("model") or request.get("model")
            out["asks"] = _last_user(request)[-300:]
            out["sent"] = len(json.dumps(request.get("messages") or request.get("input") or "",
                                         ensure_ascii=False)) + len(_system_text(request))
            out["tools_offered"] = [((t.get("function") or {}).get("name") or t.get("name")
                                     or t.get("type")) for t in request.get("tools") or []
                                    if isinstance(t, dict)]
        if phase == "end" or phase is None:
            answer = record.get("answer")
            parts = answer_parts(answer)
            out["tokens_in"], out["tokens_out"] = _usage(answer)
            out["says"] = parts["text"][:300]
            out["answer_chars"], out["reasoning_chars"] = len(parts["text"]), len(parts["reasoning"])
            if parts["tools"]:
                out["tools"] = parts["tools"][:20]
    elif kind == "search":
        payload = request.get("payload") or {}
        out["query"] = str(payload.get("query") or payload.get("q") or "") or _query_of(
            str(request.get("url") or ""))
        answer = record.get("answer")
        results = (answer.get("results") or (answer.get("web") or {}).get("results") or []
                   if isinstance(answer, dict) else [])
        if results or phase == "end":
            out["results"] = [{"url": str(r.get("url") or ""), "title": str(r.get("title") or "")[:120]}
                              for r in results[:10] if isinstance(r, dict)]
    elif kind == "page":
        out["url"] = str(request.get("url") or "")
        out["type"] = record.get("type")
    elif kind == "speech":
        out["model"] = request.get("model")
        out["host"] = request.get("host")
    return {k: v for k, v in out.items() if v not in (None, "", [])}


def _query_of(url: str) -> str:
    from urllib.parse import parse_qs, urlsplit
    values = parse_qs(urlsplit(url).query)
    return (values.get("q") or values.get("query") or [""])[0]
