"""Course material: the verified script of each part, written again for reading.

The stage runs after the rewrite and works from its output, so every fact it teaches was already
checked (lecture, added or external with a source). A model turns each part into cards (one idea
each: formal definition, instance, full explanation, worked example, a figure, check questions).
Plain code then checks the cards (every script sentence covered, every term defined once and
before use, no analogy, numbers supported, symbols consistent, valid LaTeX and Mermaid), a
second model judges each card against the sentences it claims to teach, and the best attempt is
kept, exactly as for the script. Code also chooses the review questions. `course_render.py`
writes the result as a Markdown vault.
"""
from __future__ import annotations

import copy
import difflib
import json
import re
from pathlib import Path

from pydantic import ValidationError

from . import enrich
from .course_schema import MAX_PARAGRAPHS, Card, Cards, Exercise, describe
from .outline import Node, Outline, render_outline
from .pipeline import (_NUMBER, SOFT_ISSUES, NumberIndex, TextPipeline, _and, _section, blocking,
                       format_feedback, numbers_lost, scope_words, sentences_of,
                       unsupported_numbers)

REVIEW_PER_PART = 3
LOOSE_KINDS = {"undefined_term", "missing_prerequisite", "skipped_step", "forward_reference",
               "undefined_symbol", "ambiguous", "confusing_sentence", "missing_context"}
EXERCISE_STATUSES = {"correct": None, "wrong": "exercise_wrong", "added_fact": "added_fact"}
PLAIN_MAX_WORDS = 25  # per sentence of "In short"; a warning, not a gate (readability formulas
# do not predict comprehension: Guo et al., 2025)
JUDGE_STATUSES = {"faithful": None, "changed": "changed", "added_fact": "added_fact",
                  "lost": "lost", "analogy": "analogy"}
SOFT = SOFT_ISSUES

ANALOGY = {
    "en": re.compile(r"\b(imagine|think of (?:it|this|them) as|is like an? |are like |as if |"
                     r"analogy|analogous|metaphor|picture (?:a|an|the) )", re.I),
    "fr": re.compile(r"\b(imagine[zr]?|pensez à|comme si|c'est comme|est comme (?:un|une) |"
                     r"analogie|métaphore|figurez-vous)", re.I),
}


# ---------------------------------------------------------------- the schema

def _texts(card: dict, everything: bool = False) -> list[str]:
    """The prose of a card, for the checks on wording; with `everything`, also its tables,
    figures, code and notation, for the checks on what the card holds."""
    texts = [card["title"], card.get("plain", ""), card["definition"], card["instance"],
             *card["explanation"], *card["formulas"], *card.get("proof", [])]
    if card.get("example"):
        texts += [card["example"]["title"], *card["example"]["steps"]]
    for check in card["checks"]:
        texts += [check["question"], check["answer"]]
    if card.get("diagram"):
        texts += [card["diagram"]["title"], *(f["caption"] for f in card["diagram"]["frames"])]
    if everything:
        if card.get("table"):
            texts += [*card["table"]["columns"], *(c for row in card["table"]["rows"] for c in row)]
        if card.get("diagram"):
            texts += [f.get("mermaid") or f.get("ascii") or "" for f in card["diagram"]["frames"]]
        texts += [snippet["code"] for snippet in card.get("code", [])]
        texts += [f"{n['symbol']} {n['meaning']}" for n in card.get("notation", [])]
    return [t for t in texts if t]


def validate_cards(data, ctx: dict) -> list[str]:
    """The gate on the model's answer: the schema first, then what depends on this part."""
    try:
        parsed = Cards.model_validate(data)
    except ValidationError as exc:
        return describe(exc)
    nodes, count = ctx["nodes"], ctx["count"]
    terms = {enrich._key(t["term"]) for t in ctx["termbase"]}
    idea_ids = {str(i["id"]) for i in ctx.get("ideas", [])}
    errors, last = [], -1
    for k, card in enumerate(parsed.cards, 1):
        where = f"cards.{k}"
        if card.node not in nodes:
            errors.append(f'{where}: "node" must be one of {nodes}')
        else:
            position = nodes.index(card.node)
            if position < last:
                errors.append(f"{where}: cards must follow the order of the nodes {nodes}")
            last = max(last, position)
        bad = [t for t in card.terms if enrich._key(t) not in terms]
        if bad:
            errors.append(f"{where}: terms {bad} are not in the termbase; use the exact spelling "
                          "or leave them out")
        if card.terms and not card.definition:
            errors.append(f'{where}: a card with "terms" needs a "definition"')
        if any(not 1 <= n <= count for n in card.covers):
            errors.append(f"{where}: sentence numbers in \"covers\" are between 1 and {count}")
        unknown = [i for i in card.ideas if i not in idea_ids]
        if unknown:
            errors.append(f'{where}: "ideas" {unknown} are not in IDEAS TO TEACH; use the ids '
                          f"{sorted(idea_ids)} or an empty list")
    for k, exercise in enumerate(parsed.exercises, 1):
        if any(not 1 <= n <= count for n in exercise.covers):
            errors.append(f'exercises.{k}: sentence numbers in "covers" are between 1 and {count}')
    return errors


def normalise(card: dict) -> dict:
    """The card with every optional field present and every text stripped."""
    return Card.model_validate(card).model_dump(mode="json", by_alias=True)


def _issue(kind: str, detail: str, card: int | None = None, node: str | None = None) -> dict:
    return {"type": kind, "node": node, "sentence": None, "unit": None,
            "detail": (f"card {card}: " if card else "") + detail}


