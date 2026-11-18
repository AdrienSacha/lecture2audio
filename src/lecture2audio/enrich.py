"""Gaps and terminology: plain code around the coverage, termbase, research and terms prompts.

- Coverage: every slide idea gets a status (covered, partial, missing) and every gap in the
  transcript (slide idea missing, term never defined, skipped step, fundamental assumed) is
  listed with the node it belongs to.
- Termbase: one entry per technical term, with one definition, the terms that definition relies
  on, and the terms it must not be confused with. Code checks that every term a definition relies
  on has its own entry (so nothing fundamental is assumed), that there is no cycle, and decides
  where each term is defined: at its first use, or earlier if a term defined earlier relies on it.
- Research: a fact found online is kept only if its quote is found word for word in a page the
  program could read (web.quote_found).
- Terminology in the script: where each term occurs, which words are used where they should not
  be, and the gate on the terminology judge (one status per occurrence).
"""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .outline import Outline
from .web import FetchError, fetch_page, quote_found, same_page

IDEA_KINDS = {"definition", "claim", "step", "formula", "condition", "example", "warning"}
GAP_KINDS = {"slide_missing", "undefined_term", "skipped_step", "assumed_fundamental"}
TERM_GAPS = {"undefined_term", "assumed_fundamental"}
IDEA_STATUSES = {"covered", "partial", "missing"}
USE_STATUSES = {"correct", "wrong"}


def node_order(outline: Outline) -> list[str]:
    return [node.id for part in outline.parts for node in part.walk()]


def part_of(node_id: str) -> int:
    return int(str(node_id).split(".")[0])


# ------------------------------------------------------------------- coverage

class _Idea(BaseModel):
    model_config = ConfigDict(extra="ignore")
    kind: Literal["definition", "claim", "step", "formula", "condition", "example", "warning"]
    idea: str = Field(min_length=1)
    nodes: list[str] = Field(min_length=1)
    units: list[int] = Field(min_length=1)

    @field_validator("idea")
    @classmethod
    def _stripped(cls, value):
        if not value.strip():
            raise ValueError("an idea is one non-empty sentence")
        return value.strip()

    @field_validator("nodes", mode="before")
    @classmethod
    def _node_ids(cls, value):
        return [str(v) for v in value] if isinstance(value, list) else value


class _NoContent(BaseModel):
    model_config = ConfigDict(extra="ignore")
    units: list[int] = Field(min_length=1)
    kind: Literal["no_teaching", "unclear"] = "no_teaching"
    reason: str = ""


class IdeaList(BaseModel):
    """The answer of the idea listing. No minimum count anywhere: a schema minimum makes
    models pad their lists with real but irrelevant text (Barali and Paudel, 2026)."""
    model_config = ConfigDict(extra="ignore")
    ideas: list[_Idea]
    no_content: list[_NoContent] = []


def validate_idea_list(data, part, extra_units=()) -> list[str]:
    """The gate on one part's list of atomic ideas: the schema (IdeaList), then node ids and
    unit numbers of this part. Whether every unit is accounted for is not a format error: it
    is checked by `unaccounted_units` and asked again with feedback that says fewer is fine.
    `extra_units` are units added to the part for an audit."""
    try:
        parsed = IdeaList.model_validate(data)
    except ValidationError as exc:
        return [f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:8]]
    nodes = {node.id for node in part.walk()}
    units = set(part.all_units()) | set(extra_units)
    span = f"{min(units)} to {max(units)}" if units else "none"
    errors = []
    for k, idea in enumerate(parsed.ideas, 1):
        if any(n not in nodes for n in idea.nodes):
            errors.append(f'idea {k}: "nodes" must be ids from {sorted(nodes)}')
        if any(u not in units for u in idea.units):
            errors.append(f'idea {k}: "units" must be unit numbers of this part ({span})')
    for k, item in enumerate(parsed.no_content, 1):
        if any(u not in units for u in item.units):
            errors.append(f'no_content {k}: "units" must be unit numbers of this part ({span})')
    return errors[:12]


def unaccounted_units(data, part) -> list[int]:
    """Units of the part that neither an idea nor "no_content" accounts for."""
    parsed = IdeaList.model_validate(data)
    seen = {u for i in parsed.ideas for u in i.units} | {u for n in parsed.no_content
                                                         for u in n.units}
    return [u for u in part.all_units() if u not in seen]


