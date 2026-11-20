"""The data of the app's control room: what the AI did in a run, read from the run's own files.

- `calls.jsonl` (trace.py): every request, streamed to the page as small summaries
  (`trace.summary`), each with the byte offset of its line so the page can ask for the full
  line (`details`) when a call is opened;
- `events.jsonl` (stages.Tracker): the stages;
- `ideas.csv` (ledger.py) with the outline, the script and the cards: the idea graph
  (`idea_map`).

Everything is read, nothing is written. The files are being written while a run goes:
only complete lines are read (up to the last newline), a truncated `events.jsonl` (a new run)
starts the stages again, the ledger is replaced atomically by its writer, and a stage cache
caught half-written is skipped until the next read. Every text sent goes through
`trace.redact` once more, so a key that reached a file by some other path is still not sent.
"""
from __future__ import annotations

import csv
import io
import json
import subprocess
import time
from pathlib import Path

from . import stages, trace

UPLOADS = "_uploads"
CHUNK = 4 << 20  # bytes read from calls.jsonl at a time
MAX_LINE = 64 << 20  # a line of calls.jsonl longer than this is not sent in full
LIVE_SECONDS = 90  # a run whose trace changed this recently is shown as live
TRACE_FILES = ("calls.jsonl", "events.jsonl", "ideas.csv", "report.md", "outline.json")


def run_folder(out: Path, name: str) -> Path | None:
    """The folder of run `name` directly under `out`, or None (no path tricks, no uploads)."""
    if not name or "/" in name or "\\" in name or "\x00" in name or name in (".", "..", UPLOADS):
        return None
    folder = (out / name).resolve()
    if folder.parent != out.resolve() or not folder.is_dir():
        return None
    return folder


def _title(work: Path) -> str:
    try:
        return str(json.loads((work / "outline.json").read_text(encoding="utf-8")).get("title")
                   or work.name)
    except (OSError, ValueError, AttributeError):
        return work.name


def runs(out: Path) -> list[dict]:
    """Every run folder that has something to show, the most recent first."""
    found = []
    for folder in out.iterdir():
        if not folder.is_dir() or folder.name == UPLOADS or folder.name.startswith("."):
            continue
        times = {f: (folder / f).stat().st_mtime for f in TRACE_FILES if (folder / f).is_file()}
        if not times.keys() - {"outline.json"}:
            continue
        modified = max(times.values())
        found.append({"name": folder.name, "title": _title(folder), "modified": modified,
                      "calls": "calls.jsonl" in times, "ideas": "ideas.csv" in times,
                      "finished": "report.md" in times,
                      "live": time.time() - modified < LIVE_SECONDS and "report.md" not in times
                      or (times.get("calls.jsonl", 0) > times.get("report.md", 0) + 5
                          and time.time() - times["calls.jsonl"] < LIVE_SECONDS)})
    return sorted(found, key=lambda r: -r["modified"])


def _last_pid(work: Path) -> int | None:
    """The pid in the last `run` line of calls.jsonl (trace.open_run writes it)."""
    path = work / "calls.jsonl"
    try:
        size = path.stat().st_size
        with open(path, "rb") as handle:
            handle.seek(max(0, size - (8 << 20)))
            data = handle.read()
    except OSError:
        return None
    at = data.rfind(b'"kind": "run"')
    if at < 0:
        return None
    start = data.rfind(b"\n", 0, at) + 1
    end = data.find(b"\n", at)
    try:
        return int(json.loads(data[start:end if end >= 0 else None]).get("pid"))
    except (ValueError, TypeError, AttributeError):
        return None


def processes() -> str | None:
    """`ps` of every process: pid and command line (None when ps cannot run)."""
    try:
        return subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True,
                              timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return None