def card_issues(cards: list[dict], ctx: dict, language: str) -> list[dict]:
    """Checks that need no model."""
    issues = []
    script = ctx["script"]
    covered = {n for c in cards for n in c["covers"]}
    for number in range(1, len(script) + 1):
        if number not in covered:
            issues.append(dict(_issue("uncovered", f'script sentence {number} is not covered by '
                                                   f'any card: "{script[number - 1]["text"][:90]}"'),
                               sentence=number))
    claimed = {i for c in cards for i in c.get("ideas", [])}
    for idea in ctx.get("ideas", []):
        if str(idea["id"]) not in claimed:
            issues.append(_issue("idea_unassigned", f'the lecture idea {idea["id"]} '
                                                    f'({idea["kind"]}: {idea["idea"]}) is in no '
                                                    'card\'s "ideas"; teach it in a card and list '
                                                    "its id there.", None,
                                 str(idea["nodes"][0])))
    here = [t for t in ctx["termbase"] if t["define_here"]]
    defined: dict[str, list[int]] = {}
    for k, card in enumerate(cards, 1):
        if (card["terms"] or card["kind"] == "procedure") and not card["checks"]:
            issues.append(_issue("no_check", "every card that defines a term or is a procedure "
                                             "needs a check question.", k, card["node"]))
        if card["kind"] == "procedure" and not card["example"]:
            issues.append(_issue("no_example", "a procedure needs a worked example.", k,
                                 card["node"]))
        if not card.get("plain"):
            issues.append(_issue("no_plain", 'every card needs "plain": the idea in one to three '
                                             "short sentences of everyday words.", k, card["node"]))
        for sentence in re.split(r"(?<=[.!?])\s+", card.get("plain", "")):
            if len(sentence.split()) > PLAIN_MAX_WORDS:
                issues.append(_issue("plain_long", f'"{sentence[:60]}..." has '
                                                   f"{len(sentence.split())} words; keep each "
                                                   f"sentence of \"plain\" under "
                                                   f"{PLAIN_MAX_WORDS} words.", k, card["node"]))
                break
        for term in card["terms"]:
            defined.setdefault(enrich._key(term), []).append(k)
        for text in _texts(card):
            if ANALOGY[language].search(text):
                issues.append(_issue("analogy", f'"{text[:80]}": no analogy and no comparison to '
                                                "something outside the field; state the idea or "
                                                "show the real object.", k, card["node"]))
                break
    for term in here:
        where = defined.get(enrich._key(term["term"]), [])
        if not where:
            issues.append(_issue("undefined_term", f'"{term["term"]}" must be defined in this '
                                                   f'part, in the card that first needs it: '
                                                   f'{term["definition"]}'))
        elif len(where) > 1:
            issues.append(_issue("double_definition", f'"{term["term"]}" is defined in cards '
                                                      f"{where}; define it once."))
    # a term of this part is not used before its card
    for term in here:
        first = (defined.get(enrich._key(term["term"])) or [None])[0]
        if first is None:
            continue
        pattern = enrich.term_pattern(term["term"])
        for k, card in enumerate(cards[:first - 1], 1):
            if any(pattern.search(t) for t in _texts(card)):
                issues.append(_issue("term_order", f'"{term["term"]}" is used before card '
                                                   f"{first}, which defines it.", k, card["node"]))
                break
    # terms of later parts are not used here
    for term in ctx["later_terms"]:
        pattern = enrich.term_pattern(term["term"])
        for k, card in enumerate(cards, 1):
            if any(pattern.search(t) for t in _texts(card)):
                issues.append(_issue("term_order", f'"{term["term"]}" is only defined in part '
                                                   f'{term["part"]}; do not use it here.', k,
                                     card["node"]))
                break
    # numbers that the script does not contain
    source = " ".join(s["text"] for s in script)
    for k, card in enumerate(cards, 1):
        fields = [{"origin": "lecture", "text": t} for t in
                  [card.get("plain", ""), card["definition"], *card["explanation"],
                   *card["formulas"], *card.get("proof", [])] if t]
        for issue in unsupported_numbers(fields, source, ctx["slides"], language):
            issues.append(_issue("unsupported", issue["detail"].split(" appears")[0]
                                 + " is in neither the script nor the slides; the definition, the "
                                   "explanation and the formulas may only hold the script's "
                                   "numbers.", k, card["node"]))
    # a symbol keeps one meaning
    seen: dict[str, tuple[str, int]] = {}
    for k, card in enumerate(cards, 1):
        for note in card["notation"]:
            before = seen.get(note["symbol"])
            if before and before[0].lower() != note["meaning"].lower():
                issues.append(_issue("notation", f'the symbol {note["symbol"]} means '
                                                 f'"{note["meaning"]}" here and "{before[0]}" in '
                                                 f"card {before[1]}; one symbol, one meaning.",
                                     k, card["node"]))
            seen.setdefault(note["symbol"], (note["meaning"], k))
    for symbol, meaning in ctx["notation"].items():
        for k, card in enumerate(cards, 1):
            for note in card["notation"]:
                if note["symbol"] == symbol and note["meaning"].lower() != meaning.lower():
                    issues.append(_issue("notation", f'the symbol {symbol} already means '
                                                     f'"{meaning}" earlier in the course.', k,
                                         card["node"]))
    return issues


def question_issues(cards: list[dict], exercises: list[dict]) -> list[dict]:
    """Near-duplicate questions waste the reader's retrieval practice (deterministic QC of
    generated questions: Shintani, 2026)."""
    asked = [(f"card {k}", c["question"]) for k, card in enumerate(cards, 1)
             for c in card["checks"]]
    asked += [(f"exercise {k}", e["question"]) for k, e in enumerate(exercises, 1)]
    issues = []
    for a in range(len(asked)):
        for b in range(a + 1, len(asked)):
            ratio = difflib.SequenceMatcher(None, asked[a][1].lower(), asked[b][1].lower()).ratio()
            if ratio >= 0.9:
                issues.append(_issue("duplicate_question", f"the questions of {asked[a][0]} and "
                                                           f"{asked[b][0]} are nearly the same; "
                                                           "ask about another point."))
    return issues