def validate_coverage(data, node_ids: list[str], slide_count: int,
                      blank: frozenset | set = frozenset()) -> list[str]:
    """`blank`: slide numbers with no text (an animation step merged into the next page, a
    page OCR could not read); the model is not shown them, so no idea is asked for them."""
    if not isinstance(data, dict) or not isinstance(data.get("slide_ideas"), list) \
            or not isinstance(data.get("gaps"), list):
        return ['the answer must be a JSON object with a "slide_ideas" list and a "gaps" list']
    errors, ids, seen_slides = [], set(node_ids), set()
    idea_ids = set()
    for k, idea in enumerate(data["slide_ideas"], 1):
        if not isinstance(idea, dict) or not str(idea.get("idea", "")).strip() \
                or not str(idea.get("id", "")).strip():
            errors.append(f'slide idea {k} must have "id" and "idea"')
            continue
        idea_ids.add(str(idea["id"]))
        if not isinstance(idea.get("slide"), int) or not 1 <= idea["slide"] <= slide_count:
            errors.append(f'slide idea {idea["id"]}: "slide" must be a slide number from 1 to '
                          f"{slide_count}")
        else:
            seen_slides.add(idea["slide"])
        if idea.get("status") not in IDEA_STATUSES:
            errors.append(f'slide idea {idea["id"]}: "status" must be one of '
                          f"{sorted(IDEA_STATUSES)}")
        nodes = idea.get("nodes")
        if not isinstance(nodes, list) or any(str(n) not in ids for n in nodes):
            errors.append(f'slide idea {idea["id"]}: "nodes" must list node ids from {node_ids}')
        elif idea.get("status") in ("covered", "partial") and not nodes:
            errors.append(f'slide idea {idea["id"]} is {idea["status"]}: name the node(s) '
                          "that cover it")
    empty = [n for n in range(1, slide_count + 1) if n not in seen_slides and n not in blank]
    if empty and slide_count:
        errors.append(f"slides {empty} have no idea listed: list the ideas of every slide "
                      "(a title slide gets one idea with status covered)")
    ideas = data.get("transcript_ideas", [])
    if not isinstance(ideas, list):
        errors.append('"transcript_ideas" must be a list')
        ideas = []
    seen_ideas = set()
    for k, idea in enumerate(ideas, 1):
        if not isinstance(idea, dict) or not str(idea.get("id", "")).strip() \
                or not str(idea.get("idea", "")).strip():
            errors.append(f'transcript idea {k} must have "id" and "idea"')
            continue
        where = f'transcript idea {idea["id"]}'
        if idea["id"] in seen_ideas:
            errors.append(f"{where}: the id is used twice")
        seen_ideas.add(idea["id"])
        if idea.get("kind") not in IDEA_KINDS:
            errors.append(f'{where}: "kind" must be one of {sorted(IDEA_KINDS)}')
        nodes = idea.get("nodes")
        if not isinstance(nodes, list) or not nodes or any(str(n) not in ids for n in nodes):
            errors.append(f'{where}: "nodes" must list one or more node ids from {node_ids}')
    gap_ids, named = set(), set()
    for k, gap in enumerate(data["gaps"], 1):
        if not isinstance(gap, dict) or not str(gap.get("id", "")).strip():
            errors.append(f'gap {k} must be an object with an "id"')
            continue
        where = f'gap {gap["id"]}'
        if gap["id"] in gap_ids:
            errors.append(f"{where}: the id is used twice")
        gap_ids.add(gap["id"])
        if gap.get("kind") not in GAP_KINDS:
            errors.append(f'{where}: "kind" must be one of {sorted(GAP_KINDS)}')
        if str(gap.get("node")) not in ids:
            errors.append(f'{where}: "node" must be one of {node_ids}')
        for field in ("detail", "query"):
            if not str(gap.get(field, "")).strip():
                errors.append(f'{where} needs a non-empty "{field}"')
        if gap.get("kind") in TERM_GAPS and not str(gap.get("term", "")).strip():
            errors.append(f'{where}: a gap of kind {gap.get("kind")} must name its "term"')
        if gap.get("kind") == "slide_missing":
            if str(gap.get("idea")) not in idea_ids:
                errors.append(f'{where}: "idea" must be the id of a slide idea')
            named.add(str(gap.get("idea")))
    for idea in data["slide_ideas"]:
        if isinstance(idea, dict) and idea.get("status") in ("partial", "missing") \
                and str(idea.get("id")) not in named:
            errors.append(f'slide idea {idea.get("id")} is {idea["status"]}: add a gap of kind '
                          '"slide_missing" for it')
    return errors


# ------------------------------------------------------------------- termbase

def term_pattern(term: str) -> re.Pattern:
    """The term as a whole word, case-insensitive, with an optional plural ending."""
    words = [re.escape(w) for w in str(term).split()]
    return re.compile(r"(?<![\w-])" + r"[\s-]+".join(words) + r"(?:s|es|x)?(?![\w-])", re.I)


def _key(term: str) -> str:
    return " ".join(str(term).lower().split())


