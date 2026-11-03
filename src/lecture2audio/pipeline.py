"""The text stages: outline -> rewrite each part (code gates, verifier, keep the best attempt)
-> spoken form.

Design (see CLAUDE.md for the reasons): a fixed chain of model calls with code checks between
them, not agents talking to each other. One call plans the whole lecture (the outline); each
part is then written and checked on its own, so parts can run in parallel. Every stage writes
its result under the work directory, so a rerun skips finished work and any intermediate file
can be opened or edited by hand.
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import enrich
from .llm import LLM, LLMError, extract_json
from .outline import (Node, Outline, checklist, dump, fallback_outline, moves, noise_by_part,
                      parse_outline, part_source, part_words, render_outline,
                      render_part_transcript, structure)
from .sources import select_slides
from .spoken import (en_cardinal, en_ordinal, find_residue, fr_cardinal, fr_ordinal,
                     normalize_spoken, number_to_words)

PROMPT_DIR = Path(__file__).parent / "prompts"
LANGUAGE_NAMES = {"en": "English", "fr": "French"}
ORIGINS = {"lecture", "added", "external"}
STATUSES = {"complete", "incomplete", "misplaced"}
SOFT_ISSUES = {"long", "filler", "plan", "marker"}


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
                                        "course_check", "cold_read")))


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


def validate_verify(data, node_ids: list[str]) -> list[str]:
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
    return []


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


def unsupported_numbers(script: list[dict], source: str, slides: str, language: str) -> list[dict]:
    """Numbers in sentences tagged 'lecture' that appear in neither source (as digits or words)."""
    known_digits = {_canon(t) for t in _NUMBER.findall(source + " " + slides)}
    known_words = re.sub(r"[\s\-]+", " ", (source + " " + slides).lower())
    issues = []
    for index, sentence in enumerate(script, 1):
        if sentence["origin"] != "lecture":
            continue
        for token in _NUMBER.findall(sentence["text"]):
            canon = _canon(token)
            if canon in known_digits:
                continue
            if canon.isdigit() and len(canon) <= 12:
                words = re.sub(r"[\s\-]+", " ", number_to_words(int(canon), language).lower())
                if words in known_words:
                    continue
            issues.append({
                "type": "unsupported", "sentence": index, "unit": None,
                "detail": (f"The number {token} appears in neither the transcript nor the slides. "
                           'If it is a result you computed, tag the sentence "added".')})
    return issues


def external_numbers(script: list[dict], supplements: dict[str, str], language: str) -> list[dict]:
    """Numbers in 'external' sentences that their supplement does not contain."""
    issues = []
    for index, sentence in enumerate(script, 1):
        if sentence["origin"] != "external":
            continue
        source = supplements.get(sentence.get("source"), "")
        for issue in unsupported_numbers([dict(sentence, origin="lecture")], source, "", language):
            number = issue["detail"].split()[2]
            issues.append(dict(issue, sentence=index, detail=(
                f"The number {number} is not in supplement {sentence.get('source')}. "
                "Use the supplement's own numbers.")))
    return issues


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


def format_feedback(script_text: str, issues: list[dict]) -> str:
    lines = ["A reviewer found problems in an earlier attempt. Fix every one and keep everything "
             "that was correct.", "", "Earlier attempt:", script_text, "", "Problems:"]
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


def blocking(issues: list[dict]) -> list[dict]:
    return [i for i in issues if i["type"] not in SOFT_ISSUES]


# --------------------------------------------------------------------- pipeline

class TextPipeline:
    def __init__(self, work_dir: Path, language: str, rewrite_llm: LLM, verify_llm: LLM,
                 prompts: Prompts, glossary: str = "", slides: list[str] | None = None,
                 max_retries: int = 2, log: Callable[[str], None] = print,
                 max_part_words: int = 2500, workers: int = 3,
                 researcher: Callable[[str, str], tuple[str, list[dict]]] | None = None):
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
        # Filled by the gap stages; empty when they are not run.
        self.coverage: dict = {"slide_ideas": [], "gaps": [], "transcript_ideas": []}
        self.terms: list[dict] = []
        self.research: dict[str, dict] = {}
        self.supplements: list[dict] = []

    def _dir(self, name: str) -> Path:
        directory = self.work_dir / name
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _ask_json(self, llm: LLM, system: str, user: str, validate, what: str,
                  tries: int = 3) -> dict:
        note, problem = "", ""
        for _ in range(tries):
            raw = llm.complete(system, user + note)
            try:
                data = extract_json(raw)
                errors = validate(data)
            except ValueError as exc:
                errors = [f"not valid JSON ({exc})"]
            if not errors:
                return data
            problem = "; ".join(errors)
            note = ("\n\n## PREVIOUS ANSWER REJECTED\nYour previous answer was rejected:\n- "
                    + "\n- ".join(errors)
                    + "\nReturn only the JSON object, in exactly the required format.")
        raise PipelineError(f"{what}: no valid answer after {tries} tries ({problem}).")

    def _map(self, function, items):
        if self.workers == 1 or len(items) < 2:
            return [function(item) for item in items]
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            return list(pool.map(function, items))

    # -- stage 1: outline ----------------------------------------------------------
    def run_outline(self, units: list[str]) -> Outline:
        cache = self.work_dir / "outline.json"
        if cache.exists():
            return Outline.from_dict(json.loads(cache.read_text(encoding="utf-8")))
        self.log(f"[outline] {len(units)} units")
        slides_text = "\n\n".join(self.slides)[:12000]
        user = (f"## LANGUAGE\n{self.language_name}\n\n"
                f"## LIMITS\nAt most {self.max_part_words} words per part.\n\n"
                + _section("GLOSSARY", self.glossary) + _section("SLIDES", slides_text)
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

    def _slides_numbered(self) -> str:
        return "\n\n".join(f"[Slide {k}]\n{s}" for k, s in enumerate(self.slides, 1))

    def run_coverage(self, outline: Outline) -> dict:
        """Slide ideas with their status, and the gaps of the transcript."""
        cache = self.work_dir / "coverage.json"
        if cache.exists():
            self.coverage = json.loads(cache.read_text(encoding="utf-8"))
            return self.coverage
        self.log("[gaps] looking for what the lecture leaves out")
        ids = enrich.node_order(outline)
        user = (f"## LANGUAGE\n{self.language_name}\n\n"
                + _section("GLOSSARY", self.glossary) + _section("SLIDES", self._slides_numbered())
                + "## TRANSCRIPT\n" + self._lecture(outline))
        try:
            data = self._ask_json(self.rewrite_llm, self.prompts.coverage, user,
                                  lambda d: enrich.validate_coverage(d, ids, len(self.slides)),
                                  "gap search")
            result = {"slide_ideas": data["slide_ideas"], "gaps": data["gaps"],
                      "transcript_ideas": data.get("transcript_ideas", [])}
        except PipelineError as exc:
            self.log(f"[gaps] {exc}")
            result = {"slide_ideas": [], "gaps": [], "transcript_ideas": [], "error": str(exc)}
        cache.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        self.coverage = result
        return result

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
            user = (f"## LANGUAGE\n{self.language_name}\n\n"
                    + _section("GLOSSARY", self.glossary)
                    + _section("SLIDES", self._slides_numbered())
                    + "## TRANSCRIPT\n" + self._lecture(mutated))
            try:
                data = self._ask_json(
                    self.rewrite_llm, self.prompts.coverage, user,
                    lambda d: enrich.validate_coverage(d, enrich.node_order(outline),
                                                       len(self.slides)), "gap search audit")
                new = [g for g in data["gaps"]
                       if (str(g["node"]), g["kind"], str(g.get("term", "")).lower()) not in known]
                result = {"node": node.id, "title": node.title, "unit": unit,
                          "caught": any(str(g["node"]) in region for g in new)}
            except PipelineError as exc:
                result = {"skipped": str(exc)}
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
                + _section("GLOSSARY", self.glossary) + _section("SLIDES", self._slides_numbered())
                + "## TRANSCRIPT\n" + self._lecture(outline))
        try:
            data = self._ask_json(self.rewrite_llm, self.prompts.termbase, user,
                                  lambda d: enrich.validate_termbase(d, ids, required), "termbase")
            terms, error = enrich.build_termbase(data, outline, self.glossary), None
        except PipelineError as exc:
            self.log(f"[terms] {exc}")
            terms, error = [], str(exc)
        cache.write_text(json.dumps({"terms": terms, "error": error}, ensure_ascii=False, indent=2),
                         encoding="utf-8")
        self.terms = terms
        return terms

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
                + "## CONTEXT\n" + render_part_transcript(outline, part, [])[:8000])
        try:
            text, documents = self.researcher(self.prompts.research, user)
            data = extract_json(text)
            errors = enrich.validate_research(data)
            if errors:
                return {"gap": gap["id"], "found": False, "facts": [], "rejected": [],
                        "error": "; ".join(errors)}
        except (LLMError, ValueError) as exc:
            return {"gap": gap["id"], "found": False, "facts": [], "rejected": [], "error": str(exc)}
        kept, rejected = enrich.check_facts(data["facts"], documents)
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
        shape = dict(structure(part), sources=[s["id"] for s in supplements])
        return {"part": part, "noise": noise, "source": source, "slides": slides,
                "transcript": render_part_transcript(outline, part, noise),
                "shape": shape, "checklist": checklist(part),
                "terms_before": sorted(set(terms_before) | {t["term"] for t in self.terms
                                                            if t["part"] < int(part.id)}),
                "terms_here": terms_here, "termbase": termbase, "supplements": supplements,
                "sources_text": sources}

    def _supplements_json(self, ctx: dict, quotes: bool) -> str:
        keys = ("id", "node", "text") + (("quote", "url") if quotes else ())
        return json.dumps([{k: s[k] for k in keys} for s in ctx["supplements"]],
                          ensure_ascii=False, indent=1) if ctx["supplements"] else ""

    def _rewrite(self, outline: Outline, ctx: dict, feedback: str) -> dict:
        part = ctx["part"]
        user = (f"## LANGUAGE\n{self.language_name}\n\n"
                + _section("OUTLINE", render_outline(outline, part.id))
                + _section("REQUIRED STRUCTURE (JSON)", json.dumps(ctx["shape"]))
                + _section("TERMS ALREADY DEFINED", ", ".join(ctx["terms_before"]) or "none")
                + _section("TERMS PLANNED FOR THIS PART", ", ".join(ctx["terms_here"]) or "none")
                + _section("TERMBASE FOR THIS PART (JSON)",
                           json.dumps(ctx["termbase"], ensure_ascii=False, indent=1)
                           if ctx["termbase"] else "")
                + _section("SUPPLEMENTS (JSON)", self._supplements_json(ctx, quotes=False))
                + _section("GLOSSARY", self.glossary) + _section("SLIDES", ctx["slides"])
                + _section("FEEDBACK", feedback)
                + "## TRANSCRIPT OF THIS PART\n" + ctx["transcript"])
        data = self._ask_json(self.rewrite_llm, self.prompts.rewrite, user,
                              lambda d: validate_part(d, ctx["shape"]), f"rewrite of part {part.id}")
        data.setdefault("terms_defined", [])
        data.setdefault("reference", [])
        return data

    def _verify(self, outline: Outline, ctx: dict, result: dict) -> list[dict]:
        part = ctx["part"]
        ids = [c["node"] for c in ctx["checklist"]]
        user = (f"## LANGUAGE\n{self.language_name}\n\n"
                + _section("GLOSSARY", self.glossary) + _section("SLIDES", ctx["slides"])
                + _section("SUPPLEMENTS", self._supplements_json(ctx, quotes=True))
                + _section("TERMBASE", json.dumps(
                    [{"term": t["term"], "definition": t["definition"]} for t in ctx["termbase"]],
                    ensure_ascii=False) if ctx["termbase"] else "")
                + "## TRANSCRIPT OF THIS PART\n" + ctx["transcript"] + "\n\n"
                + "## CHECKLIST (JSON)\n" + json.dumps({"nodes": ctx["checklist"]},
                                                       ensure_ascii=False) + "\n\n"
                + "## SCRIPT\n" + render_script(outline, part, result, self.language))
        data = self._ask_json(self.verify_llm, self.prompts.verify, user,
                              lambda d: validate_verify(d, ids), f"verification of part {part.id}")
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
        return issues

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
        best, feedback, attempts = None, "", 0
        for _ in range(self.max_retries + 1):
            attempts += 1
            result = self._rewrite(outline, ctx, feedback)
            issues = self._code_issues(ctx, result)
            issues += self._verify(outline, ctx, result)
            issues += self._check_terms(outline, ctx, result)
            count = len(blocking(issues))
            # A repair can damage a script that was better: keep the best attempt so far
            # and replace it only by a strictly better one.
            if best is None or count < best["blocking"]:
                best = {"result": result, "issues": issues, "blocking": count}
            if best["blocking"] == 0:
                break
            more = attempts <= self.max_retries
            self.log(f"[verify] part {part.id}: {best['blocking']} problem(s), "
                     + ("rewriting with feedback" if more else "keeping the best attempt"))
            feedback = format_feedback(render_script(outline, part, best["result"], self.language),
                                       best["issues"])
        result = best["result"]
        return {"part": part.id, "title": part.title, "words": part_words(outline, part),
                "source": ctx["source"], "preview": result["preview"],
                "sections": result["sections"], "recap": result["recap"],
                "questions": result["questions"], "terms_defined": result["terms_defined"],
                "reference": result["reference"], "issues": best["issues"],
                "blocking": best["blocking"], "attempts": attempts,
                "flagged": best["blocking"] > 0, "moves": moves(part),
                "supplements": ctx["supplements"]}

    def run_parts(self, outline: Outline) -> list[dict]:
        directory = self._dir("rewrite")

        def one(part: Node) -> dict:
            cache = directory / f"part_{int(part.id):03d}.json"
            if cache.exists():
                return json.loads(cache.read_text(encoding="utf-8"))
            self.log(f"[rewrite] part {part.id}/{len(outline.parts)}: {part.title} "
                     f"({part_words(outline, part)} words)")
            record = self._process(outline, part)
            cache.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
            return record

        return self._map(one, outline.parts)

    # -- checks on the whole lecture ---------------------------------------------------
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
            issues = self._verify(outline, self._context(outline, part), mutated)
            caught = any(i["type"] in ("missing", "misplaced") for i in issues)
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
        directory = self._dir("spoken")

        def one(record: dict) -> dict:
            cache = directory / f"part_{int(record['part']):03d}.json"
            if cache.exists():
                return json.loads(cache.read_text(encoding="utf-8"))
            self.log(f"[spoken] part {record['part']}/{len(records)}")
            result = self._spoken(outline.node(record["part"]), record)
            cache.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            return result

        return self._map(one, records)
