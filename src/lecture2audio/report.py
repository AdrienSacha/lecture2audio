"""Human-readable outputs: script.md, index.md, questions.md, reference.md and report.md."""
from __future__ import annotations

from pathlib import Path

from .outline import Outline, ranges
from .pipeline import SOFT_ISSUES, list_sentence


def _clock(seconds: float) -> str:
    seconds = int(round(seconds))
    hours, rest = divmod(seconds, 3600)
    return f"{hours}:{rest // 60:02d}:{rest % 60:02d}" if hours else f"{rest // 60}:{rest % 60:02d}"


def write_script(path: Path, name: str, outline: Outline, records: list[dict],
                 language: str = "en") -> None:
    lines = [f"# {outline.title}", "",
             f"Script of `{name}`. Sentences in _italics_ were added by the model to clarify or "
             "connect. Sentences followed by a source mark such as [E1] come from outside the "
             "lecture, to fill a gap it left; the sources are listed at the end. All other "
             "sentences come from the lecture.", ""]
    for record in records:
        part = outline.node(record["part"])
        lines += [f"## {part.id} {part.title}", "",
                  "> " + record["preview"]["intro"].strip() + " "
                  + list_sentence(record["preview"].get("items") or [], language, "points"), ""]
        for section in record["sections"]:
            node = outline.node(section["node"])
            if node is not part:
                lines += ["#" * (node.level + 1) + f" {node.id} {node.title}", ""]
            if section.get("preview"):
                lines += ["> " + list_sentence(section["preview"], language, "subpoints"), ""]
            lines += [" ".join(_sentence(s) for s in section["sentences"]), ""]
        lines += ["**Recap.** " + " ".join(record["recap"]), ""]
    lines += _sources(records)
    path.write_text("\n".join(lines), encoding="utf-8")


def _sentence(s: dict) -> str:
    if s["origin"] == "added":
        return f"_{s['text']}_"
    if s["origin"] == "external":
        return f"{s['text']} [{s.get('source')}]"
    return s["text"]


def _sources(records: list[dict]) -> list[str]:
    used = {s.get("source") for r in records for sec in r["sections"] for s in sec["sentences"]
            if s["origin"] == "external"}
    items = [x for r in records for x in r.get("supplements", []) if x["id"] in used]
    if not items:
        return []
    lines = ["## Sources from outside the lecture", ""]
    for item in items:
        title = item.get("title") or item["url"]
        lines.append(f"- [{item['id']}] {title}. <{item['url']}>. Supporting passage: "
                     f"\"{item['quote']}\"")
    return lines + [""]


def write_glossary(path: Path, name: str, outline: Outline, terms: list[dict]) -> None:
    """Every technical term with its one definition, in the order the audio defines them."""
    lines = [f"# {outline.title}: glossary", "",
             "Each term has one meaning, used everywhere in the script. Terms are listed in the "
             "order in which the audio defines them.", ""]
    if not terms:
        lines += ["The termbase is empty: no technical term was found, or the termbase step "
                  "failed (see termbase.json).", ""]
    for term in terms:
        if term.get("from_series"):
            lines.append(f"**{term['term']}** (defined in the lecture {term['from_series']}). "
                         f"{term['definition']}")
        else:
            node = outline.node(term["define_at"])
            lines.append(f"**{term['term']}** (defined in {node.id} {node.title}). "
                         f"{term['definition']}")
        extra = []
        if term["depends_on"]:
            extra.append("Relies on: " + ", ".join(term["depends_on"]) + ".")
        if term["confusable"]:
            extra.append("Not to be confused with: " + ", ".join(term["confusable"]) + ".")
        if not term["in_lecture"]:
            extra.append("The lecture assumes this term without saying it; the audio defines it.")
        if term.get("from_glossary"):
            extra.append("Definition taken from your glossary.")
        if extra:
            lines.append("  " + " ".join(extra))
        lines.append("")
    path.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")


