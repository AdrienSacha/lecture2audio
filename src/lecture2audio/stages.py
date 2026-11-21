"""The stage graph: every stage of a build, the files it writes under `out/<name>/`, and the
stages whose results it needs.

The pipeline was always a graph; this module makes it a declared one. What `--redo X` discards
(stage X and every stage that depends on it), the order the stages may run in, and the stage
section of `report.md` are all computed from `STAGES`, so they cannot drift apart. `check`
proves the graph well formed before anything runs (every stage it needs is declared before it,
no file has two writers), as artifact-driven workflow compilers do before execution
(ARTIC: Xu et al., 2026; STAGE: Luo et al., 2026). The graph stays fixed and owned by code:
models never choose the next stage, because letting a model route through a reviewed graph
cost 12 to 56 points of repeated-run success in the STAGE ablation.

A stage is either whole-lecture (one result for the lecture: the outline, the gap search,
the research) or per part (`per_part`: one result per part of the outline). A per-part stage of
part k needs only part k of the per-part stages it names, and the whole of the others; the
scheduler (`flow.py`) runs each (stage, part) node as soon as those are done, so a slow part
never holds back the next stage of the other parts (ADR-0006).

A new stage: add it here, after the stages it needs, with the files it caches.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import trace


@dataclass(frozen=True)
class Stage:
    name: str
    writes: tuple[str, ...]  # files and folders under out/<name>/
    needs: tuple[str, ...] = ()  # stages whose results this one reads
    redo: bool = True  # offered as --redo NAME
    discard_own: bool = True  # --redo NAME deletes this stage's own files
    rebuilt: bool = False  # not a cache: written again from the stage results on every run
    about: str = ""
    per_part: bool = False  # one result per part, run part by part as its inputs are ready


STAGES: tuple[Stage, ...] = (
    # the transcript is the user's file once written (they may correct misheard terms), so
    # --redo transcribe deletes it only for a recording, in cli._transcript
    Stage("slides", ("slides",),
          about="the slides read by sight (formulas as LaTeX, figures described), one call "
                "per slide"),
    Stage("transcribe", ("transcript.txt",), discard_own=False,
          about="speech recognition of a recording, or the transcript or the slides as given"),
    Stage("outline", ("outline.json",), ("transcribe", "slides"),
          about="parts, points and sub-points; every unit used once"),
    Stage("gaps", ("coverage.json", "coverage_audit.json", "termbase.json"), ("outline",),
          about="gap search over the whole lecture, its audit, and the termbase"),
    Stage("research", ("research",), ("gaps",),
          about="facts from the web for each gap, quotes checked word for word"),
    Stage("ideas", ("ideas", "ideas_audit.json"), ("outline",),
          about="atomic ideas, one call per part, and the planted-fact audit", per_part=True),
    Stage("rewrite", ("rewrite", "audit.json"), ("outline", "gaps", "research", "ideas"),
          about="script of each part, verified idea by idea, and the planted-omission audit",
          per_part=True),
    Stage("factcheck", ("factcheck",), ("rewrite", "ideas"), redo=False,
          about="MiniCheck on each lecture sentence against the units it cites"),
    Stage("spoken", ("spoken",), ("rewrite",),
          about="the exact words for the voice", per_part=True),
    Stage("audio", ("audio",), ("spoken",),
          about="voiced chunks, the round trip and the joined recording", per_part=True),
    Stage("parts", ("parts",), ("audio",), redo=False,
          about="one recording per part for the vault"),
    Stage("course", ("course",), ("rewrite", "ideas"),
          about="cards, exercises, the judge, the cold read and the revision", per_part=True),
    Stage("vault", ("vault",), ("course",), redo=False,
          about="the Obsidian notes"),
    Stage("recitation", ("recitation",), ("rewrite", "ideas", "course"),
          about="the recitation notes of each part, as blocks checked by code", per_part=True),
    Stage("project", ("project.json",), ("outline", "ideas", "gaps"),
          about="the project: one thing to build in checkable steps (--project)"),
    Stage("documents", ("recitation.tex", "recitation.pdf", "project.tex", "project.pdf",
                        ".build"), ("recitation", "project"), redo=False, rebuilt=True,
          about="the LaTeX documents and their PDFs"),
    Stage("ledger", ("ideas.csv", "anki.csv"), ("ideas", "rewrite", "course"), redo=False,
          rebuilt=True, about="the idea ledger and the Anki export"),
)

BY_NAME = {s.name: s for s in STAGES}


def check(stages: tuple[Stage, ...] = STAGES) -> list[str]:
    """Errors of a stage graph: unknown or later stages in `needs` (which also rules out
    cycles, since declaration order is then a topological order), and files with two
    writers."""
    errors, seen, writer = [], set(), {}
    for stage in stages:
        if stage.name in seen:
            errors.append(f"Stage {stage.name} is declared twice.")
        for need in stage.needs:
            if need not in seen:
                errors.append(f"Stage {stage.name} needs {need}, which is not declared "
                              "before it.")
        for path in stage.writes:
            if path in writer:
                errors.append(f"{path} is written by both {writer[path]} and {stage.name}.")
            writer[path] = stage.name
        seen.add(stage.name)
    return errors


def part_needs(name: str, stages: tuple[Stage, ...] = STAGES) -> tuple[list[str], list[str]]:
    """(the per-part stages whose same part a node of `name` needs, the stages it needs whole).
    A whole-lecture stage needs every part of a per-part stage, so all its needs are whole."""
    by_name = {s.name: s for s in stages}
    stage = by_name[name]
    same = [n for n in stage.needs if stage.per_part and by_name[n].per_part]
    return same, [n for n in stage.needs if n not in same]


def downstream(name: str, stages: tuple[Stage, ...] = STAGES) -> list[str]:
    """The stage and every stage that needs it, directly or not, in declaration order."""
    reached = {name}
    for stage in stages:  # declaration order is topological, so one pass suffices
        if any(n in reached for n in stage.needs):
            reached.add(stage.name)
    return [s.name for s in stages if s.name in reached]


def discards(name: str, stages: tuple[Stage, ...] = STAGES) -> list[str]:
    """The files --redo NAME deletes: those of the stage (unless it keeps them) and of every
    stage after it."""
    by_name = {s.name: s for s in stages}
    out: list[str] = []
    for stage_name in downstream(name, stages):
        stage = by_name[stage_name]
        if stage_name == name and not stage.discard_own:
            continue
        out += [p for p in stage.writes if p not in out]
    return out


def redo_table(stages: tuple[Stage, ...] = STAGES) -> dict[str, list[str]]:
    """{stage: files} for every stage offered to --redo."""
    return {s.name: discards(s.name, stages) for s in stages if s.redo}


def _saved(path: Path) -> bool:
    return path.is_file() or (path.is_dir() and any(path.iterdir()))


def present(work: Path) -> set[str]:
    """The stages that have a result on disk (an empty folder left by an interrupted run does
    not count)."""
    work = Path(work)
    return {s.name for s in STAGES if any(_saved(work / p) for p in s.writes)}


REUSED, COMPUTED, REBUILT, NOT_RUN = ("reused from the cache", "computed in this run",
                                      "rebuilt from the stage results", "not run")


def statuses(work: Path, before: set[str], override: dict[str, str] | None = None) -> dict:
    """What each stage did in this run: reused (its result was on disk when the run began and
    was not discarded), computed, rebuilt (not a cache) or not run."""
    now, override = present(work), override or {}
    out = {}
    for stage in STAGES:
        if stage.name in override:
            out[stage.name] = override[stage.name]
        elif stage.name not in now:
            out[stage.name] = NOT_RUN
        elif stage.rebuilt:
            out[stage.name] = REBUILT
        else:
            out[stage.name] = REUSED if stage.name in before else COMPUTED
    return out


class Tracker:
    """What a run is doing, as data. Each change of stage is one JSON line in
    `events.jsonl`, and on standard output as `@@stage {...}` when L2A_EVENTS is set: the app
    follows a run from these lines and does not have to guess from the wording of the log.

    Calling the tracker with a stage name closes every running stage and opens that one (the
    stages run one after the other). While parts stream through the stage graph, several
    stages run at once: `start` opens a stage without closing the others, and `stop` closes
    it; the scheduler opens a stage when its first node starts and closes it when its last
    node is done, so a stage is "running" while any part is in it and "done" when every part
    has finished it. A stage may be entered more than once, and its seconds add up (they
    overlap between stages that run at once). Thread-safe."""

    def __init__(self, work: Path):
        self.path = Path(work) / "events.jsonl"
        self.path.write_text("", encoding="utf-8")
        self.current: str | None = None  # the stage opened last, while it runs
        self.seconds: dict[str, float] = {}
        self._running: dict[str, float] = {}  # stage -> when it was opened
        self._lock = threading.RLock()
        self._announce = bool(os.environ.get("L2A_EVENTS"))

    def __call__(self, name: str) -> None:
        with self._lock:
            self.finish()
            self.start(name)

    def start(self, name: str) -> None:
        """Open a stage, leaving the others running (nothing happens if it is open)."""
        if name not in BY_NAME:
            raise ValueError(f"{name} is not a stage of stages.STAGES")
        with self._lock:
            if name in self._running:
                return
            self._running[name], self.current = time.monotonic(), name
            trace.set_stage(name)  # calls made outside a flow node say which stage they serve
            self._emit({"stage": name, "state": "running"})

    def stop(self, name: str) -> None:
        """Close a stage (nothing happens if it is not open)."""
        with self._lock:
            began = self._running.pop(name, None)
            if began is None:
                return
            self.seconds[name] = self.seconds.get(name, 0.0) + time.monotonic() - began
            self._emit({"stage": name, "state": "done", "seconds": round(self.seconds[name], 1)})
            if self.current == name:
                self.current = next(reversed(self._running), None)
                trace.set_stage(self.current)

    @property
    def running(self) -> list[str]:
        with self._lock:
            return list(self._running)

    def finish(self) -> None:
        """Close every stage that is running."""
        with self._lock:
            for name in list(self._running):
                self.stop(name)

    def _emit(self, event: dict) -> None:
        line = json.dumps(event)
        stamped = json.dumps({**event, "time": round(time.time(), 3)})  # for the timeline
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(stamped + "\n")
        if self._announce:
            print("@@stage " + line, flush=True)

    def report_line(self) -> str:
        """Where the time of the run went, for report.md."""
        spent = [(n, s) for n, s in self.seconds.items() if s >= 0.5]
        if not spent:
            return ""
        return ("Time spent in this run: " + ", ".join(
            f"{name} {seconds:.0f} s" for name, seconds in spent) + ".")


def mermaid(stages: tuple[Stage, ...] = STAGES) -> str:
    """The graph as a Mermaid flowchart."""
    lines = ["flowchart LR"]
    for stage in stages:
        lines += [f"  {need} --> {stage.name}" for need in stage.needs]
    return "\n".join(lines)


def report_lines(status: dict[str, str]) -> list[str]:
    """The stage section of report.md."""
    lines = ["## Stages of this run", "",
             "The stages and their order are fixed by code (`stages.py`); `--redo STAGE` "
             "discards the stage and every stage that depends on it.", "",
             "| Stage | Depends on | Result in this run |", "|---|---|---|"]
    for stage in STAGES:
        needs = ", ".join(stage.needs) or "the input"
        lines.append(f"| {stage.name} | {needs} | {status.get(stage.name, NOT_RUN)} |")
    return lines + [""]