def validate_judgement(data, count: int, idea_ids=(), exercises: int = 0) -> list[str]:
    if not isinstance(data, dict) or not isinstance(data.get("cards"), list):
        return ['the answer must be {"cards": [...]}']
    errors, seen = [], set()
    for item in data["cards"]:
        if not isinstance(item, dict) or item.get("status") not in JUDGE_STATUSES \
                or not isinstance(item.get("card"), int):
            return ['each entry must be {"card": number, "status": one of '
                    f'{sorted(JUDGE_STATUSES)}, "detail": "..."}}']
        seen.add(item["card"])
    missing = [n for n in range(1, count + 1) if n not in seen]
    if missing:
        errors.append(f"give one entry for every card; missing {missing}")
    if idea_ids:
        given = {i.get("idea"): i.get("status") for i in data.get("ideas", [])
                 if isinstance(i, dict)}
        absent = [i for i in idea_ids if given.get(i) not in ("taught", "missing")]
        if absent:
            errors.append('give {"idea": id, "status": "taught" or "missing"} for every idea '
                          f"of IDEAS; missing {absent}")
        for item in data.get("ideas", []):
            numbers = item.get("cards", []) if isinstance(item, dict) else []
            if not isinstance(numbers, list) or any(
                    not isinstance(n, int) or not 1 <= n <= count for n in numbers):
                errors.append(f'idea {item.get("idea") if isinstance(item, dict) else item}: '
                              f'"cards" lists card numbers between 1 and {count}')
    if exercises:
        given = {i.get("exercise"): i.get("status") for i in data.get("exercises", [])
                 if isinstance(i, dict)}
        absent = [n for n in range(1, exercises + 1) if given.get(n) not in EXERCISE_STATUSES]
        if absent:
            errors.append('give {"exercise": number, "status": one of '
                          f'{sorted(EXERCISE_STATUSES)}, "detail": "..."}} for every exercise; '
                          f"missing {absent}")
    return errors


def validate_cold(data, card_ids: list[str]) -> list[str]:
    if not isinstance(data, dict) or not isinstance(data.get("loose"), list):
        return ['the answer must be {"loose": [...]}']
    errors = []
    for k, item in enumerate(data["loose"], 1):
        if not isinstance(item, dict) or str(item.get("card")) not in card_ids \
                or item.get("kind") not in LOOSE_KINDS or not str(item.get("detail", "")).strip():
            errors.append(f'item {k} must be {{"card": one of {card_ids}, "kind": one of '
                          f'{sorted(LOOSE_KINDS)}, "detail": "..."}}')
    return errors


def reader_text(cards: list[dict]) -> str:
    """The cards as a student meets them, for the cold read."""
    out = []
    for card in cards:
        out.append(f"[{card['id']}] {card['title']} ({card['kind']})")
        if card.get("plain"):
            out.append(f"In short: {card['plain']}")
        if card["definition"]:
            out.append(f"Definition: {card['definition']}")
        if card["instance"]:
            out.append(f"Instance: {card['instance']}")
        out += card["explanation"]
        out += [f"Formula: {f}" for f in card["formulas"]]
        out += [f"Symbol {n['symbol']}: {n['meaning']}" for n in card["notation"]]
        if card["example"]:
            out += [f"Example, {card['example']['title']}:"] + card["example"]["steps"]
        if card.get("proof"):
            out += ["Proof:"] + card["proof"]
        if card["diagram"]:
            out += [f"Figure: {f['caption']}" for f in card["diagram"]["frames"]]
        if card.get("table"):
            out.append("Table: " + " | ".join(card["table"]["columns"]))
            out += [" | ".join(row) for row in card["table"]["rows"]]
        out.append("")
    return "\n".join(out).strip()


# ------------------------------------------------------- sources, review

def derive(card: dict, script: list[dict], source: str, slides: str, language: str) -> dict:
    """Facts the code knows about a card: where it comes from, whether it is illustrative."""
    covered = [script[n - 1] for n in card["covers"]]
    sources = sorted({s.get("source") for s in covered if s["origin"] == "external"})
    illustration = [{"origin": "lecture", "text": t} for t in
                    [card["instance"], *(card["example"]["steps"] if card["example"] else [])] if t]
    return dict(card, sources=sources,
                origin="external" if sources else
                ("added" if all(s["origin"] == "added" for s in covered) else "lecture"),
                illustrative=bool(unsupported_numbers(illustration, source, slides, language)))


def build_review(parts: list[dict]) -> dict:
    """Spaced review. At the end of part p: one check from each earlier part (oldest first),
    at most REVIEW_PER_PART. At the end of the lecture: one check from every part."""
    pool = []
    for part in parts:
        checks = [dict(check, card=c["id"], part=part["part"])
                  for c in part["cards"] for check in c["checks"]]
        pool.append(checks)
    review = {}
    for p, part in enumerate(parts):
        picked = []
        for earlier in pool[:p]:
            if earlier:
                picked.append(earlier[p % len(earlier)])
        review[part["part"]] = picked[:REVIEW_PER_PART]
    final = [checks[(len(parts) + k) % len(checks)] for k, checks in enumerate(pool) if checks]
    review["final"] = final
    return review


def collect_notation(parts: list[dict]) -> tuple[list[dict], list[str]]:
    entries, clashes, seen = [], [], {}
    for part in parts:
        for card in part["cards"]:
            for note in card["notation"]:
                before = seen.get(note["symbol"])
                if before and before["meaning"].lower() != note["meaning"].lower():
                    clashes.append(f'The symbol {note["symbol"]} means "{before["meaning"]}" '
                                   f'in card {before["card"]} and "{note["meaning"]}" in card '
                                   f'{card["id"]}.')
                elif not before:
                    seen[note["symbol"]] = {"symbol": note["symbol"], "meaning": note["meaning"],
                                            "part": part["part"], "card": card["id"]}
                    entries.append(seen[note["symbol"]])
    return entries, clashes


