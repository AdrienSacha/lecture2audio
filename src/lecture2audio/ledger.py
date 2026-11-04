"""The idea ledger: `out/<name>/ideas.csv`, one row per atomic idea the teacher taught.

`TextPipeline.run_ideas` lists the transcript's atomic ideas, one call per part (T3.12 is the
twelfth idea of part 3). The ledger records each one with its node and its transcript units, then follows it through the build: the verifier of the
script checks it off ("scripted" or "missing_script"), the judge of the course material checks
it off against the cards ("verified", with the cards that teach it, or "missing_cards"). The
file is rebuilt from the cached stage results on every run, so it always matches them, and
`report.md` lists every idea that did not reach the end.

`Ledger` keeps the file current during the run: it is rewritten each time a part finishes its
script or its cards, always rebuilt from the seed and every result received so far (never edited
in place), and replaced atomically, so a reader never sees a half-written file and the file never
says more than the saved results do.

The ledger also feeds `anki.csv` (`--export-anki`): the check questions of the cards, tagged with
the ideas they test, and one recall card for each verified idea that no check question tests.
"""
from __future__ import annotations

import copy
import csv
import os
import re
import threading
from pathlib import Path

from .outline import Outline

COLUMNS = ["idea_id", "node_id", "kind", "statement", "source_units", "covered_in_card", "status"]
PENDING, SCRIPTED, VERIFIED = "pending", "scripted", "verified"
MISSING_SCRIPT, MISSING_CARDS = "missing_script", "missing_cards"
# A judge says "taught", but code does not find a number of the idea in what was judged. Neither
# is overruled: the idea is not counted as arrived, and report.md shows it for a human to decide.
DISPUTED = "disputed"
STATUSES = (PENDING, SCRIPTED, MISSING_SCRIPT, VERIFIED, MISSING_CARDS, DISPUTED)

KIND_WORDS = {
    "en": {"definition": "definition", "claim": "claim", "step": "step", "formula": "formula",
           "condition": "condition", "example": "example", "warning": "warning"},
    "fr": {"definition": "définition", "claim": "affirmation", "step": "étape",
           "formula": "formule", "condition": "condition", "example": "exemple",
           "warning": "avertissement"},
}
RECALL = {"en": "What does the lecture state in “{title}” ({kind})?",
          "fr": "Qu'énonce le cours dans « {title} » ({kind}) ?"}


def _ranges(units: list[int]) -> str:
    """[3, 4, 5, 7] -> "3-5;7"."""
    out, run = [], []
    for unit in sorted(set(units)):
        if run and unit == run[-1] + 1:
            run.append(unit)
            continue
        if run:
            out.append(f"{run[0]}-{run[-1]}" if len(run) > 1 else str(run[0]))
        run = [unit]
    if run:
        out.append(f"{run[0]}-{run[-1]}" if len(run) > 1 else str(run[0]))
    return ";".join(out)


def _split(value: str) -> list[str]:
    return [v for v in str(value or "").split(";") if v]


def seed(outline: Outline, coverage: dict) -> list[dict]:
    """One pending row per transcript idea, in lecture order."""
    rows = []
    for idea in coverage.get("transcript_ideas", []):
        nodes = [str(n) for n in idea.get("nodes", [])]
        units: list[int] = [int(u) for u in idea.get("units") or []]
        for node_id in nodes if not units else []:  # no units given: those of its nodes
            try:
                node = outline.node(node_id)
            except (KeyError, ValueError, IndexError):
                continue
            units += node.own_units() or node.all_units()
        rows.append({"idea_id": str(idea["id"]), "node_id": ";".join(nodes),
                     "kind": str(idea.get("kind", "")), "statement": str(idea["idea"]).strip(),
                     "source_units": _ranges(units), "covered_in_card": "", "status": PENDING})
    return rows


