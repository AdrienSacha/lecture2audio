"""The text stages: outline -> rewrite each part (code gates, verifier, keep the best attempt)
-> spoken form.

Design (see CLAUDE.md for the reasons): a fixed chain of model calls with code checks between
them, not agents talking to each other. One call plans the whole lecture (the outline); each
part is then written and checked on its own, so parts can run in parallel. Every stage writes
its result under the work directory, so a rerun skips finished work and any intermediate file
can be opened or edited by hand.
"""
from __future__ import annotations

import contextvars
import copy
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import enrich, trace
from . import library as books
from .llm import LLM, LLMError, extract_json
from .outline import (Node, Outline, checklist, dump, fallback_outline, moves, noise_by_part,
                      parse_outline, part_source, part_words, ranges, render_outline,
                      render_part_transcript, structure)
from .sources import select_slides
from .spoken import (en_cardinal, en_ordinal, find_residue, fr_cardinal, fr_ordinal,
                     normalize_spoken, number_to_words)

PROMPT_DIR = Path(__file__).parent / "prompts"
LANGUAGE_NAMES = {"en": "English", "fr": "French"}
ORIGINS = {"lecture", "added", "external"}
STATUSES = {"complete", "incomplete", "misplaced"}
# Warnings: reported and shown to the writer with other feedback, never a reason to rewrite.
SOFT_ISSUES = {"long", "filler", "plan", "marker", "idea_numbers", "unledgered", "plain_long",
               "duplicate_question"}


SLIDES_ONLY = ("slides only. There is no recording and no transcript: the units are the text "
               "of the slides (titles, bullets, formulas), in slide order. They state ideas "
               "without explaining them.")

# The TASK of each request of the gap search (`TextPipeline.run_coverage`).
SLIDE_TASK = ("Only the slides above are asked for: other requests over the same lecture list "
              "the other slides and the gaps of the transcript. List the ideas of each of these "
              "slides (a title or section page gets one idea, with the status the transcript "
              "gives that section) and a gap of kind \"slide_missing\" for each idea that is "
              "partial or missing. No other gap is listed.")
PART_TASK = ("Only the gaps of part {part} (\"{title}\") are asked for: the gaps of kind "
             "\"undefined_term\", \"assumed_fundamental\" or \"skipped_step\" whose \"node\" is "
             "one of {nodes}. The whole lecture is shown so that you can see what is defined, "
             "taught or derived anywhere in it; the other parts and the slides are listed by "
             "other requests over the same lecture. List a term at the node of this part where "
             "it is first used. \"slide_ideas\" stays empty.")


class PipelineError(RuntimeError):
    pass


@dataclass
class Prompts:
    outline: str
    rewrite: str
    verify: str
    spoken: str
    coverage: str
    termbase: str
    research: str
    terms: str
    course: str
    course_check: str
    cold_read: str
    ideas: str
    recitation: str
    project: str


def load_prompts(language: str, prompts_dir: str | Path | None = None) -> Prompts:
    base = Path(prompts_dir) if prompts_dir else PROMPT_DIR
    name = LANGUAGE_NAMES.get(language, language)

    def read(stem: str) -> str:
        path = base / f"{stem}.md"
        if not path.exists():
            path = PROMPT_DIR / f"{stem}.md"
        return path.read_text(encoding="utf-8").replace("{{language}}", name)

    return Prompts(*(read(s) for s in ("outline", "rewrite", "verify", "spoken", "coverage",
                                        "termbase", "research", "terms", "course",
                                        "course_check", "cold_read", "ideas", "recitation",
                                        "project")))


# ------------------------------------------------------------------ code gates

def _check_items(items, expected: list[str], where: str) -> list[str]:
    if not expected:
        return []
    if not isinstance(items, list) or any(
            not isinstance(i, dict) or not str(i.get("phrase", "")).strip() for i in items):
        return [f'{where} must be a list of {{"node": "...", "phrase": "..."}}']
    given = [i.get("node") for i in items]
    if given != expected:
        return [f"{where} must name exactly these nodes, in this order: {expected} "
                f"(you gave {given})"]
    return []


def validate_part(data, shape: dict) -> list[str]:
    """Deterministic gate on a rewrite: the structure must match the outline exactly."""
    if not isinstance(data, dict):
        return ["the answer must be a JSON object"]
    errors = []
    preview = data.get("preview")
    if not isinstance(preview, dict) or not str(preview.get("intro", "")).strip():
        errors.append('"preview" must be {"intro": "...", "items": [...]}')
    else:
        errors += _check_items(preview.get("items"), shape["preview_items"], '"preview.items"')
    sections = data.get("sections")
    if not isinstance(sections, list):
        errors.append('"sections" must be a list')
        sections = []
    given = [s.get("node") if isinstance(s, dict) else None for s in sections]
    if given != shape["sections"]:
        errors.append(f'"sections" must have exactly these nodes, in this order: '
                      f"{shape['sections']} (you gave {given})")
    for section in sections:
        if not isinstance(section, dict):
            continue
        node = section.get("node")
        sentences = section.get("sentences")
        if not isinstance(sentences, list) or not sentences:
            errors.append(f"section {node} needs a non-empty \"sentences\" list")
            continue
        for k, item in enumerate(sentences, 1):
            if (not isinstance(item, dict) or item.get("origin") not in ORIGINS
                    or not str(item.get("text", "")).strip()):
                errors.append(f'section {node}, sentence {k} must be '
                              '{"origin": "lecture", "added" or "external", "text": "..."}')
            elif item["origin"] == "external" and item.get("source") not in shape.get("sources", []):
                errors.append(f'section {node}, sentence {k} is "external": its "source" must be '
                              f'the id of a supplement, one of {shape.get("sources", [])}')
            elif "ideas" in item and (not isinstance(item["ideas"], list) or any(
                    str(i) not in shape.get("ideas", []) for i in item["ideas"])):
                errors.append(f'section {node}, sentence {k}: "ideas" lists ids of IDEAS OF THIS '
                              f'PART only, one of {shape.get("ideas", [])}, or is empty')
        if node in shape["section_previews"]:
            errors += _check_items(section.get("preview"), shape["section_previews"][node],
                                   f'"preview" of section {node}')
    recap = data.get("recap")
    if not isinstance(recap, list) or not 1 <= len(recap) <= 4 or any(
            not str(r).strip() for r in recap):
        errors.append('"recap" must be a list of one to four sentences')
    questions = data.get("questions")
    if not isinstance(questions, list) or not 1 <= len(questions) <= 4 or any(
            not isinstance(q, dict) or not str(q.get("question", "")).strip()
            or not str(q.get("answer", "")).strip() for q in questions):
        errors.append('"questions" must be a list of one to four {"question", "answer"}')
    if not isinstance(data.get("terms_defined", []), list):
        errors.append('"terms_defined" must be a list')
    reference = data.get("reference", [])
    if not isinstance(reference, list) or any(
            not isinstance(r, dict) or "label" not in r or "content" not in r for r in reference):
        errors.append('"reference" must be a list of {"label", "content"}')
    return errors


IDEA_STATUSES = ("taught", "missing")


def validate_ideas(data, idea_ids) -> list[str]:
    """One status, taught or missing, for every idea of IDEAS (the ledger's checklist)."""
    if not idea_ids:
        return []
    items = data.get("ideas") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return ['give "ideas": [{"idea": id, "status": "taught" or "missing", "detail": "..."}] '
                f"for every idea of IDEAS: {list(idea_ids)}"]
    given = {str(i.get("idea")): i.get("status") for i in items if isinstance(i, dict)}
    absent = [i for i in idea_ids if given.get(str(i)) not in IDEA_STATUSES]
    if absent:
        return ['give {"idea": id, "status": "taught" or "missing"} for every idea of IDEAS; '
                f"missing {absent}"]
    return []


def validate_verify(data, node_ids: list[str], idea_ids=()) -> list[str]:
    """A verdict without one status per node is a superficial check: ask again."""
    if not isinstance(data, dict) or not isinstance(data.get("points"), list) \
            or not isinstance(data.get("issues"), list):
        return ['the answer must be a JSON object with a "points" list and an "issues" list']
    given = [p.get("node") if isinstance(p, dict) else None for p in data["points"]]
    if sorted(map(str, given)) != sorted(node_ids):
        return [f'"points" must have exactly one entry for each of these nodes: {node_ids} '
                f"(you gave {given})"]
    for point in data["points"]:
        if point.get("status") not in STATUSES:
            return [f'the status of node {point.get("node")} must be one of {sorted(STATUSES)}']
    for k, item in enumerate(data["issues"], 1):
        if not isinstance(item, dict) or not item.get("type") or not item.get("detail"):
            return [f'issue {k} must have "type" and "detail"']
    return validate_ideas(data, idea_ids)


def validate_spoken(data, count: int) -> list[str]:
    blocks = data.get("blocks") if isinstance(data, dict) else None
    if not isinstance(blocks, list) or len(blocks) != count or any(
            not isinstance(b, str) for b in blocks):
        return [f'the answer must be {{"blocks": [...]}} with exactly {count} texts']
    return []


# ----------------------------------------------------- deterministic number check

_NUMBER = re.compile(r"(?<![\w.,])\d+(?:,\d{3})*(?:\.\d+)?(?![\w])")


def _canon(token: str) -> str:
    return token.replace(",", "")