# -------------------------------------------------------------------- the stage

def _place(issue: dict, record: dict) -> str | None:
    """Where a course issue lies: the node of its card or sentence, "exercises", or None."""
    if issue.get("field"):
        return issue["field"]
    nodes = [s["node"] for s in record["sections"]]
    node, k = issue.get("node"), issue.get("sentence")
    if not node and isinstance(k, int):
        owner = [s["node"] for s in record["sections"] for _ in s["sentences"]]
        node = owner[k - 1] if 1 <= k <= len(owner) else None
    return str(node) if node and str(node) in nodes else None


def card_scope(issues: list[dict], record: dict, cards: list[dict]) -> list[str] | None:
    """The sections (nodes) whose cards a repair rewrites, and "exercises" when an exercise is
    at fault, in order; None when an issue cannot be placed (a term no card defines) or every
    section with cards is at fault, which calls for a full rewrite. The same bounded repair as
    the script's (`pipeline.repair_plan`): what passed is kept word for word."""
    scope: list[str] = []
    for issue in blocking(issues):
        where = _place(issue, record)
        if where is None:
            return None
        if where not in scope:
            scope.append(where)
    if not scope or {c["node"] for c in cards} <= set(scope):
        return None
    nodes = [s["node"] for s in record["sections"]]
    return [n for n in nodes if n in scope] + (["exercises"] if "exercises" in scope else [])


def splice_cards(best: dict, cards: list[dict], exercises: list[dict], scope: list[str],
                 nodes: list[str]) -> tuple[list[dict], list[dict]]:
    """The best attempt's cards with those of the nodes in `scope` taken from the repair, in the
    order of the nodes; the exercises from the repair only when they are in `scope`."""
    out = []
    for node in nodes:
        source = cards if node in scope else best["cards"]
        out += [copy.deepcopy(c) for c in source if c["node"] == node]
    kept = exercises if "exercises" in scope else best["exercises"]
    return out, copy.deepcopy(kept)


def card_repair_lines(parts: list[dict]) -> list[str]:
    """Every repair and revision of the course material: what it rewrote, whether it was kept."""
    lines = []
    for part in parts:
        for repair in part.get("repairs", []):
            what = ("all the cards were" if repair["scope"] == "all"
                    else "the cards of " + scope_words(repair["scope"]) + " were"
                    if any(s != "exercises" for s in repair["scope"]) else "the exercises were")
            outcome = "so the repair was kept" if repair["kept"] else "so the earlier cards were kept"
            lines.append(f"- Part {part['part']}, attempt {repair['attempt']}: {what} rewritten; "
                         f"the blocking problems went from {repair['before']} to "
                         f"{repair['after']}, {outcome}.")
    return (["", "Repairs of the course material (a repair rewrites only the cards of the "
                 "sections at fault when it can, and the program keeps the other cards word for "
                 "word):", ""] + lines + [""]) if lines else []


