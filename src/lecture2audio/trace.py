"""`calls.jsonl`: one line per request the program sends over the network, in full.

Each model call is written with its host, model, every message sent, the answer (the whole
completion as the provider returned it, reasoning included), the token counts, the duration
and any error. Each web search is written with its query and results, and each page read with
its address and size (the page itself is public and not copied). The file is opened by
`cli.run` in the run's folder and appended to, so reruns keep their history; with no file
opened (tests, the app server), nothing is written.

API keys are never written: every value of a known key variable is replaced by "[key]", and
images are replaced by their size.
"""
from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .keys import KNOWN

_lock = threading.Lock()
_path: Path | None = None


def open_run(work: Path | str) -> Path:
    global _path
    _path = Path(work) / "calls.jsonl"
    return _path


def close() -> None:
    global _path
    _path = None


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


def _redact(text: str) -> str:
    for var in KNOWN:
        key = os.environ.get(var)
        if key and len(key) >= 8:
            text = text.replace(key, "[key]")
    return text


def write(kind: str, **fields) -> None:
    if _path is None:
        return
    record = {"time": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
              "kind": kind, **fields}
    line = _redact(json.dumps(_clean(record), ensure_ascii=False)) + "\n"
    with _lock:
        with _path.open("a", encoding="utf-8") as out:
            out.write(line)


@contextlib.contextmanager
def call(kind: str, **request):
    """Records one request: what was sent, then `result["answer"]` (set by the caller) or the
    error, and how long it took."""
    if _path is None:
        yield {}
        return
    result: dict = {}
    start = time.monotonic()
    try:
        yield result
    except BaseException as exc:
        write(kind, request=request, error=f"{exc.__class__.__name__}: {exc}",
              seconds=round(time.monotonic() - start, 2))
        raise
    write(kind, request=request, seconds=round(time.monotonic() - start, 2), **result)