class NumberIndex:
    """The numbers a text holds, as digits or as words, for repeated look-ups."""

    def __init__(self, text: str, language: str):
        self.digits = {_canon(t) for t in _NUMBER.findall(text)}
        self.words = re.sub(r"[\s\-]+", " ", text.lower())
        self.language = language

    def __contains__(self, token: str) -> bool:
        canon = _canon(token)
        if canon in self.digits:
            return True
        if canon.isdigit() and len(canon) <= 12:
            words = re.sub(r"[\s\-]+", " ", number_to_words(int(canon), self.language).lower())
            return words in self.words
        return False

    def absent(self, text: str) -> list[str]:
        """The numbers of `text` that this text does not hold."""
        return [t for t in _NUMBER.findall(text) if t not in self]


def unsupported_numbers(script: list[dict], source: str, slides: str, language: str) -> list[dict]:
    """Numbers in sentences tagged 'lecture' that appear in neither source (as digits or words)."""
    known = NumberIndex(source + " " + slides, language)
    issues = []
    for index, sentence in enumerate(script, 1):
        if sentence["origin"] != "lecture":
            continue
        for token in known.absent(sentence["text"]):
            issues.append({
                "type": "unsupported", "sentence": index, "unit": None,
                "detail": (f"The number {token} appears in neither the transcript nor the slides. "
                           'If it is a result you computed, tag the sentence "added".')})
    return issues


# An exponent or an index ("n^2", "10^6", "x_1") is usually said in words ("n squared", "a
# million"), so it is not looked for.
_SCRIPTED = re.compile(r"[\^_]\s*\{?\s*$")


def idea_citations(script: list[dict], ideas: list[dict]) -> list[dict]:
    """The two invariants of a claim-anchored script (Guan, 2026). Coverage: every idea of the
    part is cited by at least one "lecture" sentence, else a blocking "idea_uncited". Ledger
    completeness: a "lecture" sentence that cites no idea states something the idea list may
    have missed, a soft "unledgered" that report.md lists."""
    if not ideas:
        return []
    cited = {str(i) for s in script if s["origin"] == "lecture" for i in s.get("ideas") or []}
    issues = []
    for idea in ideas:
        if str(idea["id"]) not in cited:
            issues.append({"type": "idea_uncited", "node": str(idea["nodes"][0]), "sentence": None,
                           "unit": (idea.get("units") or [None])[0],
                           "detail": (f'No "lecture" sentence cites the idea {idea["id"]} '
                                      f'({idea["kind"]}: {idea["idea"]}). Teach it in its node '
                                      'and list its id in the sentence\'s "ideas".')})
    for k, sentence in enumerate(script, 1):
        if sentence["origin"] == "lecture" and not sentence.get("ideas"):
            issues.append({"type": "unledgered", "node": None, "sentence": k, "unit": None,
                           "detail": ("This lecture sentence cites no idea of the ledger: "
                                      f'"{sentence["text"][:90]}"')})
    return issues


def _and(items: list[str]) -> str:
    """"64", "64 and 5", "64, 5 and 9"."""
    items = [str(i) for i in items]
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def numbers_lost(statement: str, source: NumberIndex, text: NumberIndex) -> list[str]:
    """Numbers of an idea that its source holds and the text that says it teaches the idea
    does not. A number the source lacks (an extractor's slip) is not held against the text."""
    lost = []
    for match in _NUMBER.finditer(statement):
        token, before = match.group(0), statement[:match.start()]
        after = statement[match.end():match.end() + 1]
        if _SCRIPTED.search(before) or after == "^":
            continue
        if token in source and token not in text and token not in lost:
            lost.append(token)
    return lost


def external_numbers(script: list[dict], supplements: dict[str, str], language: str) -> list[dict]:
    """Numbers in 'external' sentences that their supplement does not contain."""
    issues = []
    for index, sentence in enumerate(script, 1):
        if sentence["origin"] != "external":
            continue
        known = NumberIndex(supplements.get(sentence.get("source"), ""), language)
        for number in known.absent(sentence["text"]):
            issues.append(dict(type="unsupported", sentence=index, unit=None, detail=(
                f"The number {number} is not in supplement {sentence.get('source')}. "
                "Use the supplement's own numbers.")))
    return issues


# A fact planted in a copy of one part to test the idea listing (audit_ideas). It is written to
# be unmistakable: an extractor that lists everything lists it.
PLANTED_IDEA = {
    "en": "For the exam, remember that the reference threshold used in this course is exactly "
          "7919 units, and that this threshold never changes.",
    "fr": "Pour l'examen, retenez que le seuil de référence utilisé dans ce cours vaut "
          "exactement 7919 unités, et que ce seuil ne change jamais.",
}
PLANTED_NUMBER = "7919"


# --------------------------------------------------------- spoken structure

WORDING = {
    "en": {"part": "Part {n}: {title}.", "points": "This part has {count} points.",
           "one_point": "This part has one point: {phrase}.",
           "subpoints": "This point has {count} sub-points.",
           "summary": "Summary of part {n}.",
           "quiz": "Questions on part {n}. Try to answer each one before you hear the answer.",
           "question": "Question {n}: {text}", "answer": "Answer: {text}",
           "chapter_recap": "Summary and questions", "chapter_summary": "Summary"},
    "fr": {"part": "Partie {n} : {title}.", "points": "Cette partie comporte {count} points.",
           "one_point": "Cette partie comporte un seul point : {phrase}.",
           "subpoints": "Ce point comporte {count} sous-points.",
           "summary": "Résumé de la partie {n}.",
           "quiz": ("Questions sur la partie {n}. Essayez de répondre à chacune avant "
                    "d'entendre la réponse."),
           "question": "Question {n} : {text}", "answer": "Réponse : {text}",
           "chapter_recap": "Résumé et questions", "chapter_summary": "Résumé"},
}


def _words_for(language: str):
    return WORDING["fr" if language == "fr" else "en"]


def _count(n: int, language: str) -> str:
    return fr_cardinal(n) if language == "fr" else en_cardinal(n)


def _adverb(n: int, language: str) -> str:
    if language == "fr":
        return "Premièrement" if n == 1 else fr_ordinal(n).capitalize() + "ment"
    return en_ordinal(n).capitalize()


def _phrase(text: str) -> str:
    return str(text).strip().rstrip(".;:,")


def list_sentence(items: list[dict], language: str, kind: str) -> str:
    """'This part has three points. First, a. Second, b. Third, c.' (kind: points|subpoints)."""
    words = _words_for(language)
    phrases = [_phrase(i["phrase"]) for i in items]
    if not phrases:
        return ""
    if len(phrases) == 1 and kind == "points":
        return words["one_point"].format(phrase=phrases[0])
    head = words[kind].format(count=_count(len(phrases), language))
    return " ".join([head] + [f"{_adverb(k, language)}, {p}." for k, p in enumerate(phrases, 1)])


def sentences_of(record: dict) -> list[dict]:
    return [s for section in record["sections"] for s in section["sentences"]]


def chapter_of(node_id: str) -> str:
    """Chapters are parts and points; a sub-point belongs to its point's chapter."""
    return ".".join(node_id.split(".")[:2])


def part_blocks(part: Node, record: dict, language: str) -> list[dict]:
    """The part as the listener hears it, in blocks that keep their chapter and role."""
    words = _words_for(language)
    number = _count(int(part.id), language)
    preview = record["preview"]
    blocks = [{"chapter": part.id, "role": "title",
               "text": words["part"].format(n=number, title=part.title)},
              {"chapter": part.id, "role": "preview",
               "text": " ".join(t for t in (preview["intro"].strip(),
                                            list_sentence(preview.get("items") or [], language,
                                                          "points")) if t)}]
    for section in record["sections"]:
        texts = [s["text"].strip() for s in section["sentences"]]
        if section.get("preview"):
            texts.insert(1, list_sentence(section["preview"], language, "subpoints"))
        blocks.append({"chapter": chapter_of(section["node"]), "role": "section",
                       "text": " ".join(texts)})
    recap_key = f"{part.id}.recap"
    blocks.append({"chapter": recap_key, "role": "recap",
                   "text": " ".join([words["summary"].format(n=number)]
                                    + [r.strip() for r in record["recap"]])})
    blocks.append({"chapter": recap_key, "role": "quiz_intro",
                   "text": words["quiz"].format(n=number)})
    for k, item in enumerate(record["questions"], 1):
        blocks.append({"chapter": recap_key, "role": "question",
                       "text": words["question"].format(n=_count(k, language),
                                                        text=item["question"].strip())})
        blocks.append({"chapter": recap_key, "role": "answer",
                       "text": words["answer"].format(text=item["answer"].strip())})
    return blocks


def chapter_titles(outline: Outline, language: str, quiz: bool = True) -> dict[str, str]:
    words = _words_for(language)
    titles = {}
    for part in outline.parts:
        titles[part.id] = f"{part.id} {part.title}"
        for child in part.children:
            titles[child.id] = f"{child.id} {child.title}"
        label = words["chapter_recap" if quiz else "chapter_summary"]
        titles[f"{part.id}.recap"] = f"{part.id} {label}"
    return titles


# ------------------------------------------------------------------- formatting

def _section(title: str, body: str) -> str:
    return f"## {title}\n{body.strip()}\n\n" if body and body.strip() else ""


def render_script(outline: Outline, part: Node, record: dict, language: str = "en") -> str:
    """The script with numbered sentences, for the verifier and for feedback."""
    lines = ["PREVIEW: " + record["preview"]["intro"].strip() + " "
             + list_sentence(record["preview"].get("items") or [], language, "points"), ""]
    number = 0
    for section in record["sections"]:
        node = outline.node(section["node"])
        lines.append("#" * (node.level + 2) + f" {node.id} {node.title}")
        if section.get("preview"):
            lines.append("(sub-point preview: "
                         + list_sentence(section["preview"], language, "subpoints") + ")")
        for sentence in section["sentences"]:
            number += 1
            tag = sentence["origin"] + (f" {sentence.get('source')}"
                                        if sentence["origin"] == "external" else "")
            lines.append(f"{number}. [{tag}] {sentence['text']}")
        lines.append("")
    lines.append("RECAP: " + " ".join(record["recap"]))
    lines.append("QUESTIONS:")
    for k, item in enumerate(record["questions"], 1):
        lines.append(f"Q{k}. {item['question']} / Answer: {item['answer']}")
    return "\n".join(lines)


