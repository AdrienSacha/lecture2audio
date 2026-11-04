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

import json
import re
from pathlib import Path

from pydantic import ValidationError

from . import enrich
from .course_schema import Card, Cards, describe
from .outline import Node, Outline, render_outline
from .pipeline import (_NUMBER, SOFT_ISSUES, TextPipeline, _section, blocking, format_feedback,
                       sentences_of, unsupported_numbers)

REVIEW_PER_PART = 3
LOOSE_KINDS = {"undefined_term", "missing_prerequisite", "skipped_step", "forward_reference",
               "undefined_symbol", "ambiguous"}
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

def _texts(card: dict) -> list[str]:
    """The prose of a card, for the checks on wording."""
    texts = [card["title"], card["definition"], card["instance"], *card["explanation"],
             *card["formulas"]]
    if card.get("example"):
        texts += [card["example"]["title"], *card["example"]["steps"]]
    for check in card["checks"]:
        texts += [check["question"], check["answer"]]
    if card.get("diagram"):
        texts += [card["diagram"]["title"], *(f["caption"] for f in card["diagram"]["frames"])]
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
            issues.append(_issue("uncovered", f'script sentence {number} is not covered by any '
                                              f'card: "{script[number - 1]["text"][:90]}"'))
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
                  [card["definition"], *card["explanation"], *card["formulas"]] if t]
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


def validate_judgement(data, count: int, idea_ids=()) -> list[str]:
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
        if card["definition"]:
            out.append(f"Definition: {card['definition']}")
        if card["instance"]:
            out.append(f"Instance: {card['instance']}")
        out += card["explanation"]
        out += [f"Formula: {f}" for f in card["formulas"]]
        out += [f"Symbol {n['symbol']}: {n['meaning']}" for n in card["notation"]]
        if card["example"]:
            out += [f"Example, {card['example']['title']}:"] + card["example"]["steps"]
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
                           json.dumps([{k: i[k] for k in ("id", "kind", "idea")}
                                       for i in ctx["ideas"]], ensure_ascii=False, indent=1)
                           if ctx["ideas"] else "")
                + _section("FEEDBACK", feedback)
                + "## SCRIPT\n" + self._numbered(outline, record))
        data = self.p._ask_json(self.p.rewrite_llm, self.p.prompts.course, user,
                                lambda d: validate_cards(d, ctx), f"course of part {part.id}")
        return [normalise(c) for c in data["cards"]]

    def _judge(self, ctx: dict, record: dict, outline: Outline,
               cards: list[dict]) -> tuple[list[dict], list[dict]]:
        """The issues, and for each idea of the part its status and the numbers of the cards
        that teach it (the judge's, else the cards that claim the idea)."""
        part = ctx["part"]
        shown = [{"card": k, "title": c["title"], "terms": c["terms"],
                  "definition": c["definition"], "explanation": c["explanation"],
                  "formulas": c["formulas"], "instance": c["instance"], "example": c["example"],
                  "diagram": c["diagram"], "table": c["table"], "checks": c["checks"],
                  "covers": c["covers"], "ideas": c["ideas"]} for k, c in enumerate(cards, 1)]
        idea_ids = [i["id"] for i in ctx["ideas"]]
        user = (f"## LANGUAGE\n{self.p.language_name}\n\n## SCRIPT\n"
                + self._numbered(outline, record) + "\n\n"
                + _section("IDEAS (JSON)", json.dumps(
                    [{k: i[k] for k in ("id", "kind", "idea")} for i in ctx["ideas"]],
                    ensure_ascii=False, indent=1) if ctx["ideas"] else "")
                + "## CARDS (JSON)\n" + json.dumps(shown, ensure_ascii=False, indent=1))
        data = self.p._ask_json(self.p.verify_llm, self.p.prompts.course_check, user,
                                lambda d: validate_judgement(d, len(cards), idea_ids),
                                f"check of the course of part {part.id}")
        issues = []
        for item in data["cards"]:
            kind = JUDGE_STATUSES[item["status"]]
            if kind and 1 <= item["card"] <= len(cards):
                issues.append(_issue(kind, str(item.get("detail") or item["status"]),
                                     item["card"], cards[item["card"] - 1]["node"]))
        by_id = {str(i["id"]): i for i in ctx["ideas"]}
        ideas = []
        for item in data.get("ideas", []) if idea_ids else []:
            idea = by_id.get(str(item.get("idea"))) if isinstance(item, dict) else None
            if idea is None:
                continue
            numbers = [n for n in item.get("cards") or [] if 1 <= n <= len(cards)] or \
                [k for k, c in enumerate(cards, 1) if str(idea["id"]) in c["ideas"]]
            ideas.append({"idea": idea["id"], "status": item["status"],
                          "cards": numbers if item["status"] == "taught" else []})
            if item["status"] == "missing":
                issues.append(_issue("idea_missing", f'the lecture idea {idea["id"]} ({idea["kind"]}: '
                                                     f'{idea["idea"]}) is not taught in any card.',
                                     None, str(idea["nodes"][0])))
        return issues, ideas

    def _process(self, outline: Outline, part: Node, record: dict) -> dict:
        ctx = self._context(outline, part, record)
        ctx["slides"] = " ".join(self.p.slides) if self.p.slides else ""
        language = self.p.language
        best, feedback, attempts = None, "", 0
        for _ in range(self.p.max_retries + 1):
            attempts += 1
            cards = self._write(outline, ctx, record, feedback)
            judged, ideas = self._judge(ctx, record, outline, cards)
            issues = card_issues(cards, ctx, language) + judged
            count = len([i for i in issues if i["type"] not in SOFT])
            if best is None or count < best["blocking"]:
                best = {"cards": cards, "issues": issues, "blocking": count, "ideas": ideas}
            if best["blocking"] == 0:
                break
            more = attempts <= self.p.max_retries
            self.p.log(f"[course] part {part.id}: {best['blocking']} problem(s), "
                       + ("rewriting with feedback" if more else "keeping the best attempt"))
            feedback = format_feedback(json.dumps(best["cards"], ensure_ascii=False),
                                       [i for i in best["issues"] if i["type"] not in SOFT])
        source = " ".join(s["text"] for s in ctx["script"])
        cards = [dict(derive(c, ctx["script"], source, ctx["slides"], language),
                      id=f"{part.id}.{k}") for k, c in enumerate(best["cards"], 1)]
        # the ledger's view: which card (by id) teaches each idea of the part
        ideas = [dict(i, cards=[cards[n - 1]["id"] for n in i["cards"]]) for i in best["ideas"]]
        return {"part": part.id, "title": part.title, "cards": cards, "issues": best["issues"],
                "blocking": best["blocking"], "attempts": attempts,
                "flagged": best["blocking"] > 0, "ideas": ideas}

    def cold_read(self, parts: list[dict]) -> list[dict]:
        """A reader that knows only the earlier parts looks for loose ends, part by part."""
        directory = self.p._dir("course")

        def one(part: dict) -> dict:
            cache = directory / f"cold_{int(part['part']):03d}.json"
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

    def run(self, outline: Outline, records: list[dict]) -> dict:
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

        parts = self.p._map(one, outline.parts)
        notation, clashes = collect_notation(parts)
        (directory / "notation.json").write_text(json.dumps(notation, ensure_ascii=False,
                                                            indent=2), encoding="utf-8")
        return {"parts": parts, "notation": notation, "clashes": clashes,
                "review": build_review(parts), "cold": self.cold_read(parts)}