def apply_script(rows: list[dict], records: list[dict]) -> list[dict]:
    """The verifier's check of each idea against the script. An idea split over two parts is
    scripted when any part teaches it."""
    verdicts: dict[str, set[str]] = {}
    for record in records:
        for item in record.get("ideas", []):
            verdict = item.get("status")
            if verdict == "taught" and item.get("lost"):
                verdict = DISPUTED
            verdicts.setdefault(str(item.get("idea")), set()).add(verdict)
    for row in rows:
        seen = verdicts.get(row["idea_id"])
        if not seen or row["status"] == VERIFIED:
            continue
        row["status"] = (SCRIPTED if "taught" in seen else DISPUTED if DISPUTED in seen
                         else MISSING_SCRIPT)
    return rows


def apply_cards(rows: list[dict], course: dict) -> list[dict]:
    """The judge's check of each idea against the cards: the cards that teach it, and the
    status "verified". A taught idea with no card named stays at its earlier status."""
    taught: dict[str, list[str]] = {}
    disputed: set[str] = set()
    missing: set[str] = set()
    for part in course.get("parts", []):
        for item in part.get("ideas", []):
            idea = str(item.get("idea"))
            if item.get("status") == "taught" and item.get("cards"):
                taught.setdefault(idea, [])
                taught[idea] += [c for c in item["cards"] if c not in taught[idea]]
                if item.get("lost"):
                    disputed.add(idea)
            elif item.get("status") == "missing":
                missing.add(idea)
    for row in rows:
        if row["idea_id"] in taught:
            row["covered_in_card"] = ";".join(taught[row["idea_id"]])
            row["status"] = DISPUTED if row["idea_id"] in disputed else VERIFIED
        elif row["idea_id"] in missing:
            row["covered_in_card"] = ""
            row["status"] = MISSING_CARDS
    return rows


def write(path: Path, rows: list[dict]) -> None:
    """Write the ledger through a temporary file and an atomic rename."""
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows({k: row.get(k, "") for k in COLUMNS} for row in rows)
    os.replace(temporary, path)


class Ledger:
    """ideas.csv, kept current while the parts run in parallel.

    Every update rebuilds all rows from the seed and the results received so far, in part
    order, then writes the file; the rows are therefore a pure function of the saved stage
    results, whatever order the parts finish in."""

    def __init__(self, path: Path, outline: Outline, coverage: dict):
        self.path = Path(path)
        self._seed = seed(outline, coverage)
        self._records: dict[str, dict] = {}
        self._cards: dict[str, dict] = {}
        self._lock = threading.Lock()
        self.rows: list[dict] = []
        self._rebuild()

    @staticmethod
    def _ordered(results: dict[str, dict]) -> list[dict]:
        return [results[k] for k in sorted(results, key=int)]

    def _rebuild(self) -> None:
        rows = copy.deepcopy(self._seed)
        apply_script(rows, self._ordered(self._records))
        apply_cards(rows, {"parts": self._ordered(self._cards)})
        self.rows[:] = rows  # same list object: callers that hold it see the update
        write(self.path, self.rows)

    def script_done(self, record: dict) -> None:
        """A part's script was verified (called from a worker thread)."""
        with self._lock:
            self._records[str(record["part"])] = record
            self._rebuild()

    def cards_done(self, part: dict) -> None:
        """A part's cards were judged (called from a worker thread)."""
        with self._lock:
            self._cards[str(part["part"])] = part
            self._rebuild()