def format_feedback(script_text: str, issues: list[dict], scope: list[str] | None = None) -> str:
    """The feedback for a repair. With `scope` (see repair_plan), only the sections and fields
    named are rewritten: code keeps the rest of the earlier attempt word for word."""
    if scope:
        head = [f"A reviewer found problems in an earlier attempt, only in {scope_words(scope)}. "
                f"Rewrite only {'that' if len(scope) == 1 else 'those'}, fix every problem "
                "listed, and keep what was correct in them. Everything else passed the review: "
                "the program keeps it word for word, so give it back unchanged; any change to it "
                "is discarded."]
    else:
        head = ["A reviewer found problems in an earlier attempt. Fix every one and keep "
                "everything that was correct."]
    lines = head + ["", "Earlier attempt:", script_text, "", "Problems:"]
    for issue in issues:
        where = []
        if issue.get("node"):
            where.append(f"node {issue['node']}")
        if issue.get("sentence"):
            where.append(f"sentence {issue['sentence']}")
        if issue.get("unit"):
            where.append(f"unit {issue['unit']}")
        lines.append(f"- ({issue['type']}, {', '.join(where) or 'general'}) {issue['detail']}")
    return "\n".join(lines)


# Fields of a part outside its sections that an issue can point at, with the word used in the
# feedback: the recap and the questions of a script, the exercises of the course material. The
# preview is assembled by code from the sections' phrases and is never repaired alone.
PART_FIELDS = {"recap": "the recap", "questions": "the questions", "exercises": "the exercises"}
_FIELD_OF_ISSUE = {"recap": "recap", "question": "questions"}


def scope_words(scope: list[str]) -> str:
    """["1.2", "1.3", "recap"] -> "sections 1.2 and 1.3 and the recap"."""
    sections = [s for s in scope if s not in PART_FIELDS]
    named = [("section " if len(sections) == 1 else "sections ") + _and(sections)] if sections else []
    fields = [PART_FIELDS[s] for s in scope if s in PART_FIELDS]
    return " and ".join(named + ([_and(fields)] if fields else []))


def _locate(issue: dict, record: dict, part: Node) -> str | None:
    """Where an issue lies: the node of its section, a part field, or None when it cannot be
    placed (a term the part never defines, a remark on the whole part)."""
    if issue["type"] in _FIELD_OF_ISSUE:
        return _FIELD_OF_ISSUE[issue["type"]]
    sections = [s["node"] for s in record["sections"]]
    node = issue.get("node")
    if not node and issue.get("sentence"):
        owner = [s["node"] for s in record["sections"] for _ in s["sentences"]]
        k = issue["sentence"]
        node = owner[k - 1] if isinstance(k, int) and 1 <= k <= len(owner) else None
    if not node and issue.get("unit"):
        node = next((n.id for n in part.walk() if issue["unit"] in n.own_units()), None)
    return str(node) if node and str(node) in sections else None


def repair_plan(part: Node, record: dict, issues: list[dict]) -> tuple[list[str] | None,
                                                                      list[dict]]:
    """What a repair rewrites, and the issues its feedback shows.

    A repair that rewrites the whole part can damage sections that passed, in ways the count
    of blocking problems does not show (Zhang, 2026); a repair pointed at the faulty span and
    bounded to it recovers more and damages less (Guo et al., 2026). So when every blocking
    issue can be placed in a section (or in the recap or the questions), only those are
    rewritten and code keeps the rest word for word (`splice_repair`). The scope is None, a
    full rewrite, when an issue cannot be placed or every section is at fault."""
    scope: list[str] = []
    for issue in blocking(issues):
        where = _locate(issue, record, part)
        if where is None:
            return None, issues
        if where not in scope:
            scope.append(where)
    sections = [s["node"] for s in record["sections"]]
    if not scope or set(sections) <= set(scope):
        return None, issues
    scope = [s for s in sections if s in scope] + [f for f in ("recap", "questions")
                                                   if f in scope]
    return scope, [i for i in issues if _locate(i, record, part) in scope]


def splice_repair(best: dict, new: dict, scope: list[str]) -> dict:
    """The best attempt with the sections and fields in `scope` taken from the repair. Every
    other section, the preview and the other fields stay as they were, so a bounded repair
    cannot change what passed. The terms defined and the reference entries are merged, since a
    kept section still defines its terms and still needs its formulas."""
    rewritten = {s["node"]: s for s in new["sections"]}
    out = copy.deepcopy(best)
    out["sections"] = [copy.deepcopy(rewritten[s["node"]]) if s["node"] in scope else s
                       for s in out["sections"]]
    for field in ("recap", "questions"):
        if field in scope:
            out[field] = copy.deepcopy(new[field])
    out["terms_defined"] = list(dict.fromkeys(list(best.get("terms_defined", []))
                                              + list(new.get("terms_defined", []))))
    labels = {r["label"] for r in best.get("reference", [])}
    out["reference"] = list(best.get("reference", [])) + [
        r for r in new.get("reference", []) if r["label"] not in labels]
    return out


def blocking(issues: list[dict]) -> list[dict]:
    return [i for i in issues if i["type"] not in SOFT_ISSUES]


# --------------------------------------------------------------------- pipeline

# Slides with text per request of the gap search. One request over a whole deck wrote about
# 100 s of output and failed whole when a few slides were left out (ADR-0006).
SLIDES_PER_CHUNK = 12


def _gap_key(gap: dict) -> tuple:
    """Two gaps with the same key are the same gap. A term is researched and defined once,
    wherever it is first missed, so a term gap is keyed by its kind and term alone; any other
    gap by its node, kind, term and detail."""
    term = " ".join(str(gap.get("term", "")).lower().split())
    if gap.get("kind") in enrich.TERM_GAPS and term:
        return (gap.get("kind"), term)
    return (str(gap.get("node")), gap.get("kind"), term,
            " ".join(str(gap.get("detail", "")).lower().split()))


def _merge_coverage(result: dict, extra: dict) -> dict:
    """`result` with the slide ideas and gaps of `extra` appended under fresh ids (after the
    ids already used, so that the ids of `result`, which the research cache is keyed by, never
    change). A gap already in `result` (`_gap_key`) is not added twice, and a "slide_missing"
    gap whose idea is not in `extra` is dropped."""
    def top(items, prefix):
        numbers = [int(m.group(1)) for i in items
                   if (m := re.fullmatch(prefix + r"(\d+)", str(i.get("id", ""))))]
        return max(numbers, default=0)

    next_idea, next_gap = top(result["slide_ideas"], "S"), top(result["gaps"], "G")
    renamed, ideas, gaps = {}, [], []
    seen = {_gap_key(g) for g in result["gaps"]}
    for idea in extra["slide_ideas"]:
        next_idea += 1
        renamed[str(idea["id"])] = f"S{next_idea}"
        ideas.append(dict(idea, id=f"S{next_idea}"))
    for gap in extra["gaps"]:
        if gap.get("kind") == "slide_missing":
            if str(gap.get("idea")) not in renamed:
                continue
            gap = dict(gap, idea=renamed[str(gap["idea"])])
        if _gap_key(gap) in seen:
            continue
        seen.add(_gap_key(gap))
        next_gap += 1
        gaps.append(dict(gap, id=f"G{next_gap}"))
    return dict(result, slide_ideas=result["slide_ideas"] + ideas, gaps=result["gaps"] + gaps,
                transcript_ideas=result.get("transcript_ideas", [])
                + extra.get("transcript_ideas", []))


def _merge_slide_ideas(result: dict, extra: dict, slides: set[int]) -> dict:
    """`result` with the slide ideas of `slides` from `extra` and the gaps that name them,
    renumbered after the ids already used."""
    ideas = [i for i in extra["slide_ideas"] if i.get("slide") in slides]
    return _merge_coverage(result, {"slide_ideas": ideas, "gaps": [
        g for g in extra["gaps"] if g.get("kind") == "slide_missing"]})


def _in_lecture_order(result: dict, node_ids: list[str]) -> dict:
    """Ideas by slide and gaps by node, renumbered S1, S2, ... and G1, G2, ... (for a fresh
    search only: renumbering a saved one would detach the research cached under its ids)."""
    rank = {n: k for k, n in enumerate(node_ids)}
    ideas = sorted(result["slide_ideas"], key=lambda i: i["slide"])
    renamed = {str(i["id"]): f"S{k}" for k, i in enumerate(ideas, 1)}
    gaps = sorted(result["gaps"], key=lambda g: rank.get(str(g["node"]), len(rank)))
    return dict(result,
                slide_ideas=[dict(i, id=renamed[str(i["id"])]) for i in ideas],
                gaps=[dict(g, id=f"G{k}", **({"idea": renamed[str(g["idea"])]}
                                             if g.get("kind") == "slide_missing" else {}))
                      for k, g in enumerate(gaps, 1)])


