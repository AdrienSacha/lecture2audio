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

A new stage: add it here, after the stages it needs, with the files it caches.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Stage:
    name: str
    writes: tuple[str, ...]  # files and folders under out/<name>/
    needs: tuple[str, ...] = ()  # stages whose results this one reads
    redo: bool = True  # offered as --redo NAME
    discard_own: bool = True  # --redo NAME deletes this stage's own files
    rebuilt: bool = False  # not a cache: written again from the stage results on every run
    about: str = ""


STAGES: tuple[Stage, ...] = (
    # the transcript is the user's file once written (they may correct misheard terms), so
    # --redo transcribe deletes it only for a recording, in cli._transcript
    Stage("transcribe", ("transcript.txt",), discard_own=False,
          about="speech recognition of a recording, or the transcript as given"),
    Stage("outline", ("outline.json",), ("transcribe",),
          about="parts, points and sub-points; every unit used once"),
    Stage("gaps", ("coverage.json", "coverage_audit.json", "termbase.json"), ("outline",),
          about="gap search over the whole lecture, its audit, and the termbase"),
    Stage("research", ("research",), ("gaps",),
          about="facts from the web for each gap, quotes checked word for word"),
    Stage("ideas", ("ideas", "ideas_audit.json"), ("outline",),
          about="atomic ideas, one call per part, and the planted-fact audit"),
    Stage("rewrite", ("rewrite", "audit.json"), ("outline", "gaps", "research", "ideas"),
          about="script of each part, verified idea by idea, and the planted-omission audit"),
    Stage("factcheck", ("factcheck",), ("rewrite", "ideas"), redo=False,
          about="MiniCheck on each lecture sentence against the units it cites"),
    Stage("spoken", ("spoken",), ("rewrite",),
          about="the exact words for the voice"),
    Stage("audio", ("audio",), ("spoken",),
          about="voiced chunks, the round trip and the joined recording"),
    Stage("parts", ("parts",), ("audio",), redo=False,
          about="one recording per part for the vault"),
    Stage("course", ("course",), ("rewrite", "ideas"),
          about="cards, exercises, the judge, the cold read and the revision"),
    Stage("vault", ("vault",), ("course",), redo=False,
          about="the Obsidian notes"),
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
