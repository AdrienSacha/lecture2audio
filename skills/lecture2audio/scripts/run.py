#!/usr/bin/env python3
"""Run lecture2audio on a lecture and print a short summary an agent can read.

    python3 scripts/run.py ./lecture_folder/ [--text] [--out out] [any lecture2audio flag]
    python3 scripts/run.py --status out/<run>

The run's log goes to out/<run>/skill-run.log, not to the agent's context. The summary gives
the stages from events.jsonl, the request counts from calls.jsonl, the result files and the
open points of report.md. Standard library only. API keys are neither read nor printed here:
the command line loads them itself.
"""
from __future__ import annotations

import csv
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

RESULTS = ("report.md", "script.md", "glossary.md", "questions.md", "ideas.csv",
           "termbase.json", "index.md", "recitation.pdf", "project.pdf", "anki.csv")
OPEN = ("missing_script", "missing_cards", "disputed", "pending")


def command() -> list[str]:
    """How to start lecture2audio: the installed command, else this interpreter's module."""
    found = shutil.which("lecture2audio")
    return [found] if found else [sys.executable, "-m", "lecture2audio"]


def slug(name: str) -> str:  # same rule as lecture2audio.sources.slugify
    return re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").lower() or "lecture"


def summary(work: Path) -> str:
    lines = [f"Run folder: {work}"]
    events = work / "events.jsonl"
    if events.exists():
        state: dict[str, str] = {}
        for line in events.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            seconds = f" {event['seconds']} s" if "seconds" in event else ""
            state[event.get("stage", "?")] = event.get("state", "?") + seconds
        lines.append("Stages: " + ", ".join(f"{k} ({v})" for k, v in state.items()))
        running = [k for k, v in state.items() if v.startswith("running")]
        if running:
            lines.append(f"Stage still open: {running[-1]} (running, or the run stopped there)")
    calls = work / "calls.jsonl"
    if calls.exists():
        kinds: dict[str, int] = {}
        errors = 0
        with calls.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kinds[record.get("kind", "other")] = kinds.get(record.get("kind", "other"), 0) + 1
                errors += bool(record.get("error"))
        lines.append("Requests: " + ", ".join(f"{k} {n}" for k, n in kinds.items())
                     + f"; errors {errors}")
    present = [name for name in RESULTS if (work / name).exists()]
    present += sorted(p.name for p in work.glob("*.m4a"))
    lines.append("Results: " + (", ".join(present) or "none yet"))
    ledger = work / "ideas.csv"
    if ledger.exists():
        with ledger.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        counts = {s: sum(r.get("status") == s for r in rows) for s in OPEN}
        missing = ", ".join(f"{s} {n}" for s, n in counts.items() if n)
        lines.append(f"Ideas: {len(rows)}; not arrived: {missing or 'none'}")
    report = work / "report.md"
    if report.exists():
        heads = [h[3:] for h in report.read_text(encoding="utf-8").splitlines()
                 if h.startswith("## ")]
        lines.append("report.md sections: " + "; ".join(heads))
        lines.append(f"Read {report} next and report its open problems to the user.")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv[0] == "--status":
        if len(argv) < 2 or not Path(argv[1]).is_dir():
            print("give the run folder: --status out/<run>", file=sys.stderr)
            return 2
        print(summary(Path(argv[1])))
        return 0
    source, rest = Path(argv[0]), argv[1:]
    if not source.exists():
        print(f"input not found: {source}", file=sys.stderr)
        return 2
    verb = "build"
    if "--text" in rest:
        rest.remove("--text")
        verb = "text"
    out = Path(rest[rest.index("--out") + 1]) if "--out" in rest else Path("out")
    name = rest[rest.index("--name") + 1] if "--name" in rest else (
        source.resolve().name if source.is_dir() else source.stem)
    work = out / slug(name)
    work.mkdir(parents=True, exist_ok=True)
    args = [*command(), verb, str(source), *rest]
    if "--open-obsidian" not in rest:
        args.append("--no-open-obsidian")
    env = {k: v for k, v in os.environ.items() if k != "L2A_EVENTS"}
    print(f"Running: lecture2audio {verb} {source} {' '.join(rest)}".rstrip(), flush=True)
    with (work / "skill-run.log").open("w", encoding="utf-8") as log:
        code = subprocess.call(args, stdout=log, stderr=subprocess.STDOUT, env=env)
    print(f"Exit code: {code}")
    if code != 0:
        tail = (work / "skill-run.log").read_text(encoding="utf-8").splitlines()[-10:]
        print("Last log lines:\n  " + "\n  ".join(tail))
    print(summary(work))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