def _scope_chunk(data, chunk: dict, part_nodes: set[str]):
    """Only what a request of the gap search was asked for: the ideas of its slides and their
    "slide_missing" gaps, or the other gaps of its part. Anything else is dropped (the request
    that owns it lists it, over the same transcript) rather than rejected, because a rejection
    costs a whole new answer."""
    if not isinstance(data, dict) or not isinstance(data.get("slide_ideas"), list) \
            or not isinstance(data.get("gaps"), list):
        return data
    if chunk["kind"] == "slides":
        wanted = set(chunk["slides"])
        ideas = [i for i in data["slide_ideas"]
                 if not isinstance(i, dict) or i.get("slide") in wanted]
        kept = {str(i.get("id")) for i in ideas if isinstance(i, dict)}
        gaps = [g for g in data["gaps"] if not isinstance(g, dict)
                or (g.get("kind") == "slide_missing" and str(g.get("idea")) in kept)]
    else:
        ideas = []
        gaps = [g for g in data["gaps"] if not isinstance(g, dict)
                or (g.get("kind") != "slide_missing" and str(g.get("node")) in part_nodes)]
    return dict(data, slide_ideas=ideas, gaps=gaps, transcript_ideas=[])


def _chunk_label(chunk: dict) -> str:
    if chunk["kind"] == "slides":
        first, last = chunk["slides"][0], chunk["slides"][-1]
        return f"slides {first}-{last}" if last != first else f"slide {first}"
    return f"part {chunk['part']}"