def process(work: Path, listing: str | None = None) -> dict:
    """Whether a lecture2audio process is working on this run now, started by the app or on
    the command line: the pid that opened calls.jsonl if it still runs lecture2audio, or else
    a process whose command line names this run (`--name NAME` or its folder). The processes
    are only looked at, never signalled."""
    listing = processes() if listing is None else listing
    if listing is None:
        return {"alive": None, "pid": None}  # unknown
    commands = {}
    for line in listing.splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) == 2 and fields[0].isdigit() and "lecture2audio" in fields[1]:
            commands[int(fields[0])] = fields[1]
    pid = _last_pid(work)
    if pid in commands:
        return {"alive": True, "pid": pid}
    for pid, command in commands.items():
        if " app" in command.split("lecture2audio", 1)[-1][:5]:
            continue  # the app server itself
        if (f"--name {work.name}" in command or f"--name={work.name}" in command
                or str(work) in command):
            return {"alive": True, "pid": pid}
    return {"alive": False, "pid": None}


def graph() -> list[dict]:
    """The stage graph, for the page: name, what it needs, what it does."""
    return [{"name": s.name, "needs": list(s.needs), "about": s.about} for s in stages.STAGES]


def outline_parts(work: Path) -> list[dict]:
    try:
        data = json.loads((work / "outline.json").read_text(encoding="utf-8"))
        return [{"id": str(k), "title": str(p.get("title") or "")}
                for k, p in enumerate(data.get("parts") or [], 1)]
    except (OSError, ValueError, AttributeError):
        return []


class Tail:
    """Reads the new complete lines of a growing file. `lines()` returns [(offset, bytes)];
    a file that became shorter (rewritten) makes `reset` True and starts again from 0."""

    def __init__(self, path: Path):
        self.path, self.offset, self.reset = path, 0, False

    def lines(self, limit: int = CHUNK) -> list[tuple[int, bytes]]:
        self.reset = False
        try:
            size = self.path.stat().st_size
        except OSError:
            return []
        if size < self.offset:
            self.offset, self.reset = 0, True
        if size == self.offset:
            return []
        out = []
        with open(self.path, "rb") as handle:
            handle.seek(self.offset)
            data = handle.read(min(limit, size - self.offset))
            end = data.rfind(b"\n")
            if end < 0:
                if len(data) < limit:
                    return []  # a line still being written
                # one line longer than the chunk: read on to its end
                data += handle.readline(MAX_LINE)
                if not data.endswith(b"\n"):
                    if len(data) < MAX_LINE:
                        return []  # still being written
                    self.offset += len(data) + len(handle.readline())  # too large: skipped
                    return []
                end = len(data) - 1
            data = data[:end + 1]
        start = 0
        for line in data.split(b"\n")[:-1]:
            out.append((self.offset + start, line))
            start += len(line) + 1
        self.offset += len(data)
        return out


def summaries(lines: list[tuple[int, bytes]]) -> list[dict]:
    """The summaries of lines of calls.jsonl, each with `at` (its offset). Progress lines of a
    call that a later line of the same batch supersedes are dropped, so replaying a long run
    sends one progress line per call at most."""
    out = []
    for offset, raw in lines:
        try:
            record = json.loads(trace.redact(raw.decode("utf-8", "replace")))
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        item = trace.summary(record)
        item["at"] = offset
        out.append(item)
    latest: dict[str, int] = {}
    for index, item in enumerate(out):
        if item.get("id"):
            latest[item["id"]] = index
    return [item for index, item in enumerate(out)
            if item.get("phase") != "progress" or latest.get(item.get("id")) == index]


def stage_events(lines: list[tuple[int, bytes]], mtime: float | None = None) -> list[dict]:
    """The stage lines. Lines written before events carried a time have none; the last line
    of the file then takes the file's modification time, which is when it was written, so a
    running stage shows how long it has been running even after a reload."""
    out = []
    for index, (_offset, raw) in enumerate(lines):
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("stage") in stages.BY_NAME:
            item = {k: event[k] for k in ("stage", "state", "seconds", "time") if k in event}
            if "time" not in item and mtime and index == len(lines) - 1:
                item["time"] = round(mtime, 3)
            out.append(item)
    return out