def validate_termbase(data, node_ids: list[str], required: list[str]) -> list[str]:
    if not isinstance(data, dict) or not isinstance(data.get("terms"), list):
        return ['the answer must be a JSON object with a "terms" list']
    errors, names = [], {}
    for k, item in enumerate(data["terms"], 1):
        if not isinstance(item, dict) or not str(item.get("term", "")).strip() \
                or not str(item.get("definition", "")).strip():
            errors.append(f'term {k} must have a non-empty "term" and "definition"')
            continue
        key = _key(item["term"])
        if key in names:
            errors.append(f'"{item["term"]}" appears twice: one entry per term')
        names[key] = item
        for field in ("depends_on", "confusable"):
            if not isinstance(item.get(field, []), list) or any(
                    not isinstance(x, str) or not x.strip() for x in item.get(field, [])):
                errors.append(f'"{item["term"]}": "{field}" must be a list of terms')
        if str(item.get("first_node")) not in node_ids:
            errors.append(f'"{item["term"]}": "first_node" must be one of {node_ids}')
    if errors:
        return errors
    for item in data["terms"]:
        for dep in item.get("depends_on", []):
            if _key(dep) not in names:
                errors.append(f'the definition of "{item["term"]}" relies on "{dep}", which has '
                              "no entry: add an entry for it (fundamentals are made explicit) or "
                              "remove it from depends_on if it is an everyday word")
            if _key(dep) == _key(item["term"]):
                errors.append(f'"{item["term"]}" cannot depend on itself')
    for term in required:
        if _key(term) not in names:
            errors.append(f'the coverage step found that "{term}" is used without a definition: '
                          "add an entry for it")
    cycle = _cycle({k: [_key(d) for d in v.get("depends_on", [])] for k, v in names.items()})
    if cycle:
        errors.append("these definitions rely on each other in a circle: "
                      + " -> ".join(cycle) + ". Define one of them without the others")
    return errors


def _cycle(graph: dict[str, list[str]]) -> list[str]:
    state, stack = {}, []

    def visit(node):
        state[node] = 1
        stack.append(node)
        for nxt in graph.get(node, []):
            if state.get(nxt) == 1:
                return stack[stack.index(nxt):] + [nxt]
            if nxt in graph and not state.get(nxt):
                found = visit(nxt)
                if found:
                    return found
        stack.pop()
        state[node] = 2
        return None

    for node in graph:
        if not state.get(node):
            found = visit(node)
            if found:
                return found
    return []


def parse_glossary(glossary: str) -> dict[str, str]:
    """'term: definition' lines. A line without a definition only fixes the spelling."""
    out = {}
    for line in glossary.splitlines():
        term, sep, definition = line.partition(":")
        if sep and term.strip() and definition.strip():
            out[_key(term)] = definition.strip()
    return out


def build_termbase(data: dict, outline: Outline, glossary: str = "") -> list[dict]:
    """Final entries, in the order they are defined. The glossary's definitions win.

    `define_at` is the node where the term must be defined: its first use in the transcript
    (or the node the model gave, if earlier), moved earlier when a term defined before relies on
    it, so that every definition only uses terms already defined."""
    order = node_order(outline)
    index = {n: i for i, n in enumerate(order)}
    texts = {node.id: " ".join(outline.text(u) for u in node.own_units())
             for part in outline.parts for node in part.walk()}
    given = parse_glossary(glossary)
    entries = {}
    for item in data["terms"]:
        key = _key(item["term"])
        pattern = term_pattern(item["term"])
        first = next((i for i, n in enumerate(order) if pattern.search(texts[n])), len(order))
        entries[key] = {
            "term": item["term"].strip(),
            "definition": given.get(key, str(item["definition"]).strip()),
            "from_glossary": key in given,
            "depends_on": [d.strip() for d in item.get("depends_on", [])],
            "confusable": [c.strip() for c in item.get("confusable", [])],
            "in_lecture": first < len(order),
            "at": min(first, index[str(item["first_node"])]),
        }
    for key, entry in entries.items():  # "not to be confused with" goes both ways
        for other in entry["confusable"]:
            if _key(other) in entries and not any(
                    _key(c) == key for c in entries[_key(other)]["confusable"]):
                entries[_key(other)]["confusable"].append(entry["term"])
    for _ in range(len(entries)):  # pull dependencies before the terms that rely on them
        changed = False
        for entry in entries.values():
            for dep in entry["depends_on"]:
                target = entries[_key(dep)]
                if target["at"] > entry["at"]:
                    target["at"] = entry["at"]
                    changed = True
        if not changed:
            break
    result = []
    for entry in sorted(entries.values(), key=lambda e: (e["at"], e["term"].lower())):
        node = order[min(entry.pop("at"), len(order) - 1)]
        result.append(dict(entry, define_at=node, part=part_of(node)))
    return result