# --------------------------------------------------------------------- dry run

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
                "terms": [], "definition": "", "instance": "",
                "explanation": [text for _, text in section["lines"]], "formulas": [],
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
    return json.dumps({"cards": cards})


def dry_course_check(user: str) -> str:
    cards = json.loads(user.split("## CARDS (JSON)\n", 1)[1])
    ideas = []
    if "## IDEAS (JSON)\n" in user:
        ideas = [{"idea": i["id"], "status": "taught", "detail": "",
                  "cards": [c["card"] for c in cards if i["id"] in c.get("ideas", [])]}
                 for i in json.loads(user.split("## IDEAS (JSON)\n", 1)[1].split("\n\n## ", 1)[0])]
    return json.dumps({"cards": [{"card": c["card"], "status": "faithful", "detail": ""}
                                 for c in cards], "ideas": ideas})


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
    external = [c for c in cards if c["origin"] == "external"]
    lines.append(f"{len(external)} card(s) teach facts from outside the lecture and are marked "
                 "as such in the notes.")
    illustrative = [c for c in cards if c["illustrative"]]
    if illustrative:
        lines.append(f"{len(illustrative)} card(s) contain an instance or an example whose "
                     "numbers are not in the script; the notes mark them as illustrations added "
                     "by the course material.")
    loose = [(c["part"], i) for c in course.get("cold", []) for i in c["loose"]]
    if loose:
        lines += ["", "A cold read of the notes, by a reader who knows only the earlier parts, "
                  f"found {len(loose)} loose end(s):", ""]
        lines += [f"- Card {i['card']} ({i['kind']}): {i['detail']}" for _, i in loose]
        lines.append("")
    elif course.get("cold"):
        lines.append("A cold read of the notes, by a reader who knows only the earlier parts, "
                     "found no loose end.")
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