class TextPipeline:
    def __init__(self, work_dir: Path, language: str, rewrite_llm: LLM, verify_llm: LLM,
                 prompts: Prompts, glossary: str = "", slides: list[str] | None = None,
                 max_retries: int = 2, log: Callable[[str], None] = print,
                 max_part_words: int = 1500, workers: int = 3,
                 researcher: Callable[[str, str], tuple[str, list[dict]]] | None = None,
                 source_kind: str = "transcript", library=None):
        self.work_dir = Path(work_dir)
        self.language = language
        self.language_name = LANGUAGE_NAMES.get(language, language)
        self.rewrite_llm = rewrite_llm
        self.verify_llm = verify_llm
        self.prompts = prompts
        self.glossary = glossary
        self.slides = slides or []
        self.max_retries = max_retries
        self.log = log
        self.max_part_words = max_part_words
        self.workers = max(1, workers)
        # researcher(system, user) -> (answer, documents read); None: gaps are only listed.
        self.researcher = researcher
        # "slides": no recording and no transcript, the units are the text of the slides
        self.source_kind = source_kind
        # The user's reference library (library.Library with books), searched before the web.
        self.library = library
        # Filled by the gap stages; empty when they are not run.
        self.coverage: dict = {"slide_ideas": [], "gaps": [], "transcript_ideas": []}
        self.terms: list[dict] = []
        self.terms_error: str | None = None
        self.idea_notes: dict = {"no_content": [], "unaccounted": {}}
        self.research: dict[str, dict] = {}
        self.supplements: list[dict] = []
        # The idea list of each part, as each finishes: parts may finish in any order and
        # while the gap search runs (flow.py), so the lecture's list is rebuilt in part order.
        self._idea_results: dict[str, dict] = {}
        self._ideas_lock = threading.RLock()

    def _dir(self, name: str) -> Path:
        directory = self.work_dir / name
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _kind(self) -> str:
        """The SOURCE KIND section of a request: only said when the units are not speech."""
        return _section("SOURCE KIND", SLIDES_ONLY if self.source_kind == "slides" else "")

    def _ask_json(self, llm: LLM, system: str, user: str, validate, what: str,
                  tries: int = 3) -> dict:
        note, best = "", None
        # The trace is told which prompt this is and which model seat sends it; it observes.
        prompt = next((name for name, text in getattr(self.prompts, "__dict__", {}).items()
                       if text == system), None)
        seat = "checker" if llm is self.verify_llm and llm is not self.rewrite_llm else "writer"
        for attempt in range(tries):
            with trace.context(prompt=prompt, role=trace.ROLES.get(prompt), seat=seat,
                               **{"try": attempt + 1 if attempt else None}):
                raw = llm.complete(system, user + note)
                try:
                    data = extract_json(raw)
                    errors = validate(data)
                except ValueError as exc:
                    errors = [f"not valid JSON ({exc})"]
                trace.note("gate", parent=trace.last_id(), what=what, ok=not errors,
                           errors=errors[:12])
            if not errors:
                return data
            if best is None or len(errors) < len(best):  # feedback from the best attempt
                best = errors
            note = ("\n\n## PREVIOUS ANSWER REJECTED\nYour previous answer was rejected:\n- "
                    + "\n- ".join(best)
                    + "\nReturn only the JSON object, in exactly the required format.")
        problem = "; ".join(best or [])
        raise PipelineError(f"{what}: no valid answer after {tries} tries ({problem}).")

    def _map(self, function, items, on_done: Callable[[object], None] | None = None):
        """function(item) for every item, in parallel, results in order. `on_done(result)` is
        called as each item finishes (from its worker thread)."""
        def one(item):
            with trace.context(**trace.item_fields(item)):  # the part the calls serve
                result = function(item)
            if on_done:
                on_done(result)
            return result

        # each item runs in its own copy of the caller's context (trace fields), taken here,
        # in the calling thread, so that parallel parts never share or mix theirs
        contexts = [contextvars.copy_context() for _ in items]
        if self.workers == 1 or len(items) < 2:
            return [ctx.run(one, item) for ctx, item in zip(contexts, items)]
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            return list(pool.map(lambda pair: pair[0].run(one, pair[1]), zip(contexts, items)))

    # -- stage 1: outline ----------------------------------------------------------
    def run_outline(self, units: list[str]) -> Outline:
        cache = self.work_dir / "outline.json"
        if cache.exists():
            return Outline.from_dict(json.loads(cache.read_text(encoding="utf-8")))
        self.log(f"[outline] {len(units)} units")
        slides_text = "\n\n".join(s for s in self.slides if s.strip())[:12000]
        user = (f"## LANGUAGE\n{self.language_name}\n\n"
                f"## LIMITS\nAt most {self.max_part_words} words per part.\n\n"
                + self._kind() + _section("GLOSSARY", self.glossary) + _section("SLIDES", slides_text)
                + "## UNITS\n" + "\n".join(f"[{k}] {u}" for k, u in enumerate(units, 1)))
        holder = {}

        def check(data):
            outline, errors = parse_outline(data, units, self.max_part_words)
            holder["outline"] = outline
            return errors

        try:
            self._ask_json(self.rewrite_llm, self.prompts.outline, user, check, "outline")
            outline = holder["outline"]
        except PipelineError as exc:
            self.log(f"[outline] the model's outline kept failing the checks; using parts cut by "
                     f"size instead ({exc})")
            outline = fallback_outline(units, self.max_part_words, str(exc))
        cache.write_text(dump(outline), encoding="utf-8")
        return outline

    # -- gaps and terminology (before the rewrite) -----------------------------------
    def _lecture(self, outline: Outline) -> str:
        return "\n\n".join(render_part_transcript(outline, p, []) for p in outline.parts)

    def _blank_slides(self) -> set[int]:
        """Slide numbers with no text, which the gap search is not shown."""
        return {k for k, s in enumerate(self.slides, 1) if not s.strip()}

    def _slides_numbered(self) -> str:
        return "\n\n".join(f"[Slide {k}]\n{s}" for k, s in enumerate(self.slides, 1) if s.strip())

    def run_coverage(self, outline: Outline) -> dict:
        """Slide ideas with their status, and the gaps of the transcript, as a map-reduce.

        Map: one request per range of SLIDES_PER_CHUNK slides with text (their ideas and
        "slide_missing" gaps) and one per part (the other gaps whose node is in that part), run
        in parallel. Every request sees the whole transcript, because a slide idea can be taught
        anywhere and a term used in part 2 may be defined in part 5; only what it must list is
        narrowed, so each answer is short. Reduce, in code: the answers are merged under fresh
        ids, a gap found twice is kept once (`_gap_key`), and the merged result passes the same
        gate as one whole answer.

        Slides an answer leaves without an idea (often section or title pages) are asked for
        alone in one bounded call; any still missing are named in report.md
        (`unlisted_slides`). A request that keeps failing never drops the others: it is named in
        report.md (`failed_chunks`) and asked again, alone, on the next run. A search whose
        every request fails is not cached, so the next run tries it again."""
        cache = self.work_dir / "coverage.json"
        base, chunks = None, None
        if cache.exists():
            saved = json.loads(cache.read_text(encoding="utf-8"))
            if not saved.get("error"):  # a failure saved by an older version: retry
                if not saved.get("failed_chunks"):
                    self._set_coverage(saved)
                    return saved
                base = {k: v for k, v in saved.items()
                        if k not in ("failed_chunks", "unlisted_slides")}
                chunks = [{k: v for k, v in f.items() if k in ("kind", "slides", "part")}
                          for f in saved["failed_chunks"]]
        ids, blank, count = enrich.node_order(outline), self._blank_slides(), len(self.slides)
        if chunks is None:
            chunks = self._coverage_chunks(outline)
        ranges_ = sum(c["kind"] == "slides" for c in chunks)
        self.log(f"[gaps] looking for what the lecture leaves out: {ranges_} slide range(s) and "
                 f"{len(chunks) - ranges_} part(s), in parallel")
        results = self._map(lambda c: self._run_chunk(outline, ids, c), chunks)
        failed = [r for r in results if r["error"]]
        if base is None and failed and len(failed) == len(results):
            error = "; ".join(f"{_chunk_label(r['chunk'])}: {r['error']}" for r in failed)
            self.log(f"[gaps] every request failed (not cached: the next run tries again)")
            self._set_coverage({"slide_ideas": [], "gaps": [], "transcript_ideas": [],
                                "error": error})
            return self.coverage
        result = base or {"slide_ideas": [], "gaps": [], "transcript_ideas": []}
        for r in results:
            result = _merge_coverage(result, r["data"])
        if base is None:
            result = _in_lecture_order(result, ids)
        errors = enrich.validate_coverage(result, ids, count, blank, require_slides=False)
        if errors:  # each answer passed the gate; the merge should keep it so
            self.log(f"[gaps] the merged answers fail the gate: {'; '.join(errors)}")
        missing = enrich.unlisted_slides(result, count, blank) \
            if self.source_kind != "slides" else []
        if missing:
            result["unlisted_slides"] = missing
            self.log(f"[gaps] slides {missing} still have no idea listed; named in report.md")
        if failed:
            result["failed_chunks"] = [dict(r["chunk"], label=_chunk_label(r["chunk"]),
                                            error=r["error"]) for r in failed]
            self.log(f"[gaps] {', '.join(_chunk_label(r['chunk']) for r in failed)} kept "
                     "failing; named in report.md and asked again on the next run")
        cache.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        self._set_coverage(result)
        return result

    def _set_coverage(self, coverage: dict) -> None:
        """The result of the gap search, with the idea lists of the parts that are done (the
        idea stage's lists replace the gap search's own, as they always did)."""
        with self._ideas_lock:
            if self._idea_results:
                self._publish_ideas(coverage)
            self.coverage = coverage

    def _coverage_chunks(self, outline: Outline) -> list[dict]:
        """The requests of the gap search: the slides with text in ranges of
        SLIDES_PER_CHUNK, then one request per part. When the lecture is only slides, the
        slides are the transcript and no slide idea is asked for."""
        chunks = []
        if self.source_kind != "slides":
            shown = [k for k, s in enumerate(self.slides, 1) if s.strip()]
            chunks = [{"kind": "slides", "slides": shown[k:k + SLIDES_PER_CHUNK]}
                      for k in range(0, len(shown), SLIDES_PER_CHUNK)]
        return chunks + [{"kind": "part", "part": p.id} for p in outline.parts]

    def _coverage_request(self, lecture: Outline, slides: list[int], task: str) -> str:
        """The transcript comes before what differs between the requests of one search, so
        that they share their beginning (and the provider's prefix cache)."""
        wanted = set(slides)
        shown = "\n\n".join(f"[Slide {k}]\n{s}" for k, s in enumerate(self.slides, 1)
                            if k in wanted)
        return (f"## LANGUAGE\n{self.language_name}\n\n" + self._kind()
                + _section("GLOSSARY", self.glossary)
                + "## TRANSCRIPT\n" + self._lecture(lecture) + "\n\n"
                + _section("SLIDES", shown) + _section("TASK", task))

    def _ask_chunk(self, lecture: Outline, chunk: dict, ids: list[str], what: str,
                   tries: int = 3) -> dict:
        """One request of the gap search, checked by the gate on what it was asked for."""
        everything = set(range(1, len(self.slides) + 1))
        if chunk["kind"] == "slides":
            nodes, hidden, task = set(), everything - set(chunk["slides"]), SLIDE_TASK
        else:
            part = lecture.node(chunk["part"])
            nodes = {n.id for n in part.walk()}
            hidden = everything
            task = PART_TASK.format(part=part.id, title=part.title,
                                    nodes=", ".join(n.id for n in part.walk()))
        user = self._coverage_request(lecture, chunk.get("slides", []), task)
        data = self._ask_json(
            self.rewrite_llm, self.prompts.coverage, user,
            lambda d: enrich.validate_coverage(_scope_chunk(d, chunk, nodes), ids,
                                               len(self.slides), hidden, require_slides=False),
            what, tries=tries)
        return _scope_chunk(data, chunk, nodes)

    def _run_chunk(self, lecture: Outline, ids: list[str], chunk: dict,
                   repair: bool = True) -> dict:
        """{"chunk", "data", "error"}: one request, and for a slide range the bounded repair of
        the slides it left without an idea. `error` is set when nothing usable came back (for
        a slide range: the request and its repair both failed)."""
        label = _chunk_label(chunk)
        empty = {"slide_ideas": [], "gaps": [], "transcript_ideas": []}
        try:
            data, error = self._ask_chunk(lecture, chunk, ids, f"gap search, {label}"), None
        except (PipelineError, LLMError) as exc:
            self.log(f"[gaps] {label}: {exc}")
            data, error = empty, str(exc)
        if chunk["kind"] == "slides" and repair:
            listed = {i["slide"] for i in data["slide_ideas"]}
            missing = [k for k in chunk["slides"] if k not in listed]
            if missing:
                data, repaired = self._repair_slides(lecture, data, missing, ids)
                error = None if repaired else error
        return {"chunk": chunk, "data": data, "error": error}

    def _repair_slides(self, lecture: Outline, data: dict, missing: list[int],
                       ids: list[str]) -> tuple[dict, bool]:
        """One bounded call for the slides of a range that its answer left without an idea:
        only those slides are shown, the transcript stays, and the answer is merged under fresh
        ids. The merged answer is kept only if it passes the gate."""
        self.log(f"[gaps] asking again for slides {missing} only")
        try:
            extra = self._ask_chunk(lecture, {"kind": "slides", "slides": missing}, ids,
                                    "gap search, missing slides", tries=2)
        except (PipelineError, LLMError) as exc:
            self.log(f"[gaps] {exc}")
            return data, False
        merged = _merge_slide_ideas(data, extra, set(missing))
        if enrich.validate_coverage(merged, ids, len(self.slides), self._blank_slides(),
                                    require_slides=False):
            return data, False
        return merged, True

    def run_ideas(self, outline: Outline) -> list[dict]:
        """The atomic ideas the teacher taught, listed part by part (in parallel), so that no
        output limit cuts the list on a long lecture. Ids are scoped to the part (T3.12 is the
        twelfth idea of part 3): redoing one part never renumbers the others.

        Every unit must be accounted for: cited by an idea, or declared as teaching nothing or as
        unclear ("no_content"), so the model can abstain instead of padding (Claimify, 2025;
        Barali and Paudel, 2026). Units left unaccounted for are asked again with feedback that
        says fewer is fine, and the attempt with the fewest is kept; any that remain are named in
        report.md. A part whose list keeps failing the gate is logged, left empty and not cached,
        so the next run tries it again."""
        self.log(f"[ideas] listing the atomic ideas of {len(outline.parts)} part(s)")
        self._map(lambda part: self.keep_ideas(part.id, self.ideas_part(outline, part)),
                  outline.parts)
        with self._ideas_lock:
            return self._publish_ideas(self.coverage)

    def keep_ideas(self, part_id: str, result: dict) -> list[dict]:
        """Add one part's idea list to the lecture's (coverage["transcript_ideas"], in part
        order) and return the lecture's list so far. Safe from several threads."""
        with self._ideas_lock:
            self._idea_results[str(part_id)] = result
            return self._publish_ideas(self.coverage)

    def _publish_ideas(self, coverage: dict) -> list[dict]:
        """Rebuild the lecture's list from the parts done so far, in part order: a new list
        each time, so a reader in another thread never sees one half updated."""
        results = [(k, self._idea_results[k]) for k in sorted(self._idea_results, key=int)]
        ideas = [i for _, r in results for i in r.get("ideas", [])]
        coverage["transcript_ideas"] = ideas
        self.idea_notes = {"no_content": [n for _, r in results for n in r.get("no_content", [])],
                           "unaccounted": {k: r.get("unaccounted", []) for k, r in results
                                           if r.get("unaccounted")}}
        return ideas

    def ideas_part(self, outline: Outline, part: Node) -> dict:
        """The idea list of one part, cached in ideas/ (see run_ideas). It needs only the
        outline, so it runs while the gap search does (flow.py)."""
        cache = self._dir("ideas") / f"part_{int(part.id):03d}.json"
        if cache.exists():
            saved = json.loads(cache.read_text(encoding="utf-8"))
            return saved if isinstance(saved, dict) else {"ideas": saved}
        if not part.all_units():
            return {"ideas": []}
        user = self._ideas_request(outline, part, render_part_transcript(outline, part, []),
                                   noise_by_part(outline)[part.id])
        best, note = None, ""
        for _ in range(self.max_retries + 1):
            try:
                data = self._ask_json(self.rewrite_llm, self.prompts.ideas, user + note,
                                      lambda d: enrich.validate_idea_list(d, part),
                                      f"ideas of part {part.id}")
            except PipelineError as exc:
                self.log(f"[ideas] part {part.id}: {exc}")
                break
            left = enrich.unaccounted_units(data, part)
            if best is None or len(left) < len(best[1]):
                best = (data, left)
            if not left:
                break
            note = ("\n\n## UNITS NOT ACCOUNTED FOR\nUnits " + ranges(left) + " are cited by no "
                    "idea and are not in no_content. List the ideas they teach. If a unit "
                    "teaches nothing, or is too garbled to interpret, list it under "
                    "no_content: fewer ideas are fine, and an invented idea is worse than none.")
        if best is None:
            return {"ideas": []}
        parsed = enrich.IdeaList.model_validate(best[0])
        result = {"ideas": [{"id": f"T{part.id}.{k}", "kind": i.kind, "idea": i.idea,
                             "nodes": i.nodes, "units": sorted(set(i.units))}
                            for k, i in enumerate(parsed.ideas, 1)],
                  "no_content": [{"part": part.id, "units": sorted(set(n.units)),
                                  "kind": n.kind, "reason": n.reason.strip()}
                                 for n in parsed.no_content],
                  "unaccounted": best[1]}
        cache.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result

    def _ideas_request(self, outline: Outline, part: Node, transcript: str, noise: list[int]) -> str:
        source = part_source(outline, part, noise)
        return (f"## LANGUAGE\n{self.language_name}\n\n"
                + _section("OUTLINE", render_outline(outline, part.id))
                + _section("GLOSSARY", self.glossary)
                + _section("SLIDES", select_slides(self.slides, source) if self.slides else "")
                + "## TRANSCRIPT OF THIS PART\n" + transcript)

    @trace.tagged(audit="audit_ideas")
    def audit_ideas(self, outline: Outline) -> dict:
        """Plant one unmistakable fact in a copy of a part and list that part's ideas again: an
        extractor that does not list it may be dropping ideas, and the ledger only proves that
        the ideas it lists arrive."""
        cache = self.work_dir / "ideas_audit.json"
        if cache.exists():
            return json.loads(cache.read_text(encoding="utf-8"))
        listed = {i["nodes"][0].split(".")[0] for i in self.coverage.get("transcript_ideas", [])}
        candidates = [n for part in outline.parts if part.id in listed
                      for n in part.walk() if n.own_units()]
        result = {"skipped": "no part has a list of ideas"}
        if candidates:
            node = max(candidates, key=lambda n: len(n.own_units()))
            part = outline.node(node.id.split(".")[0])
            planted = len(outline.units) + 1
            fact = PLANTED_IDEA.get(self.language, PLANTED_IDEA["en"])
            last = f"[{node.own_units()[-1]}] "
            lines = render_part_transcript(outline, part, []).splitlines()
            at = next(k for k, line in enumerate(lines) if line.startswith(last)) + 1
            lines.insert(at, f"[{planted}] {fact}")
            self.log(f"[audit] planting a fact in node {node.id} to test the idea listing")
            user = self._ideas_request(outline, part, "\n".join(lines),
                                       noise_by_part(outline)[part.id])
            try:
                data = self._ask_json(
                    self.rewrite_llm, self.prompts.ideas, user,
                    lambda d: enrich.validate_idea_list(d, part, extra_units={planted}),
                    "idea listing audit")
            except (PipelineError, LLMError) as exc:  # retried next run: not cached
                self.log(f"[audit] idea listing: {exc}")
                return {"skipped": f"the test failed ({exc}); it runs again next time"}
            caught = any(planted in (i.get("units") or []) or PLANTED_NUMBER in str(i["idea"])
                         for i in data["ideas"])
            result = {"part": part.id, "node": node.id, "title": node.title, "caught": caught}
        cache.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result

    @trace.tagged(audit="audit_coverage")
    def audit_coverage(self, outline: Outline) -> dict:
        """Plant a hole in the lecture and see whether the gap search notices it, because a
        search that finds nothing might simply be blind."""
        cache = self.work_dir / "coverage_audit.json"
        if cache.exists():
            return json.loads(cache.read_text(encoding="utf-8"))
        result = {"skipped": "no point with at least three units"}
        candidates = [n for part in outline.parts for n in part.walk()
                      if n.level > 1 and len(n.own_units()) >= 3]
        if candidates and not self.coverage.get("error"):
            node = max(candidates, key=lambda n: len(n.own_units()))
            unit = max(node.own_units()[1:], key=lambda u: len(outline.text(u)))
            units = list(outline.units)
            units[unit - 1] = "um ok"
            mutated = Outline(outline.title, outline.parts, outline.noise, outline.terms, units,
                              outline.fallback)
            known = {(str(g["node"]), g["kind"], str(g.get("term", "")).lower())
                     for g in self.coverage.get("gaps", [])}
            region = {node.id, node.id.rsplit(".", 1)[0]}
            self.log(f"[audit] blanking unit {unit} of node {node.id} to test the gap search")
            # The requests of the search that can name a node of the region: the part's own, and
            # the slides with an idea taught there (a blank can only make such an idea partial).
            # The others list nodes elsewhere, so running them would not change the score.
            taught = {i["slide"] for i in self.coverage.get("slide_ideas", [])
                      if region & {str(n) for n in i.get("nodes", [])}}
            part_id = node.id.split(".")[0]
            chunks = [c for c in (dict(c, slides=[k for k in c["slides"] if k in taught])
                                  if c["kind"] == "slides" else c
                                  for c in self._coverage_chunks(outline))
                      if c.get("slides") or c.get("part") == part_id]
            ids = enrich.node_order(outline)
            results = self._map(lambda c: self._run_chunk(mutated, ids, c, repair=False), chunks)
            failed = [r for r in results if r["error"]]
            if failed:  # retried next run: not cached
                error = "; ".join(f"{_chunk_label(r['chunk'])}: {r['error']}" for r in failed)
                self.log(f"[audit] gap search: {error}")
                return {"skipped": f"the test failed ({error}); it runs again next time"}
            new = [g for r in results for g in r["data"]["gaps"]
                   if (str(g["node"]), g["kind"], str(g.get("term", "")).lower()) not in known]
            result = {"node": node.id, "title": node.title, "unit": unit,
                      "caught": any(str(g["node"]) in region for g in new)}
        cache.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result

    def run_termbase(self, outline: Outline) -> list[dict]:
        """One definition per term; fundamentals made explicit; where each term is defined."""
        cache = self.work_dir / "termbase.json"
        if cache.exists():
            self.terms = json.loads(cache.read_text(encoding="utf-8"))["terms"]
            return self.terms
        self.log("[terms] building the termbase")
        ids = enrich.node_order(outline)
        required = sorted({str(g["term"]).strip() for g in self.coverage.get("gaps", [])
                           if g.get("kind") in enrich.TERM_GAPS and str(g.get("term", "")).strip()})
        user = (f"## LANGUAGE\n{self.language_name}\n\n"
                + _section("REQUIRED TERMS", "\n".join(required))
                + _section("REFERENCE", self._term_references(required))
                + _section("GLOSSARY", self.glossary) + _section("SLIDES", self._slides_numbered())
                + "## TRANSCRIPT\n" + self._lecture(outline))
        try:
            data = self._ask_json(self.rewrite_llm, self.prompts.termbase, user,
                                  lambda d: enrich.validate_termbase(d, ids, required), "termbase")
            terms, error = enrich.build_termbase(data, outline, self.glossary), None
        except (PipelineError, LLMError) as exc:  # not cached: the next run tries again
            self.log(f"[terms] {exc} (not cached: the next run tries again)")
            self.terms, self.terms_error = [], str(exc)
            return self.terms
        cache.write_text(json.dumps({"terms": terms, "error": error}, ensure_ascii=False, indent=2),
                         encoding="utf-8")
        self.terms = terms
        return terms

    def _term_references(self, required: list[str]) -> str:
        """For each required term, the best page of the reference library that names it."""
        hits = []
        for term in required:
            found = books.passages(self.library, [term], k=1, max_chars=1500)
            if found and enrich.term_pattern(term).search(found[0]["text"]):
                hits.append(dict(found[0], text=f'({term}) {found[0]["text"]}'))
        return books.render_passages(hits)

    def _research_one(self, outline: Outline, gap: dict) -> dict:
        node = outline.node(str(gap["node"]))
        part = outline.node(str(gap["node"]).split(".")[0])
        entry = next((t for t in self.terms
                      if gap.get("term") and t["term"].lower() == str(gap["term"]).lower()), None)
        user = (f"## LANGUAGE\n{self.language_name}\n\n"
                + _section("LECTURE", render_outline(outline, node.id))
                + _section("GAP", json.dumps({k: gap.get(k) for k in
                                              ("kind", "node", "term", "detail", "query")},
                                             ensure_ascii=False)
                           + f"\nNode: {node.id} {node.title}")
                + _section("TERM", json.dumps(entry, ensure_ascii=False) if entry else "")
                + _section("LIBRARY", books.render_passages(books.passages(
                    self.library, [str(gap.get("query", "")), str(gap.get("term") or "")])))
                + "## CONTEXT\n" + render_part_transcript(outline, part, [])[:8000])
        try:
            with trace.context(prompt="research", role=trace.ROLES["research"], seat="writer"):
                text, documents = self.researcher(self.prompts.research, user)
            data = extract_json(text)
            errors = enrich.validate_research(data)
            if errors:
                return {"gap": gap["id"], "found": False, "facts": [], "rejected": [],
                        "error": "; ".join(errors)}
        except (LLMError, ValueError) as exc:
            return {"gap": gap["id"], "found": False, "facts": [], "rejected": [], "error": str(exc)}
        kept, rejected = enrich.check_facts(data["facts"], documents, library=self.library)
        return {"gap": gap["id"], "found": bool(kept), "facts": kept, "rejected": rejected,
                "error": None}

    def run_research(self, outline: Outline) -> list[dict]:
        """Search the web for each gap; keep only facts whose quote is found in the page."""
        gaps = self.coverage.get("gaps", [])
        if self.researcher is not None and gaps:
            directory = self._dir("research")

            def one(gap: dict) -> dict:
                cache = directory / f"gap_{gap['id']}.json"
                if cache.exists():
                    return json.loads(cache.read_text(encoding="utf-8"))
                self.log(f"[research] {gap['id']} ({gap['kind']}, node {gap['node']}): "
                         f"{gap['query']}")
                result = self._research_one(outline, gap)
                cache.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                return result

            self.research = {r["gap"]: r for r in self._map(one, gaps)}
        self.supplements = []
        for gap in gaps:
            for fact in self.research.get(gap["id"], {}).get("facts", []):
                self.supplements.append(dict(fact, id=f"E{len(self.supplements) + 1}",
                                             node=str(gap["node"]), gap=gap["id"]))
        return self.supplements

    # -- stage 2: rewrite and verify each part ---------------------------------------
    def _context(self, outline: Outline, part: Node) -> dict:
        noise = noise_by_part(outline)[part.id]
        source = part_source(outline, part, noise)
        terms_before = [t["term"] for t in outline.terms if t["part"] < int(part.id)]
        terms_here = [t["term"] for t in outline.terms if t["part"] == int(part.id)]
        nodes = {n.id for n in part.walk()}
        supplements = [s for s in self.supplements if s["node"] in nodes]
        slides = select_slides(self.slides, source) if self.slides else ""
        # Terms this part uses: the ones it must define, and the ones found in its sources.
        sources = " ".join([source, slides] + [s["text"] for s in supplements])
        termbase = []
        for term in self.terms:
            here = term["part"] == int(part.id)
            if here or (term["part"] < int(part.id)
                        and enrich.term_pattern(term["term"]).search(sources)):
                termbase.append({"term": term["term"], "definition": term["definition"],
                                 "depends_on": term["depends_on"],
                                 "confusable": term["confusable"], "define_here": here})
        ideas = [i for i in self.coverage.get("transcript_ideas", [])
                 if any(str(n) in nodes for n in i.get("nodes", []))]
        shape = dict(structure(part), sources=[s["id"] for s in supplements],
                     ideas=[str(i["id"]) for i in ideas])
        return {"part": part, "noise": noise, "source": source, "slides": slides,
                "transcript": render_part_transcript(outline, part, noise),
                "shape": shape, "checklist": checklist(part),
                "terms_before": sorted(set(terms_before) | {t["term"] for t in self.terms
                                                            if t["part"] < int(part.id)}),
                "terms_here": terms_here, "termbase": termbase, "supplements": supplements,
                "sources_text": sources, "ideas": ideas}

    def _supplements_json(self, ctx: dict, quotes: bool) -> str:
        keys = ("id", "node", "text") + (("quote", "url") if quotes else ())
        return json.dumps([{k: s[k] for k in keys} for s in ctx["supplements"]],
                          ensure_ascii=False, indent=1) if ctx["supplements"] else ""

    def _rewrite(self, outline: Outline, ctx: dict, feedback: str) -> dict:
        part = ctx["part"]
        user = (f"## LANGUAGE\n{self.language_name}\n\n" + self._kind()
                + _section("OUTLINE", render_outline(outline, part.id))
                + _section("REQUIRED STRUCTURE (JSON)", json.dumps(ctx["shape"]))
                + _section("TERMS ALREADY DEFINED", ", ".join(ctx["terms_before"]) or "none")
                + _section("TERMS PLANNED FOR THIS PART", ", ".join(ctx["terms_here"]) or "none")
                + _section("TERMBASE FOR THIS PART (JSON)",
                           json.dumps(ctx["termbase"], ensure_ascii=False, indent=1)
                           if ctx["termbase"] else "")
                + _section("IDEAS OF THIS PART (JSON)", json.dumps(
                    [{k: i[k] for k in ("id", "kind", "idea", "nodes", "units") if k in i}
                     for i in ctx["ideas"]], ensure_ascii=False, indent=1) if ctx["ideas"] else "")
                + _section("SUPPLEMENTS (JSON)", self._supplements_json(ctx, quotes=False))
                + _section("GLOSSARY", self.glossary) + _section("SLIDES", ctx["slides"])
                + "## TRANSCRIPT OF THIS PART\n" + ctx["transcript"]
                # last, so that a repair resends the same opening and hits the provider's
                # prefix cache (ADR-0006)
                + ("\n\n" + _section("FEEDBACK", feedback).rstrip() if feedback else ""))
        data = self._ask_json(self.rewrite_llm, self.prompts.rewrite, user,
                              lambda d: validate_part(d, ctx["shape"]), f"rewrite of part {part.id}")
        data.setdefault("terms_defined", [])
        data.setdefault("reference", [])
        return data

    def _verify(self, outline: Outline, ctx: dict, result: dict) -> tuple[list[dict], list[dict]]:
        """The issues, and the status of each idea of the ledger that this part teaches."""
        part = ctx["part"]
        ids = [c["node"] for c in ctx["checklist"]]
        idea_ids = [i["id"] for i in ctx.get("ideas", [])]
        user = (f"## LANGUAGE\n{self.language_name}\n\n"
                + _section("GLOSSARY", self.glossary) + _section("SLIDES", ctx["slides"])
                + _section("SUPPLEMENTS", self._supplements_json(ctx, quotes=True))
                + _section("TERMBASE", json.dumps(
                    [{"term": t["term"], "definition": t["definition"]} for t in ctx["termbase"]],
                    ensure_ascii=False) if ctx["termbase"] else "")
                + "## TRANSCRIPT OF THIS PART\n" + ctx["transcript"] + "\n\n"
                + "## CHECKLIST (JSON)\n" + json.dumps({"nodes": ctx["checklist"]},
                                                       ensure_ascii=False) + "\n\n"
                + _section("IDEAS (JSON)", json.dumps(
                    [{k: i[k] for k in ("id", "kind", "idea", "nodes", "units") if k in i}
                     for i in ctx["ideas"]],
                    ensure_ascii=False, indent=1) if idea_ids else "")
                + "## SCRIPT\n" + render_script(outline, part, result, self.language))
        data = self._ask_json(self.verify_llm, self.prompts.verify, user,
                              lambda d: validate_verify(d, ids, idea_ids),
                              f"verification of part {part.id}")
        issues = []
        for point in data["points"]:
            if point["status"] != "complete":
                issues.append({"type": "missing" if point["status"] == "incomplete" else "misplaced",
                               "node": str(point["node"]), "sentence": None, "unit": None,
                               "detail": str(point.get("detail") or point["status"])})
        for item in data["issues"]:
            sentence, unit = item.get("sentence"), item.get("unit")
            issues.append({"type": str(item["type"]), "detail": str(item["detail"]), "node": None,
                           "sentence": sentence if isinstance(sentence, int) else None,
                           "unit": unit if isinstance(unit, int) else None})
        ideas, by_id = [], {str(i["id"]): i for i in ctx.get("ideas", [])}
        source = NumberIndex(ctx["source"], self.language)
        script = sentences_of(result)
        said = NumberIndex(" ".join(x["text"] for x in script), self.language)
        citing: dict[str, list[str]] = {}
        for sentence in script:
            for cited in sentence.get("ideas") or []:
                citing.setdefault(str(cited), []).append(sentence["text"])
        for item in data.get("ideas", []) if idea_ids else []:
            idea = by_id.get(str(item.get("idea"))) if isinstance(item, dict) else None
            if idea is None:
                continue
            status, detail = item["status"], str(item.get("detail") or "")
            where = (NumberIndex(" ".join(citing[str(idea["id"])]), self.language)
                     if citing.get(str(idea["id"])) else said)
            lost = numbers_lost(idea["idea"], source, where) if status == "taught" else []
            ideas.append({"idea": idea["id"], "status": status, "judged": status,
                          "detail": detail, "lost": lost})
            if status == "missing":
                issues.append({"type": "idea_missing", "node": str(idea["nodes"][0]),
                               "sentence": None, "unit": None,
                               "detail": (f'The lecture idea {idea["id"]} ({idea["kind"]}: '
                                          f'{idea["idea"]}) is not in the script. {detail}').strip()})
            elif lost:  # the judge and code disagree: reported, not blocking
                issues.append({"type": "idea_numbers", "node": str(idea["nodes"][0]),
                               "sentence": None, "unit": None,
                               "detail": (f'The verifier finds the lecture idea {idea["id"]} '
                                          f'({idea["idea"]}) in the script, but the script does '
                                          f'not contain {_and(lost)}, which the transcript '
                                          "gives. State the number as the teacher did.")})
        return issues, ideas

    def _code_issues(self, ctx: dict, result: dict) -> list[dict]:
        script = sentences_of(result)
        issues = unsupported_numbers(script, ctx["source"], ctx["slides"], self.language)
        issues += external_numbers(script, {s["id"]: s["text"] for s in ctx["supplements"]},
                                   self.language)
        used = {s.get("source") for s in script if s["origin"] == "external"}
        for supplement in ctx["supplements"]:
            if supplement["id"] not in used:
                issues.append({"type": "gap", "node": supplement["node"], "sentence": None,
                               "unit": None,
                               "detail": (f'Supplement {supplement["id"]} fills a gap the lecture '
                                          "left and is not taught. Teach it in its node, tagged "
                                          f'"external" with source {supplement["id"]}.')})
        defined = " | ".join(str(t).lower() for t in result.get("terms_defined", []))
        planned = {t["term"].lower() for t in ctx["termbase"] if t["define_here"]}
        for term in ctx["terms_here"]:
            if term.lower() not in defined and term.lower() not in planned:
                issues.append({"type": "plan", "node": None, "sentence": None, "unit": None,
                               "detail": f'The outline expected this part to define "{term}".'})
        for term in ctx["termbase"]:
            if term["define_here"] and term["term"].lower() not in defined:
                issues.append({"type": "undefined_term", "node": None, "sentence": None,
                               "unit": None,
                               "detail": (f'"{term["term"]}" must be defined in this part, at its '
                                          f'first use: {term["definition"]} (if you did define it, '
                                          'list it in "terms_defined").')})
        part = int(ctx["part"].id)
        later = [t for t in self.terms if t["part"] > part]
        for use in enrich.occurrences(script, later):
            term = next(t for t in later if t["term"] == use["term"])
            issues.append({"type": "term_order", "node": None, "sentence": use["sentence"],
                           "unit": None,
                           "detail": (f'"{term["term"]}" is only defined in part {term["part"]}; '
                                      "do not use it here, or say the same thing without it.")})
        allowed = {t["term"].lower() for t in ctx["termbase"]}
        issues += enrich.stray_terms(script, self.terms, ctx["sources_text"], allowed)
        issues += idea_citations(script, ctx.get("ideas", []))
        return issues

    def _check_terms(self, outline: Outline, ctx: dict, result: dict) -> list[dict]:
        """A judge for one criterion only: is each term used with its one meaning?"""
        if not ctx["termbase"]:
            return []
        script = sentences_of(result)
        pairs = enrich.occurrences(script, ctx["termbase"])
        if not pairs:
            return []
        numbered = "\n".join(f"{k}. {s['text']}" for k, s in enumerate(script, 1))
        user = (f"## LANGUAGE\n{self.language_name}\n\n"
                + "## TERMBASE (JSON)\n" + json.dumps(
                    [{k: t[k] for k in ("term", "definition", "confusable")}
                     for t in ctx["termbase"]], ensure_ascii=False) + "\n\n"
                + "## SCRIPT\n" + numbered + "\n\n"
                + "## OCCURRENCES (JSON)\n" + json.dumps(pairs, ensure_ascii=False))
        data = self._ask_json(self.verify_llm, self.prompts.terms, user,
                              lambda d: enrich.validate_uses(d, pairs),
                              f"terminology check of part {ctx['part'].id}")
        return [{"type": "term_misuse", "node": None, "sentence": use["sentence"], "unit": None,
                 "detail": f'"{use.get("term")}": {use.get("detail") or "used with another meaning"}'}
                for use in data["uses"] if use["status"] == "wrong"]

    def _process(self, outline: Outline, part: Node) -> dict:
        ctx = self._context(outline, part)
        best, feedback, attempts, scope, repairs = None, "", 0, None, []
        for _ in range(self.max_retries + 1):
            attempts += 1
            with trace.context(attempt=attempts, repair=scope_words(scope) if scope else
                               ("whole part" if attempts > 1 else None)):
                result = self._rewrite(outline, ctx, feedback)
                if scope:  # a bounded repair: only the sections at fault come from the new text
                    result = splice_repair(best["result"], result, scope)
                issues = self._code_issues(ctx, result)
                found, ideas = self._verify(outline, ctx, result)
                issues += found
                issues += self._check_terms(outline, ctx, result)
            count = len(blocking(issues))
            if best is not None:
                repairs.append({"attempt": attempts, "scope": scope or "all",
                                "before": best["blocking"], "after": count,
                                "kept": count < best["blocking"]})
            # A repair can damage a script that was better: keep the best attempt so far
            # and replace it only by a strictly better one.
            if best is None or count < best["blocking"]:
                best = {"result": result, "issues": issues, "blocking": count, "ideas": ideas}
            if best["blocking"] == 0:
                break
            more = attempts <= self.max_retries
            scope, shown = repair_plan(part, best["result"], best["issues"])
            self.log(f"[verify] part {part.id}: {best['blocking']} problem(s), "
                     + ("keeping the best attempt" if not more else
                        f"rewriting {scope_words(scope)} with feedback, keeping the rest"
                        if scope
                        else "rewriting the part with feedback"))
            feedback = format_feedback(render_script(outline, part, best["result"], self.language),
                                       shown, scope)
        result = best["result"]
        return {"part": part.id, "title": part.title, "words": part_words(outline, part),
                "source": ctx["source"], "preview": result["preview"],
                "sections": result["sections"], "recap": result["recap"],
                "questions": result["questions"], "terms_defined": result["terms_defined"],
                "reference": result["reference"], "issues": best["issues"],
                "blocking": best["blocking"], "attempts": attempts, "repairs": repairs,
                "flagged": best["blocking"] > 0, "moves": moves(part),
                "supplements": ctx["supplements"], "ideas": best["ideas"]}

    def run_parts(self, outline: Outline, on_done: Callable[[dict], None] | None = None) -> list[dict]:
        """Rewrite and verify every part. `on_done(record)` is called as each part finishes
        (from a worker thread), so that the idea ledger stays current during the run."""
        return self._map(lambda part: self.rewrite_part(outline, part), outline.parts, on_done)

    def rewrite_part(self, outline: Outline, part: Node) -> dict:
        """The verified script of one part, cached in rewrite/. It needs the part's ideas, the
        termbase and the research (flow.py)."""
        cache = self._dir("rewrite") / f"part_{int(part.id):03d}.json"
        if cache.exists():
            return json.loads(cache.read_text(encoding="utf-8"))
        self.log(f"[rewrite] part {part.id}/{len(outline.parts)}: {part.title} "
                 f"({part_words(outline, part)} words)")
        record = self._process(outline, part)
        cache.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        return record

    # -- checks on the whole lecture ---------------------------------------------------
    @trace.tagged(audit="audit_verifier")
    def audit_verifier(self, outline: Outline, records: list[dict]) -> dict:
        """Plant an omission in an accepted part and see whether the verifier notices it."""
        cache = self.work_dir / "audit.json"
        if cache.exists():
            return json.loads(cache.read_text(encoding="utf-8"))
        result = {"skipped": "no accepted section with at least three lecture sentences"}
        for record in records:
            if record["flagged"]:
                continue
            part = outline.node(record["part"])
            sections = sorted(record["sections"],
                              key=lambda s: (outline.node(s["node"]).level == 1,
                                             -sum(x["origin"] == "lecture" for x in s["sentences"])))
            target = next((s for s in sections
                           if sum(x["origin"] == "lecture" for x in s["sentences"]) >= 3), None)
            if target is None:
                continue
            kept = [x for x in target["sentences"] if x["origin"] == "added"][:1] \
                or target["sentences"][:1]
            removed = len(target["sentences"]) - len(kept)
            mutated = dict(record, sections=[dict(s, sentences=kept) if s is target else s
                                             for s in record["sections"]])
            self.log(f"[audit] removing {removed} sentences from node {target['node']} "
                     "to test the verifier")
            issues, ideas = self._verify(outline, self._context(outline, part), mutated)
            # the model's own answers only: the code checks inside _verify must not score
            caught = (any(i["type"] in ("missing", "misplaced") for i in issues)
                      or any(i["judged"] == "missing" for i in ideas))
            result = {"part": part.id, "node": target["node"],
                      "title": outline.node(target["node"]).title, "removed": removed,
                      "caught": caught}
            break
        cache.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result

    @staticmethod
    def global_notes(outline: Outline, records: list[dict]) -> list[str]:
        notes = []
        if outline.fallback:
            notes.append("The outline was built by code (parts cut by size, no points) because "
                         f"the model's outline kept failing the checks: {outline.fallback}")
        defined: dict[str, list[str]] = {}
        for record in records:
            for term in record.get("terms_defined", []):
                defined.setdefault(str(term).lower().strip(), []).append(record["part"])
        for item in outline.terms:
            term = item["term"].lower()
            if not any(term in d or d in term for d in defined):
                notes.append(f'The outline expected part {item["part"]} to define "{item["term"]}", '
                             "but no part reports defining it.")
        for term, parts in defined.items():
            if len(set(parts)) > 1:
                notes.append(f'"{term}" is defined in several parts: {", ".join(sorted(set(parts)))}.')
        total = sum(len(u.split()) for u in outline.units) or 1
        noise = sum(len(outline.text(u).split()) for u in outline.noise_units())
        if noise / total > 0.2:
            notes.append(f"{100 * noise // total}% of the words were marked as noise. Check the list "
                         "at the end of this report: teaching content may have been dropped.")
        return notes

    # -- stage 3: spoken form ------------------------------------------------------
    def _spoken(self, part: Node, record: dict) -> dict:
        blocks = part_blocks(part, record, self.language)
        texts = [b["text"] for b in blocks]
        fallback, spoken = None, texts
        try:
            data = self._ask_json(self.rewrite_llm, self.prompts.spoken,
                                  json.dumps({"blocks": texts}, ensure_ascii=False),
                                  lambda d: validate_spoken(d, len(texts)),
                                  f"spoken form of part {part.id}")
            spoken = data["blocks"]
            before = sum(len(t.split()) for t in texts)
            if sum(len(t.split()) for t in spoken) < 0.6 * before:
                fallback = "the model shortened the text too much; used the rule-based conversion only"
                spoken = texts
        except PipelineError as exc:
            fallback = f"model step failed ({exc}); used the rule-based conversion only"
        residue = {"digits": 0, "symbols": {}}
        for text in spoken:
            found = find_residue(text)
            residue["digits"] += found["digits"]
            for symbol, n in found["symbols"].items():
                residue["symbols"][symbol] = residue["symbols"].get(symbol, 0) + n
        return {"part": part.id,
                "blocks": [dict(b, text=normalize_spoken(t, self.language))
                           for b, t in zip(blocks, spoken)],
                "llm_residue": residue, "fallback": fallback}

    def run_spoken(self, outline: Outline, records: list[dict]) -> list[dict]:
        return self._map(lambda record: self.spoken_part(outline, record, len(records)), records)

    def spoken_part(self, outline: Outline, record: dict, total: int) -> dict:
        """The spoken form of one part's script, cached in spoken/."""
        cache = self._dir("spoken") / f"part_{int(record['part']):03d}.json"
        if cache.exists():
            return json.loads(cache.read_text(encoding="utf-8"))
        self.log(f"[spoken] part {record['part']}/{total}")
        result = self._spoken(outline.node(record["part"]), record)
        cache.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result