class CoursePipeline:
    """Runs after TextPipeline: reuses its models, prompts, caches and logging."""

    def __init__(self, pipeline: TextPipeline, notation: list[dict] | None = None):
        self.p = pipeline
        self.notation = {n["symbol"]: n["meaning"] for n in (notation or [])}

    def _context(self, outline: Outline, part: Node, record: dict) -> dict:
        script = sentences_of(record)
        text = " ".join(s["text"] for s in script)
        pid = int(part.id)
        termbase = [{"term": t["term"], "definition": t["definition"],
                     "depends_on": t["depends_on"], "confusable": t["confusable"],
                     "define_here": t["part"] == pid}
                    for t in self.p.terms
                    if t["part"] == pid or (t["part"] < pid
                                            and enrich.term_pattern(t["term"]).search(text))]
        return {"part": part, "script": script, "count": len(script),
                "nodes": [s["node"] for s in record["sections"]], "termbase": termbase,
                "later_terms": [t for t in self.p.terms if t["part"] > pid],
                "slides": "", "notation": dict(self.notation),
                "ideas": [i for i in self.p.coverage.get("transcript_ideas", [])
                          if any(str(n) in {s["node"] for s in record["sections"]}
                                 for n in i["nodes"])]}

    @staticmethod
    def _numbered(outline: Outline, record: dict) -> str:
        lines, number = [], 0
        for section in record["sections"]:
            node = outline.node(section["node"])
            lines.append("#" * (node.level + 2) + f" {node.id} {node.title}")
            for sentence in section["sentences"]:
                number += 1
                tag = sentence["origin"] + (f" {sentence.get('source')}"
                                            if sentence["origin"] == "external" else "")
                if sentence.get("ideas"):
                    tag += " " + ",".join(map(str, sentence["ideas"]))
                lines.append(f"{number}. [{tag}] {sentence['text']}")
            lines.append("")
        return "\n".join(lines).strip()

    def _write(self, outline: Outline, ctx: dict, record: dict, feedback: str) -> list[dict]:
        part = ctx["part"]
        user = (f"## LANGUAGE\n{self.p.language_name}\n\n"
                + _section("OUTLINE", render_outline(outline, part.id))
                + _section("REQUIRED STRUCTURE (JSON)", json.dumps({"sections": ctx["nodes"]}))
                + _section("TERMBASE FOR THIS PART (JSON)",
                           json.dumps(ctx["termbase"], ensure_ascii=False, indent=1)
                           if ctx["termbase"] else "")
                + _section("NOTATION SO FAR (JSON)",
                           json.dumps([{"symbol": s, "meaning": m}
                                       for s, m in ctx["notation"].items()], ensure_ascii=False)
                           if ctx["notation"] else "")
                + _section("IDEAS TO TEACH (JSON)",
                           json.dumps([{k: i[k] for k in ("id", "kind", "idea", "nodes") if k in i}
                                       for i in ctx["ideas"]], ensure_ascii=False, indent=1)
                           if ctx["ideas"] else "")
                + _section("FEEDBACK", feedback)
                + "## SCRIPT\n" + self._numbered(outline, record))
        data = self.p._ask_json(self.p.rewrite_llm, self.p.prompts.course, user,
                                lambda d: validate_cards(d, ctx), f"course of part {part.id}")
        exercises = [Exercise.model_validate(e).model_dump(mode="json")
                     for e in data.get("exercises") or []]
        return [normalise(c) for c in data["cards"]], exercises

    def _judge(self, ctx: dict, record: dict, outline: Outline, cards: list[dict],
               exercises: list[dict] = ()) -> tuple[list[dict], list[dict]]:
        """The issues, and for each idea of the part its status and the numbers of the cards
        that teach it (the judge's, else the cards that claim the idea)."""
        part = ctx["part"]
        shown = [{"card": k, "title": c["title"], "plain": c.get("plain", ""), "terms": c["terms"],
                  "definition": c["definition"], "explanation": c["explanation"],
                  "proof": c.get("proof", []),
                  "formulas": c["formulas"], "instance": c["instance"], "example": c["example"],
                  "diagram": c["diagram"], "table": c["table"], "checks": c["checks"],
                  "covers": c["covers"], "ideas": c.get("ideas", [])}
                 for k, c in enumerate(cards, 1)]
        idea_ids = [i["id"] for i in ctx["ideas"]]
        source = NumberIndex(" ".join(s["text"] for s in ctx["script"]), self.p.language)
        user = (f"## LANGUAGE\n{self.p.language_name}\n\n## SCRIPT\n"
                + self._numbered(outline, record) + "\n\n"
                + _section("IDEAS (JSON)", json.dumps(
                    [{k: i[k] for k in ("id", "kind", "idea")} for i in ctx["ideas"]],
                    ensure_ascii=False, indent=1) if ctx["ideas"] else "")
                + _section("EXERCISES (JSON)", json.dumps(
                    [dict(e, exercise=k) for k, e in enumerate(exercises, 1)], ensure_ascii=False,
                    indent=1) if exercises else "")
                + "## CARDS (JSON)\n" + json.dumps(shown, ensure_ascii=False, indent=1))
        data = self.p._ask_json(self.p.verify_llm, self.p.prompts.course_check, user,
                                lambda d: validate_judgement(d, len(cards), idea_ids,
                                                             len(exercises)),
                                f"check of the course of part {part.id}")
        issues = []
        for item in data["cards"]:
            kind = JUDGE_STATUSES[item["status"]]
            if kind and 1 <= item["card"] <= len(cards):
                issues.append(_issue(kind, str(item.get("detail") or item["status"]),
                                     item["card"], cards[item["card"] - 1]["node"]))
        for item in data.get("exercises", []) if exercises else []:
            kind = EXERCISE_STATUSES.get(item.get("status"))
            if kind and isinstance(item.get("exercise"), int):
                issues.append(dict(_issue(kind, f'exercise {item["exercise"]}: '
                                                + str(item.get("detail") or item["status"])),
                                   field="exercises"))
        by_id = {str(i["id"]): i for i in ctx["ideas"]}
        ideas = []
        for item in data.get("ideas", []) if idea_ids else []:
            idea = by_id.get(str(item.get("idea"))) if isinstance(item, dict) else None
            if idea is None:
                continue
            numbers = [n for n in item.get("cards") or [] if 1 <= n <= len(cards)] or \
                [k for k, c in enumerate(cards, 1) if str(idea["id"]) in c.get("ideas", [])]
            status, lost = item["status"], []
            why = "is not taught in any card."
            names = _and([f"{part.id}.{n}" for n in numbers]) if numbers else ""
            if status == "taught" and not numbers:  # a claim without a card is no evidence
                status, why = "missing", "is said to be taught, but no card is named or claims it."
            elif status == "taught":
                held = NumberIndex("\n".join(t for n in numbers for t in _texts(cards[n - 1], True)),
                                   self.p.language)
                lost = numbers_lost(idea["idea"], source, held)
            ideas.append({"idea": idea["id"], "status": status, "judged": item["status"],
                          "cards": numbers if status == "taught" else [], "lost": lost})
            if status == "missing":
                issues.append(_issue("idea_missing", f'The lecture idea {idea["id"]} ({idea["kind"]}: '
                                                     f'{idea["idea"]}) {why}',
                                     None, str(idea["nodes"][0])))
            elif lost:  # the judge and code disagree: reported, not blocking
                issues.append(_issue("idea_numbers", f'The judge finds the lecture idea {idea["id"]} '
                                                     f'({idea["idea"]}) in card {names}, but the '
                                                     f'card does not contain {_and(lost)}, which '
                                                     "the script gives. State the number as the "
                                                     "script does.",
                                     None, str(idea["nodes"][0])))
        return issues, ideas

    def _process(self, outline: Outline, part: Node, record: dict, start: dict | None = None,
                 feedback: str = "", tries: int | None = None,
                 scope: list[str] | None = None) -> dict:
        """Write, check by code, judge, and keep the best attempt. `start` (a finished result)
        and `feedback` make a revision: the earlier result stays unless the new one has no more
        blocking problems. With `scope` (see card_scope), the first attempt replaces only the
        cards of those nodes; later repairs are bounded the same way when they can be."""
        ctx = self._context(outline, part, record)
        ctx["slides"] = " ".join(self.p.slides) if self.p.slides else ""
        language = self.p.language
        best, attempts, repairs = None, 0, []
        if start:
            best = {"cards": start["cards"], "exercises": start.get("exercises", []),
                    "issues": start["issues"], "blocking": start["blocking"],
                    "ideas": [dict(i, cards=[int(c.rsplit(".", 1)[1]) for c in i["cards"]])
                              for i in start.get("ideas", [])]}
        for _ in range(self.p.max_retries + 1 if tries is None else tries):
            attempts += 1
            cards, exercises = self._write(outline, ctx, record, feedback)
            if scope and best is not None:  # a bounded repair keeps every card that passed
                cards, exercises = splice_cards(best, cards, exercises, scope, ctx["nodes"])
            judged, ideas = self._judge(ctx, record, outline, cards, exercises)
            issues = card_issues(cards, ctx, language) + question_issues(cards, exercises) + judged
            count = len([i for i in issues if i["type"] not in SOFT])
            better = count <= best["blocking"] if (start and attempts == 1) else (
                best is None or count < best["blocking"])
            if best is not None:
                repairs.append({"attempt": (start or {}).get("attempts", 0) + attempts,
                                "scope": scope or "all", "before": best["blocking"],
                                "after": count, "kept": better})
            if better:
                best = {"cards": cards, "exercises": exercises, "issues": issues,
                        "blocking": count, "ideas": ideas, "revised": bool(start)}
            if best["blocking"] == 0:
                break
            more = attempts < (self.p.max_retries + 1 if tries is None else tries)
            scope = card_scope(best["issues"], record, best["cards"])
            self.p.log(f"[course] part {part.id}: {best['blocking']} problem(s), "
                       + ("keeping the best attempt" if not more else
                          f"rewriting the cards of {scope_words(scope)} with feedback, keeping "
                          "the rest" if scope else "rewriting with feedback"))
            shown = [i for i in best["issues"] if i["type"] not in SOFT
                     and (not scope or _place(i, record) in scope)]
            feedback = format_feedback(json.dumps({"cards": best["cards"],
                                                   "exercises": best["exercises"]},
                                                  ensure_ascii=False), shown, scope)
        source = " ".join(s["text"] for s in ctx["script"])
        cards = [dict(derive(c, ctx["script"], source, ctx["slides"], language),
                      id=f"{part.id}.{k}") for k, c in enumerate(best["cards"], 1)]
        exercises = [dict(e, id=f"{part.id}.x{k}") for k, e in enumerate(best["exercises"], 1)]
        # the ledger's view: which card (by id) teaches each idea of the part
        ideas = [dict(i, cards=[cards[n - 1]["id"] for n in i["cards"]]) for i in best["ideas"]]
        return {"part": part.id, "title": part.title, "cards": cards, "exercises": exercises,
                "issues": best["issues"], "blocking": best["blocking"],
                "attempts": (start or {}).get("attempts", 0) + attempts,
                "repairs": (start or {}).get("repairs", []) + repairs,
                "flagged": best["blocking"] > 0, "ideas": ideas,
                "revised": best.get("revised", False)}

    def cold_read(self, parts: list[dict], tag: str = "cold") -> list[dict]:
        """A reader that knows only the earlier parts looks for loose ends, part by part."""
        directory = self.p._dir("course")

        def one(part: dict) -> dict:
            cache = directory / f"{tag}_{int(part['part']):03d}.json"
            if cache.exists():
                return json.loads(cache.read_text(encoding="utf-8"))
            pid = int(part["part"])
            known = [{"term": t["term"], "definition": t["definition"]} for t in self.p.terms
                     if t["part"] < pid]
            ids = [c["id"] for c in part["cards"]]
            user = (f"## LANGUAGE\n{self.p.language_name}\n\n"
                    + _section("KNOWN BEFORE (JSON)", json.dumps(known, ensure_ascii=False,
                                                                indent=1) if known else "nothing")
                    + "## THIS PART\n" + reader_text(part["cards"]))
            try:
                data = self.p._ask_json(self.p.verify_llm, self.p.prompts.cold_read, user,
                                        lambda d: validate_cold(d, ids),
                                        f"cold read of part {part['part']}")
                result = {"part": part["part"], "loose": data["loose"]}
            except Exception as exc:  # a failed audit must not stop the build
                self.p.log(f"[course] cold read of part {part['part']}: {exc}")
                result = {"part": part["part"], "loose": [], "error": str(exc)}
            cache.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            return result

        return self.p._map(one, parts)

    def run(self, outline: Outline, records: list[dict], on_done=None, audit: bool = True) -> dict:
        """Cards for every part. `on_done(part result)` is called as each part finishes (from a
        worker thread), so that the idea ledger stays current; `audit` runs the planted test of
        the judge."""
        directory = self.p._dir("course")
        by_part = {r["part"]: r for r in records}

        def one(part: Node) -> dict:
            cache = directory / f"part_{int(part.id):03d}.json"
            if cache.exists():
                return json.loads(cache.read_text(encoding="utf-8"))
            self.p.log(f"[course] part {part.id}/{len(outline.parts)}: {part.title}")
            result = self._process(outline, part, by_part[part.id])
            cache.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            return result

        parts = self.p._map(one, outline.parts, on_done)
        cold = self.cold_read(parts)
        parts, cold = self.revise(outline, parts, cold, by_part)
        for part in parts:  # the ledger follows a revision too
            if on_done and part.get("revised"):
                on_done(part)
        notation, clashes = collect_notation(parts)
        (directory / "notation.json").write_text(json.dumps(notation, ensure_ascii=False,
                                                            indent=2), encoding="utf-8")
        return {"parts": parts, "notation": notation, "clashes": clashes,
                "review": build_review(parts), "cold": cold,
                "audit": self.audit_judge(outline, parts, by_part) if audit else None}

    def revise(self, outline: Outline, parts: list[dict], cold: list[dict],
               by_part: dict) -> tuple[list[dict], list[dict]]:
        """One revision round from the cold read: the loose ends a simulated first reader found
        (unknown terms, missing context, confusing sentences) go back to the writer once, and
        the revision is kept only if it has no more blocking problems. Feedback from simulated
        readers, applied by one editor, improves both readability and factuality, best at one
        or two rounds (Jung et al., 2026)."""
        directory = self.p._dir("course")
        loose = {c["part"]: c["loose"] for c in cold if c.get("loose")}
        if not loose:
            return parts, cold

        def one(part: dict) -> dict:
            if part["part"] not in loose:
                return part
            cache = directory / f"revised_{int(part['part']):03d}.json"
            if cache.exists():
                return json.loads(cache.read_text(encoding="utf-8"))
            # bounded like a repair: only the cards of the sections with loose ends are new
            node_of = {c["id"]: c["node"] for c in part["cards"]}
            record = by_part[part["part"]]
            at = {node_of.get(str(i["card"])) for i in loose[part["part"]]}
            scope = [s["node"] for s in record["sections"] if s["node"] in at] or None
            if scope and {c["node"] for c in part["cards"]} <= set(scope):
                scope = None
            self.p.log(f"[course] part {part['part']}: revising {len(loose[part['part']])} "
                       "loose end(s) found by the cold read"
                       + (f", in the cards of {scope_words(scope)} only" if scope else ""))
            only = (f" Rewrite only the cards of {scope_words(scope)}: the program keeps every "
                    "other card and the exercises word for word, so give them back unchanged."
                    if scope else "")
            feedback = ("A first-year reader who knows only the earlier parts read these cards "
                        "and was lost at the places below. Fix each one in the card named (define "
                        "the term, add the missing step or context, rewrite the confusing "
                        "sentence), using only the SCRIPT, and keep everything else as it is."
                        + only + "\n\n"
                        + json.dumps({"cards": part["cards"], "exercises": part.get("exercises",
                                                                                    [])},
                                     ensure_ascii=False)
                        + "\n\nLoose ends:\n" + "\n".join(
                            f"- card {i['card']} ({i['kind']}): {i['detail']}"
                            for i in loose[part["part"]]))
            try:
                result = self._process(outline, outline.node(part["part"]), record,
                                       start=part, feedback=feedback, tries=1, scope=scope)
            except Exception as exc:  # a failed revision keeps the reviewed cards
                self.p.log(f"[course] revision of part {part['part']}: {exc}")
                return part
            result["revision_checked"] = True
            cache.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            return result

        parts = self.p._map(one, parts)
        changed = [p for p in parts if p.get("revised")]
        if changed:  # read again what changed, so the report shows the loose ends that remain
            again = {c["part"]: c for c in self.cold_read(changed, tag="cold_revised")}
            cold = [dict(again[c["part"]], before=len(c["loose"])) if c["part"] in again else c
                    for c in cold]
        return parts, cold

    def audit_judge(self, outline: Outline, parts: list[dict], by_part: dict) -> dict:
        """Empty, in an accepted part, the one card that teaches an idea (it keeps its title
        and its claim to the idea), and ask the judge again. The judge's own verdict is scored,
        not the code checks after it: a judge that still calls the idea taught is passing what
        it should refuse."""
        cache = self.p._dir("course") / "audit.json"
        if cache.exists():
            return json.loads(cache.read_text(encoding="utf-8"))
        result = {"skipped": "no accepted part has an idea taught by a single card"}
        for part in parts:
            if part["flagged"]:
                continue
            claims = {}
            for card in part["cards"]:
                for idea in card.get("ideas", []):
                    claims.setdefault(idea, []).append(card["id"])
            target = next((i for i in part.get("ideas", []) if i["status"] == "taught"
                           and len(i["cards"]) == 1 and claims.get(i["idea"]) == i["cards"]), None)
            if target is None:
                continue
            node = outline.node(part["part"])
            ctx = self._context(outline, node, by_part[part["part"]])
            ctx["slides"] = " ".join(self.p.slides) if self.p.slides else ""
            hollow = [c if c["id"] != target["cards"][0] else dict(
                c, plain="", definition="", instance="", explanation=[c["title"]], formulas=[],
                notation=[], example=None, proof=[], diagram=None, code=[], table=None,
                checks=[])
                for c in part["cards"]]
            self.p.log(f"[audit] emptying card {target['cards'][0]} to test the course judge")
            try:
                _, ideas = self._judge(ctx, by_part[part["part"]], outline, hollow)
            except Exception as exc:  # a failed audit must not stop the build; retried next run
                self.p.log(f"[audit] course judge: {exc}")
                return {"skipped": f"the test failed ({exc}); it runs again next time"}
            verdict = next((i["judged"] for i in ideas if i["idea"] == target["idea"]), None)
            result = {"part": part["part"], "card": target["cards"][0], "idea": target["idea"],
                      "caught": verdict == "missing"}
            break
        cache.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result