def read(path: Path) -> list[dict]:
    with open(path, encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def open_rows(rows: list[dict], course_ran: bool) -> list[dict]:
    """The ideas that did not reach the last stage that ran."""
    done = {VERIFIED} if course_ran else {SCRIPTED, VERIFIED}
    return [r for r in rows if r["status"] not in done]


def parts_without_ideas(outline: Outline, rows: list[dict]) -> list[str]:
    """Parts that teach (they have units) but have no idea in the ledger: the sign of an idea
    list cut short, for example by the output limit of the gap search on a long lecture."""
    named = {n.split(".")[0] for r in rows for n in _split(r["node_id"])}
    return [p.id for p in outline.parts if p.all_units() and p.id not in named]


# ------------------------------------------------------------------------- Anki

_DISPLAY = re.compile(r"\$\$(.+?)\$\$", re.S)
_INLINE = re.compile(r"\$([^$\n]+)\$")


def anki_math(text: str) -> str:
    """$...$ and $$...$$ as the MathJax delimiters Anki reads: \\(...\\) and \\[...\\]."""
    text = _DISPLAY.sub(lambda m: "\\[" + m.group(1) + "\\]", str(text))
    return _INLINE.sub(lambda m: "\\(" + m.group(1) + "\\)", text)


def _tag(text: str) -> str:
    return re.sub(r"[^\w:.-]+", "_", str(text)).strip("_")


def anki_rows(name: str, rows: list[dict], course: dict | None, records: list[dict],
              language: str = "en") -> list[tuple[str, str, str]]:
    """(Front, Back, Tags) for spaced repetition. With course material: every check question
    of every card, tagged with the ideas the card teaches, then one recall card for each
    verified idea that no check question tests. Without it: the questions of the script."""
    base = _tag(name)
    out: list[tuple[str, str, str]] = []
    if not course:
        for record in records:
            tags = f"{base} {base}::part-{int(record['part']):02d}"
            for item in record.get("questions", []):
                out.append((anki_math(item["question"]), anki_math(item["answer"]), tags))
        return out
    by_card: dict[str, list[str]] = {}
    for row in rows:
        for card_id in _split(row["covered_in_card"]):
            by_card.setdefault(card_id, []).append(row["idea_id"])
    tested: set[str] = set()
    titles: dict[str, str] = {}
    for part in course["parts"]:
        for card in part["cards"]:
            titles[card["id"]] = card["title"]
            ideas = by_card.get(card["id"], [])
            tags = " ".join([base, f"{base}::part-{int(part['part']):02d}",
                             f"{base}::card-{_tag(card['id'])}",
                             *(f"{base}::idea-{_tag(i)}" for i in ideas)])
            for check in card["checks"]:
                out.append((anki_math(check["question"]), anki_math(check["answer"]), tags))
                tested.update(ideas)
        for exercise in part.get("exercises", []):
            back = " ".join(f"{k}. {step}" for k, step in enumerate(exercise["steps"], 1))
            out.append((anki_math(exercise["question"]),
                        anki_math(f"{back} → {exercise['answer']}"),
                        f"{base} {base}::part-{int(part['part']):02d} {base}::exercise"))
    kinds = KIND_WORDS.get(language, KIND_WORDS["en"])
    recall = RECALL.get(language, RECALL["en"])
    for row in rows:
        if row["status"] != VERIFIED or row["idea_id"] in tested:
            continue
        card_id = _split(row["covered_in_card"])[0]
        part = card_id.split(".")[0]
        front = recall.format(title=titles.get(card_id, card_id),
                              kind=kinds.get(row["kind"], row["kind"]))
        tags = " ".join([base, f"{base}::part-{int(part):02d}", f"{base}::card-{_tag(card_id)}",
                         f"{base}::idea-{_tag(row['idea_id'])}"])
        out.append((front, anki_math(row["statement"]), tags))
    return out


def write_anki(path: Path, name: str, rows: list[dict], course: dict | None,
               records: list[dict], language: str = "en") -> int:
    """An Anki import file (File > Import): the header lines set the separator and the
    columns Front, Back, Tags. Returns the number of notes."""
    notes = anki_rows(name, rows, course, records, language)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write("#separator:Comma\n#html:false\n#columns:Front,Back,Tags\n#tags column:3\n")
        csv.writer(handle, lineterminator="\n").writerows(notes)
    return len(notes)
