"""A course of several lectures: one definition per term and one meaning per symbol across all of
them, and one vault.

The lectures are first prepared one by one (outline, gaps, termbase). Their termbases are then
merged: the lecture that defines a term first owns it, and every later lecture borrows that
definition and treats the term as already known. The lectures are then finished in order, each
one starting from the notation of the lectures before it.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import enrich
from .course import build_review, collect_notation
from .outline import Outline


def _same(a: str, b: str) -> bool:
    return " ".join(a.lower().split()).rstrip(".") == " ".join(b.lower().split()).rstrip(".")


def merge_termbases(works: list[Path], names: list[str]) -> list[dict]:
    """Rewrite each lecture's termbase.json so that every term has one definition across the
    course. Returns the conflicts: the same term defined differently in two lectures (the first
    lecture's definition wins)."""
    owners: dict[str, tuple[int, dict]] = {}
    conflicts = []
    for number, work in enumerate(works):
        path = work / "termbase.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        for entry in data["terms"]:
            key = enrich._key(entry["term"])
            if entry.get("from_series"):
                continue
            if key not in owners:
                owners[key] = (number, entry)
                continue
            first, owner = owners[key]
            if not _same(owner["definition"], entry["definition"]):
                conflicts.append({"term": entry["term"], "kept": names[first],
                                  "kept_definition": owner["definition"], "dropped": names[number],
                                  "dropped_definition": entry["definition"]})
            entry.update(definition=owner["definition"], part=0, from_series=names[first],
                         from_glossary=owner.get("from_glossary", False))
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return conflicts


def load_lecture(work: Path, name: str) -> dict:
    """Everything the vault needs from a finished lecture, read back from its caches."""
    read = lambda p: json.loads(p.read_text(encoding="utf-8"))  # noqa: E731
    outline = Outline.from_dict(read(work / "outline.json"))
    records = [read(p) for p in sorted((work / "rewrite").glob("part_*.json"))]
    parts = [read(p) for p in sorted((work / "course").glob("part_*.json"))]
    cold = [read(p) for p in sorted((work / "course").glob("cold_*.json"))]
    notation, clashes = collect_notation(parts)
    course = {"parts": parts, "notation": notation, "clashes": clashes,
              "review": build_review(parts), "cold": cold}
    terms = read(work / "termbase.json")["terms"] if (work / "termbase.json").exists() else []
    return {"name": name, "outline": outline, "records": records, "course": course,
            "terms": terms}


def series_report(path: Path, course_name: str, lectures: list[dict], conflicts: list[dict],
                  stats: dict) -> None:
    lines = [f"# {course_name}: course report", "",
             f"The course has {len(lectures)} lectures, {stats['terms']} terms, each with one "
             f"definition for the whole course, and {stats['cards']} cards in {stats['notes']} "
             "notes.", ""]
    lines += ["| Lecture | Parts | Cards | Terms it defines | Terms it borrows |", "|---|---|---|---|---|"]
    for lecture in lectures:
        cards = sum(len(p["cards"]) for p in lecture["course"]["parts"])
        borrowed = [t for t in lecture["terms"] if t.get("from_series")]
        lines.append(f"| {lecture['name']} | {len(lecture['course']['parts'])} | {cards} | "
                     f"{len(lecture['terms']) - len(borrowed)} | {len(borrowed)} |")
    lines.append("")
    if conflicts:
        lines += ["## Terms defined differently in two lectures", "",
                  "A term has one definition in the course. In each case below the definition of "
                  "the earlier lecture was kept for both lectures. Decide whether the later "
                  "lecture really teaches another meaning; if it does, give the two meanings "
                  "different names in a glossary file and run the course again.", ""]
        for c in conflicts:
            lines += [f"- **{c['term']}**. Kept from {c['kept']}: {c['kept_definition']} Dropped "
                      f"from {c['dropped']}: {c['dropped_definition']}"]
        lines.append("")
    else:
        lines += ["No term is defined differently in two lectures.", ""]
    clashes = [c for lecture in lectures for c in lecture["course"]["clashes"]]
    if clashes:
        lines += ["## Symbols with two meanings", ""] + [f"- {c}" for c in clashes] + [""]
    path.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")