# --------------------------------------------------------------------- dry run

def _paragraphs(sentences: list[str], limit: int = MAX_PARAGRAPHS) -> list[str]:
    """The sentences as at most `limit` paragraphs (a long section of a real lecture has more
    sentences than a card may have paragraphs)."""
    size = -(-len(sentences) // limit)
    return [" ".join(sentences[k:k + size]) for k in range(0, len(sentences), size)]


def dry_course(user: str) -> str:
    """Stand-in for the course writer in smoke tests: one card per section, one sentence each."""
    termbase = []
    marker = "## TERMBASE FOR THIS PART (JSON)\n"
    if marker in user:
        termbase = json.loads(user.split(marker, 1)[1].split("\n\n## ", 1)[0])
    owed = [t for t in termbase if t["define_here"]]
    ideas = []
    marker = "## IDEAS TO TEACH (JSON)\n"
    if marker in user:
        ideas = json.loads(user.split(marker, 1)[1].split("\n\n## ", 1)[0])
    script = user.split("## SCRIPT\n", 1)[1]
    sections, current = [], None
    for line in script.splitlines():
        heading = re.match(r"^#{3,5} (\d+(?:\.\d+)*) ", line)
        if heading:
            current = {"node": heading.group(1), "lines": []}
            sections.append(current)
            continue
        sentence = re.match(r"^(\d+)\. \[[^\]]*\] (.*)$", line)
        if sentence and current:
            current["lines"].append((int(sentence.group(1)), sentence.group(2)))
    cards = []
    for section in sections:
        if not section["lines"]:
            continue
        card = {"node": section["node"], "kind": "concept", "title": f"Section {section['node']}",
                "plain": section["lines"][0][1], "terms": [], "definition": "", "instance": "",
                "explanation": _paragraphs([text for _, text in section["lines"]]), "formulas": [],
                "notation": [], "example": None, "diagram": None, "code": [], "table": None,
                "checks": [], "covers": [n for n, _ in section["lines"]]}
        if owed:
            term = owed.pop(0)
            card.update(kind="definition", terms=[term["term"]], definition=term["definition"],
                        checks=[{"question": f"What is {term['term']}?",
                                 "answer": term["definition"]}])
        cards.append(card)
    for term in owed:  # terms of a part whose sections are empty
        cards[-1]["terms"].append(term["term"])
    nodes = [c["node"] for c in cards]
    for idea in ideas if cards else []:  # the card of the idea's node, else the first card
        place = next((k for k, n in enumerate(nodes) if n in map(str, idea.get("nodes", []))), 0)
        cards[place].setdefault("ideas", []).append(idea["id"])
    return json.dumps({"cards": cards, "exercises": []})


def dry_course_check(user: str) -> str:
    cards = json.loads(user.split("## CARDS (JSON)\n", 1)[1])
    ideas = []
    if "## IDEAS (JSON)\n" in user:
        ideas = [{"idea": i["id"], "status": "taught", "detail": "",
                  "cards": [c["card"] for c in cards if i["id"] in c.get("ideas", [])]}
                 for i in json.loads(user.split("## IDEAS (JSON)\n", 1)[1].split("\n\n## ", 1)[0])]
    exercises = []
    if "## EXERCISES (JSON)\n" in user:
        exercises = [{"exercise": e["exercise"], "status": "correct", "detail": ""} for e in
                     json.loads(user.split("## EXERCISES (JSON)\n", 1)[1].split("\n\n## ", 1)[0])]
    return json.dumps({"cards": [{"card": c["card"], "status": "faithful", "detail": ""}
                                 for c in cards], "ideas": ideas, "exercises": exercises})


# ---------------------------------------------------------------------- report

def course_report(course: dict, stats: dict, language: str = "en") -> str:
    """The section of report.md about the course material."""
    parts = course["parts"]
    cards = [c for p in parts for c in p["cards"]]
    lines = ["", "## Course material", ""]
    lines.append(f"The course material has {stats['cards']} cards and {stats['diagrams']} "
                 f"diagrams, written as {stats['notes']} notes of a Markdown vault. Every sentence "
                 f"of the verified script is covered by at least one card, unless a part is listed "
                 f"below as having open problems.")
    exercises = sum(len(p.get("exercises", [])) for p in parts)
    if exercises:
        lines.append(f"{exercises} practice exercise(s) with worked solutions close the parts, "
                     "each checked by the judge against the script.")
    revised = [p["part"] for p in parts if p.get("revised")]
    if revised:
        lines.append(f"Parts {', '.join(revised)} were revised once from the loose ends of the "
                     "cold read; a revision is kept only when it has no more problems than the "
                     "cards it replaces.")
    lines += card_repair_lines(parts)
    external = [c for c in cards if c["origin"] == "external"]
    lines.append(f"{len(external)} card(s) teach facts from outside the lecture and are marked "
                 "as such in the notes.")
    illustrative = [c for c in cards if c["illustrative"]]
    if illustrative:
        lines.append(f"{len(illustrative)} card(s) contain an instance or an example whose "
                     "numbers are not in the script; the notes mark them as illustrations added "
                     "by the course material.")
    loose = [(c["part"], i) for c in course.get("cold", []) for i in c["loose"]]
    before = sum(c.get("before", len(c["loose"])) for c in course.get("cold", []))
    if before > len(loose):
        lines.append(f"The cold read found {before} loose end(s) before the revision round and "
                     f"{len(loose)} after it.")
    if loose:
        lines += ["", "A cold read of the notes, by a reader who knows only the earlier parts, "
                  f"found {len(loose)} loose end(s) that remain:", ""]
        lines += [f"- Card {i['card']} ({i['kind']}): {i['detail']}" for _, i in loose]
        lines.append("")
    elif course.get("cold"):
        lines.append("A cold read of the notes, by a reader who knows only the earlier parts, "
                     "found no loose end.")
    audit = course.get("audit")
    if audit and audit.get("skipped"):
        lines.append(f"The test of the course judge was skipped: {audit['skipped']}.")
    elif audit:
        lines.append(f"The test of the course judge emptied card {audit['card']}, the only card "
                     f"that teaches idea {audit['idea']}, and asked the judge again; "
                     + ("it reported the idea as missing." if audit["caught"] else
                        "**it still called the idea taught**, so its verdicts on ideas are less "
                        "trustworthy; consider a stronger or a different checker model "
                        "(--verify-model, --verify-llm)."))
    for clash in course["clashes"]:
        lines.append(f"Notation clash: {clash}")
    flagged = [p for p in parts if p["flagged"]]
    if flagged:
        lines += ["", "Parts of the course material with open problems after the retries:", ""]
        for part in flagged:
            lines.append(f"- Part {part['part']} ({part['title']}), {part['blocking']} problem(s):")
            for issue in [i for i in part["issues"] if i["type"] not in SOFT][:12]:
                lines.append(f"  - ({issue['type']}) {issue['detail']}")
    else:
        lines.append("No part of the course material has open problems.")
    return "\n".join(lines) + "\n"