def details(work: Path, offsets: list[int]) -> list[dict]:
    """The full lines of calls.jsonl that start at these offsets (at most 8). An offset that
    is not the start of a line is refused, so only whole records are ever read."""
    path = work / "calls.jsonl"
    out = []
    try:
        size = path.stat().st_size
    except OSError:
        return []
    with open(path, "rb") as handle:
        for offset in offsets[:8]:
            if not 0 <= offset < size:
                continue
            if offset:
                handle.seek(offset - 1)
                if handle.read(1) != b"\n":
                    continue
            handle.seek(offset)
            raw = handle.readline(MAX_LINE)
            if not raw.endswith(b"\n"):
                out.append({"at": offset, "error": "This record is too large to show here; "
                                                    "open calls.jsonl to read it."})
                continue
            try:
                record = json.loads(trace.redact(raw.decode("utf-8", "replace")))
            except ValueError:
                continue
            if isinstance(record, dict):
                record["at"] = offset
                out.append(record)
    return out


# ------------------------------------------------------------------------------ ideas

def _json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None  # missing, or caught while being written: read again next time


def idea_signature(work: Path) -> tuple:
    """Changes when any file the idea graph is built from changes."""
    sig = []
    for path in [work / "ideas.csv", work / "outline.json",
                 *sorted((work / "rewrite").glob("part_*.json")),
                 *sorted((work / "course").glob("part_*.json"))]:
        try:
            stat = path.stat()
            sig.append((path.name, path.parent.name, stat.st_size, stat.st_mtime_ns))
        except OSError:
            pass
    return tuple(sig)


def idea_map(work: Path) -> dict:
    """The idea graph of a run: the ledger's rows, the parts and nodes, the script sentences
    that cite each idea, the cards that teach it and the verdicts of the judges."""
    try:
        text = (work / "ideas.csv").read_text(encoding="utf-8")
        rows = list(csv.DictReader(io.StringIO(text)))
    except (OSError, csv.Error):
        rows = []
    outline = _json(work / "outline.json") or {}
    nodes: dict[str, str] = {}

    def walk(items, prefix=""):
        for k, node in enumerate(items or [], 1):
            if not isinstance(node, dict):
                continue
            node_id = f"{prefix}{k}"
            nodes[node_id] = str(node.get("title") or "")
            walk(node.get("children"), node_id + ".")

    walk(outline.get("parts"))
    cites: dict[str, list[dict]] = {}
    verdicts: dict[str, list[dict]] = {}
    for path in sorted((work / "rewrite").glob("part_*.json")):
        record = _json(path)
        if not isinstance(record, dict):
            continue
        for section in record.get("sections") or []:
            for sentence in section.get("sentences") or []:
                for idea in sentence.get("ideas") or []:
                    found = cites.setdefault(str(idea), [])
                    if len(found) < 6:
                        found.append({"node": str(section.get("node")),
                                      "text": str(sentence.get("text") or "")[:400]})
        for item in record.get("ideas") or []:
            verdicts.setdefault(str(item.get("idea")), []).append(
                {"by": "checker", "status": item.get("status"),
                 "detail": str(item.get("detail") or "")[:400]})
    cards: dict[str, str] = {}
    for path in sorted((work / "course").glob("part_*.json")):
        record = _json(path)
        if not isinstance(record, dict):
            continue
        for card in record.get("cards") or []:
            cards[str(card.get("id"))] = str(card.get("title") or "")
        for item in record.get("ideas") or []:
            verdicts.setdefault(str(item.get("idea")), []).append(
                {"by": "course judge", "status": item.get("status"),
                 "detail": str(item.get("detail") or "")[:400]})
    ideas = []
    for row in rows:
        idea_id = str(row.get("idea_id") or "")
        if not idea_id:
            continue
        card_ids = [c for c in str(row.get("covered_in_card") or "").split(";") if c]
        ideas.append({"id": idea_id, "part": idea_id[1:].split(".")[0] if idea_id[:1] == "T"
                      else str(row.get("node_id") or "").split(".")[0],
                      "nodes": [n for n in str(row.get("node_id") or "").split(";") if n],
                      "kind": row.get("kind") or "", "statement": row.get("statement") or "",
                      "units": row.get("source_units") or "", "status": row.get("status") or "",
                      "cards": [{"id": c, "title": cards.get(c, "")} for c in card_ids],
                      "cited": cites.get(idea_id, []), "verdicts": verdicts.get(idea_id, [])})
    data = {"ideas": ideas, "parts": outline_parts(work), "nodes": nodes}
    return json.loads(trace.redact(json.dumps(data, ensure_ascii=False)))