def write_index(path: Path, name: str, outline: Outline,
                chapters: list[tuple[str, float]] | None = None) -> None:
    """The tree of the lecture, with the time where each part and point starts in the audio."""
    starts, clock = {}, 0.0
    for key, seconds in chapters or []:
        starts[key] = clock
        clock += seconds
    lines = [f"# {outline.title}: index", "",
             "A map of the lecture, to find your way in the audio. It is not a set of notes.", ""]
    for part in outline.parts:
        for node in part.walk():
            at = f" ({_clock(starts[node.id])})" if node.id in starts else ""
            lines.append("  " * (node.level - 1) + f"- {node.id} {node.title}{at}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_questions(path: Path, name: str, outline: Outline, records: list[dict]) -> None:
    """Questions first, answers at the end: for testing yourself on a later day."""
    lines = [f"# {outline.title}: questions", "",
             "Answer from memory first, then check the answers at the end of this file.", ""]
    answers = ["## Answers", ""]
    for record in records:
        part = outline.node(record["part"])
        lines += [f"## {part.id} {part.title}", ""]
        answers += [f"### {part.id} {part.title}", ""]
        for k, item in enumerate(record["questions"], 1):
            lines.append(f"{k}. {item['question']}")
            answers.append(f"{k}. {item['answer']}")
        lines.append("")
        answers.append("")
    path.write_text("\n".join(lines + answers), encoding="utf-8")


def write_reference(path: Path, name: str, outline: Outline, records: list[dict]) -> None:
    lines = [f"# {outline.title}: formulas, code and tables, copied from the transcript or slides", ""]
    found = False
    for record in records:
        for item in record["reference"]:
            found = True
            lines += [f"**{item['label']}** (part {record['part']})", "", "```",
                      str(item["content"]), "```", ""]
    if not found:
        lines.append("Nothing of this kind was extracted.")
        lines.append("")
    lines += _sources(records)
    path.write_text("\n".join(lines), encoding="utf-8")


def _issue_line(issue: dict) -> str:
    where = [f"{k} {issue[k]}" for k in ("node", "sentence", "unit") if issue.get(k)]
    soft = " (minor)" if issue["type"] in SOFT_ISSUES else ""
    return f"- {issue['type']}{soft}{', ' + ', '.join(where) if where else ''}: {issue['detail']}"


def write_report(path: Path, name: str, outline: Outline, records: list[dict],
                 spoken: list[dict], notes: list[str], audit: dict | None,
                 usage: dict | None = None, roundtrip: list[dict] | None = None,
                 gaps: dict | None = None) -> None:
    lines = [f"# {name}: quality report", ""]
    flagged = [r for r in records if r["flagged"]]
    lines.append(f"**{len(flagged)} part(s) still have open problems after the retries.** Listen "
                 "to them with the transcript at hand." if flagged else
                 "No part has open problems.")
    lines += ["", "| Part | Title | Words | Attempts | Open problems | Digits/symbols left by the model |",
              "|---|---|---|---|---|---|"]
    for record, sp in zip(records, spoken):
        res = sp["llm_residue"]
        left = res["digits"] + sum(res["symbols"].values())
        lines.append(f"| {record['part']} | {record['title']} | {record['words']} | "
                     f"{record['attempts']} | {record['blocking']} | {left} |")
    lines.append("")

    lines += ["## Checks on the whole lecture", ""]
    if audit and audit.get("skipped"):
        lines.append(f"- Verifier test skipped: {audit['skipped']}.")
    elif audit:
        verdict = ("it reported the omission" if audit["caught"] else
                   "**it did not notice the omission**, so its passes are less trustworthy; "
                   "consider a stronger or a different verifier model (--verify-model, --verify-llm)")
        lines.append(f"- Verifier test: {audit['removed']} sentences were removed from "
                     f"{audit['node']} \"{audit['title']}\" and the script was checked again; "
                     f"{verdict}.")
    lines += [f"- {note}" for note in notes] or []
    if not notes and not audit:
        lines.append("- Nothing to report.")
    lines.append("")

    if gaps:
        lines += _gap_lines(outline, gaps)

    moved = [(r["part"], m) for r in records for m in r.get("moves", [])]
    if moved:
        lines += ["## Order changes made by the outline", "",
                  "These units were placed earlier than the teacher said them, under the point "
                  "they belong to.", ""]
        for part_id, move in moved:
            first, last = move["units"]
            lines.append(f"- part {part_id}: units {first}-{last} moved under {move['node']} "
                         f"\"{move['title']}\" (after unit {move['after_unit']} had been said)")
        lines.append("")

    for record, sp in zip(records, spoken):
        notes_here = [_issue_line(i) for i in record["issues"]]
        if sp.get("fallback"):
            notes_here.append(f"- spoken form: {sp['fallback']}")
        if notes_here:
            lines += [f"## Part {record['part']}: {record['title']}", ""] + notes_here + [""]

    if usage:
        lines += ["## Model use in this run", "", "| Model | Calls | Input tokens | Output tokens |",
                  "|---|---|---|---|"]
        for model, u in usage.items():
            lines.append(f"| {model} | {u['calls']} | {u['input_tokens']} | {u['output_tokens']} |")
        lines.append("")

    if roundtrip:
        lines += ["## Round trip (voice, then speech recognition, compared with the script)", "",
                  "| Part | Chunk | Similarity | Heard |", "|---|---|---|---|"]
        for item in roundtrip:
            lines.append(f"| {item['segment'] + 1} | {item['chunk'] + 1} | {item['score']:.2f} | "
                         f"{item['heard'][:80]} |")
        lines.append("")

    noise = outline.noise_units()
    lines += ["## Removed as noise", ""]
    if noise:
        lines.append(f"Units {ranges(noise)}. Check that none of them holds teaching content.")
        lines.append("")
        lines += [f"- [{u}] {outline.text(u)}" for u in noise]
    else:
        lines.append("No unit was marked as noise.")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


KIND_NAMES = {"slide_missing": "slide idea not taught", "undefined_term": "term never defined",
              "assumed_fundamental": "fundamental taken for granted",
              "skipped_step": "step skipped"}


def _gap_lines(outline: Outline, gaps: dict) -> list[str]:
    coverage, research = gaps["coverage"], gaps["research"]
    lines = ["## Gaps in the lecture and how they were filled", ""]
    if coverage.get("error"):
        lines += [f"The gap search failed: {coverage['error']}", ""]
    ideas = coverage.get("slide_ideas", [])
    if ideas:
        counts = {s: sum(i["status"] == s for i in ideas) for s in ("covered", "partial", "missing")}
        lines += [f"Slides: {len(ideas)} ideas, of which {counts['covered']} are taught in the "
                  f"lecture, {counts['partial']} in part and {counts['missing']} not at all.", ""]
    if coverage.get("transcript_ideas"):
        lines += [f"Transcript: {len(coverage['transcript_ideas'])} atomic ideas were listed; the "
                  "course material is checked against this list.", ""]
    audit = gaps.get("audit")
    if audit and audit.get("skipped"):
        lines += [f"Gap search test skipped: {audit['skipped']}.", ""]
    elif audit:
        lines += [f"Gap search test: unit {audit['unit']} of {audit['node']} \"{audit['title']}\" "
                  "was blanked and the gap search run again; "
                  + ("it reported a new gap there." if audit["caught"] else
                     "**it reported nothing new there**, so a lecture with few gaps may simply "
                     "have been missed; consider a stronger model (--rewrite-model)."), ""]
    if not coverage.get("gaps"):
        lines += ["No gap was found.", ""]
        return lines + _term_summary(gaps)
    if gaps.get("note"):
        lines += [f"The gaps were not searched on the web: {gaps['note']}.", ""]
    used = {s["gap"]: [] for s in gaps["supplements"]}
    for s in gaps["supplements"]:
        used[s["gap"]].append(s["id"])
    lines += ["| Gap | Kind | Where | What is missing | Result |", "|---|---|---|---|---|"]
    for k, gap in enumerate(coverage["gaps"]):
        node = outline.node(str(gap["node"]))
        found = research.get(gap["id"])
        if gap["id"] in used:
            result = "filled from outside sources: " + ", ".join(used[gap["id"]])
        elif found and found.get("error"):
            result = f"search failed: {found['error'][:120]}"
        elif found:
            result = "no reliable source found; not filled"
        else:
            result = "not searched"
        detail = str(gap["detail"]).replace("|", "/")
        lines.append(f"| {gap['id']} | {KIND_NAMES.get(gap['kind'], gap['kind'])} | "
                     f"{node.id} {node.title} | {detail} | {result} |")
    lines.append("")
    rejected = [(g, r) for g, res in research.items() for r in res.get("rejected", [])]
    if rejected:
        lines += ["Facts found online but discarded, because their quote could not be checked:", ""]
        lines += [f"- {g}: {r.get('text', '')} <{r.get('url', '')}> ({r.get('reason', '')})"
                  for g, r in rejected]
        lines.append("")
    return lines + _term_summary(gaps)


def _term_summary(gaps: dict) -> list[str]:
    terms = gaps.get("terms") or []
    if not terms:
        return []
    assumed = [t["term"] for t in terms if not t["in_lecture"]]
    lines = ["## Terminology", "",
             f"{len(terms)} terms, each with one definition (see glossary.md). Every use of a term "
             "in the script was checked against its definition."]
    if assumed:
        lines.append(f"Terms the lecture relies on without saying them, defined in the audio: "
                     f"{', '.join(assumed)}.")
    return lines + [""]