# ------------------------------------------------------------------- research

def validate_research(data) -> list[str]:
    if not isinstance(data, dict) or not isinstance(data.get("found"), bool) \
            or not isinstance(data.get("facts"), list):
        return ['the answer must be {"found": true or false, "facts": [...]}']
    for k, fact in enumerate(data["facts"], 1):
        if not isinstance(fact, dict) or not all(
                str(fact.get(f, "")).strip() for f in ("text", "url", "quote")):
            return [f'fact {k} must have non-empty "text", "url" and "quote"']
    return []


def check_facts(facts: list[dict], documents: list[dict], fetch=fetch_page) -> tuple[list, list]:
    """Keep the facts whose quote is found word for word in a page the provider returned or
    that the program fetched. Returns (kept, rejected with the reason)."""
    pages: dict[str, list[str]] = {}
    for doc in documents:
        pages.setdefault(doc["url"], [])
        if doc.get("text"):
            pages[doc["url"]].append(doc["text"])
    kept, rejected = [], []
    for fact in facts:
        url = str(fact["url"]).strip()
        known = [u for u in pages if same_page(u, url)]
        if not known:
            rejected.append(dict(fact, reason="the address is not among the pages the search "
                                              "returned"))
            continue
        texts = [t for u in known for t in pages[u]]
        if not quote_found(fact["quote"], texts):
            try:
                page = fetch(url)
                pages[known[0]].append(page)
                texts.append(page)
            except FetchError as exc:
                rejected.append(dict(fact, reason=f"the page could not be read ({exc})"))
                continue
        if quote_found(fact["quote"], texts):
            title = next((d.get("title") for d in documents
                          if same_page(d["url"], url) and d.get("title")), "")
            kept.append({"text": str(fact["text"]).strip(), "url": url, "title": title,
                         "quote": str(fact["quote"]).strip()})
        else:
            rejected.append(dict(fact, reason="the quote was not found word for word in the page"))
    return kept, rejected


# ----------------------------------------------------------- terminology in the script

def occurrences(sentences: list[dict], terms: list[dict]) -> list[dict]:
    """(sentence number, term) for every term of the termbase found in the script.
    A term inside a longer term (\"rate\" in \"bit rate\") is not counted twice."""
    found = []
    longest_first = sorted(terms, key=lambda t: -len(t["term"]))
    for number, sentence in enumerate(sentences, 1):
        text = sentence["text"]
        taken: list[tuple[int, int]] = []
        for term in longest_first:
            for match in term_pattern(term["term"]).finditer(text):
                span = match.span()
                if any(a < span[1] and span[0] < b for a, b in taken):
                    continue
                taken.append(span)
                found.append({"sentence": number, "term": term["term"]})
                break
    return sorted(found, key=lambda x: (x["sentence"], x["term"].lower()))


def validate_uses(data, pairs: list[dict]) -> list[str]:
    if not isinstance(data, dict) or not isinstance(data.get("uses"), list):
        return ['the answer must be a JSON object with a "uses" list']
    wanted = {(p["sentence"], _key(p["term"])) for p in pairs}
    given = set()
    for k, use in enumerate(data["uses"], 1):
        if not isinstance(use, dict) or not isinstance(use.get("sentence"), int) \
                or use.get("status") not in USE_STATUSES:
            return [f'use {k} must be {{"sentence": number, "term": "...", "status": '
                    f'"correct" or "wrong", "detail": "..."}}']
        given.add((use["sentence"], _key(use.get("term", ""))))
    missing = sorted(wanted - given)
    if missing:
        listed = ", ".join(f'sentence {s} "{t}"' for s, t in missing[:20])
        return [f"give one entry for every occurrence listed in OCCURRENCES; missing: {listed}"]
    return []


def stray_terms(sentences: list[dict], terms: list[dict], sources: str,
                allowed: set[str]) -> list[dict]:
    """Words that the termbase marks as not to be confused with a term, used in the script
    although the sources of this part never use them: likely a synonym swapped in."""
    issues = []
    for term in terms:
        for word in term["confusable"]:
            if _key(word) in allowed:
                continue
            pattern = term_pattern(word)
            if pattern.search(sources):
                continue
            for number, sentence in enumerate(sentences, 1):
                if pattern.search(sentence["text"]):
                    issues.append({
                        "type": "term_swap", "node": None, "sentence": number, "unit": None,
                        "detail": (f'"{word}" is used here, but the sources of this part never use '
                                   f'it. It is not a synonym of "{term["term"]}" ({term["definition"]}). '
                                   f'Use the exact term the lecture uses.')})
    unique = {(i["sentence"], i["detail"]): i for i in issues}
    return list(unique.values())
