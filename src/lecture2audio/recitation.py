"""The recitation notes: the lecture re-taught as a handout, typeset with LaTeX.

One model call per part writes blocks (prose, definitions, results, proofs, worked examples,
figures, tables), each naming the ideas of the ledger it teaches and the outside sources it
relies on. Code checks them (every idea of the part is claimed by a block, the idea's numbers
are in the blocks that claim it, every TikZ figure compiles alone), sends the blocks at fault
back once or twice, and then writes the document: the model never writes LaTeX outside math
and figures. Exercises and check questions come from the course stage, where a judge has
solved them again. Every link of the PDF (terms to the glossary, questions to answers,
citations to sources) is made by code, so each one points somewhere.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Annotated, Literal

from pydantic import Field, ValidationError, model_validator

from . import enrich, latex
from .course import CoursePipeline, reader_text
from .course_schema import HTML_ELEMENT, Model, Text, describe, html_in_text
from .outline import Node, Outline, render_outline
from .pipeline import (NumberIndex, PipelineError, TextPipeline, _and, _section,
                       format_feedback, numbers_lost, scope_words, sentences_of)

SOFT = ("idea_numbers", "source_unused", "definition_missing")
KINDS = ("prose", "definition", "result", "proof", "example", "intuition", "warning", "equation",
         "figure", "code", "table")


class Block(Model):
    kind: Literal[KINDS]
    node: Text
    title: Text = ""
    text: Text = ""
    steps: list[Text] = Field(default=[], max_length=24)
    latex: str = Field(default="", max_length=4000)
    tikz: str = Field(default="", max_length=8000)
    ascii: str = Field(default="", max_length=4000)
    language: Text = ""
    code: str = Field(default="", max_length=6000)
    columns: list[Text] = Field(default=[], max_length=8)
    rows: list[Annotated[list[Text], Field(max_length=8)]] = Field(default=[], max_length=40)
    caption: Text = ""
    ideas: list[Text] = []
    sources: list[Text] = []

    @model_validator(mode="after")
    def _shape(self):
        kind = self.kind
        if kind in ("prose", "definition", "result", "intuition", "warning") and not self.text:
            raise ValueError(f'a "{kind}" block has a "text"')
        if kind in ("definition", "result", "example") and not self.title:
            raise ValueError(f'a "{kind}" block has a "title"')
        if kind in ("proof", "example") and not self.steps:
            raise ValueError(f'a "{kind}" block has "steps"')
        if kind == "equation" and not self.latex.strip():
            raise ValueError('an "equation" block has "latex"')
        if kind == "figure":
            if bool(self.tikz.strip()) == bool(self.ascii.strip()):
                raise ValueError('a "figure" block has exactly one of "tikz" and "ascii"')
            if not self.caption:
                raise ValueError('a "figure" block has a "caption"')
            if HTML_ELEMENT.search(self.ascii) or "```" in self.ascii:
                raise ValueError("an ASCII drawing holds no HTML and no ``` fence")
        if kind == "code" and not self.code.strip():
            raise ValueError('a "code" block has "code"')
        if kind == "table" and (not self.columns or not self.rows
                                or any(len(r) != len(self.columns) for r in self.rows)):
            raise ValueError('a "table" block has "columns" and "rows" of the same width')
        texts = prose_of(self.model_dump())
        if any(html_in_text(t) for t in texts):
            raise ValueError("write plain Markdown: no HTML tags in the text")
        errors = [e for t in texts for e in latex.math_errors(t)]
        if self.latex.strip() and (why := latex.tex_error(self.latex)):
            errors.append(f"latex: {why}")
        if errors:
            raise ValueError("math is LaTeX the program can typeset: " + "; ".join(errors[:4]))
        self.ideas = list(dict.fromkeys(i for i in self.ideas if i))
        self.sources = list(dict.fromkeys(s for s in self.sources if s))
        return self


class Section(Model):
    """The notes of one part."""
    hook: Text = Field(min_length=1)
    blocks: list[Block] = Field(min_length=1)
    takeaways: list[Text] = Field(min_length=1, max_length=8)
    pitfalls: list[Text] = Field(default=[], max_length=6)


def prose_of(block: dict) -> list[str]:
    """The text fields of a block that hold prose (escaped by the program, math checked)."""
    return [block.get("title", ""), block.get("text", ""), block.get("caption", ""),
            *block.get("steps", []), *block.get("columns", []),
            *(c for r in block.get("rows", []) for c in r)]


def text_of(block: dict) -> str:
    """Everything a block says, for the number check."""
    return "\n".join([*prose_of(block), block.get("latex", ""), block.get("tikz", ""),
                      block.get("ascii", ""), block.get("code", "")])


def validate_section(data, ctx: dict) -> list[str]:
    """The gate on the model's JSON: the shape, nodes of this part in order, ideas and sources
    that exist."""
    try:
        section = Section.model_validate(data)
    except ValidationError as exc:
        return describe(exc)
    errors, order, last = [], {n: k for k, n in enumerate(ctx["nodes"])}, 0
    for k, block in enumerate(section.blocks, 1):
        if block.node not in order:
            errors.append(f'blocks.{k}: "node" is one of {ctx["nodes"]}')
            continue
        if order[block.node] < last:
            errors.append(f"blocks.{k}: blocks follow the order of NODES; node {block.node} "
                          "comes after a later node")
        last = max(last, order[block.node])
        if unknown := [i for i in block.ideas if i not in ctx["idea_ids"]]:
            errors.append(f'blocks.{k}: "ideas" holds ids of IDEAS TO TEACH; unknown: {unknown}')
        if unknown := [s for s in block.sources if s not in ctx["source_ids"]]:
            errors.append(f'blocks.{k}: "sources" holds ids of SOURCES; unknown: {unknown}')
    extra = [e for t in [section.hook, *section.takeaways, *section.pitfalls]
             for e in latex.math_errors(t)]
    return errors[:8] + extra[:4]


def _issue(kind: str, detail: str, node: str | None = None, block: int | None = None) -> dict:
    return {"type": kind, "detail": detail, "node": node, "block": block}


def section_issues(section: dict, ctx: dict, language: str, figure_check=latex.figure_error) -> list[dict]:
    """What code finds in a section that passed the gate. Blocking: an idea no block claims,
    a figure that does not compile. Soft (reported): an idea whose numbers are not in the
    blocks that claim it, an outside source no block cites, a term owed and not defined."""
    issues, blocks = [], section["blocks"]
    source = NumberIndex(ctx["source"], language)
    for idea in ctx["ideas"]:
        claiming = [b for b in blocks if str(idea["id"]) in b["ideas"]]
        node = str(idea["nodes"][0]) if idea.get("nodes") else None
        if not claiming:
            issues.append(_issue("idea_uncited", f'No block teaches the lecture idea {idea["id"]} '
                                                 f'({idea.get("kind", "idea")}: {idea["idea"]}). '
                                                 'Teach it and list its id in the block\'s '
                                                 '"ideas".', node))
            continue
        held = NumberIndex("\n".join(text_of(b) for b in claiming), language)
        if lost := numbers_lost(idea["idea"], source, held):
            issues.append(_issue("idea_numbers", f'The blocks that claim the idea {idea["id"]} '
                                                 f'({idea["idea"]}) do not contain {_and(lost)}, '
                                                 "which the lecture gives.", node))
    for k, block in enumerate(blocks, 1):
        if block["kind"] == "figure" and block["tikz"].strip():
            if why := figure_check(block["tikz"]):
                issues.append(_issue("figure", f'The TikZ figure "{block["caption"]}" cannot be '
                                               f"used: {why}.", block["node"], k))
    cited = {s for b in blocks for s in b["sources"]}
    for source_id in ctx["source_ids"]:
        if source_id in ctx["used_sources"] and source_id not in cited:
            issues.append(_issue("source_unused", f"The script uses the outside source "
                                                  f'{source_id}, and no block lists it in '
                                                  '"sources".'))
    defined = {enrich._key(b["title"]) for b in blocks if b["kind"] == "definition"}
    for term in ctx["termbase"]:
        if term["define_here"] and enrich._key(term["term"]) not in defined:
            issues.append(_issue("definition_missing", f'The term "{term["term"]}" is to be '
                                                       'defined in this part, in a "definition" '
                                                       "block whose title is the term."))
    return issues


def blocking(issues: list[dict]) -> list[dict]:
    return [i for i in issues if i["type"] not in SOFT]


def splice(best: dict, new: dict, scope: list[str], nodes: list[str]) -> dict:
    """A bounded repair: the blocks of the nodes in `scope` come from the new attempt, every
    other block and the hook, takeaways and pitfalls stay as they were, word for word."""
    blocks = []
    for node in nodes:
        origin = new if node in scope else best
        blocks += [b for b in origin["blocks"] if b["node"] == node]
    return dict(best, blocks=blocks)


class RecitationPipeline:
    """Runs after the script (and the course material, when it is made): reuses the models,
    prompts, caches and logging of TextPipeline."""

    def __init__(self, pipeline: TextPipeline, figure_check=latex.figure_error):
        self.p = pipeline
        self._figures: dict[str, str | None] = {}
        self._check = figure_check

    def _figure(self, tikz: str) -> str | None:
        key = hashlib.sha1(tikz.encode()).hexdigest()
        if key not in self._figures:  # an unchanged figure is not compiled twice
            self._figures[key] = self._check(tikz)
        return self._figures[key]

    def _context(self, outline: Outline, part: Node, record: dict, cards: dict | None) -> dict:
        script = sentences_of(record)
        nodes = [s["node"] for s in record["sections"]]
        pid = int(part.id)
        text = " ".join(s["text"] for s in script)
        ideas = [i for i in self.p.coverage.get("transcript_ideas", [])
                 if any(str(n) in nodes for n in i.get("nodes", []))]
        used = {s.get("source") for s in script if s["origin"] == "external"}
        return {"part": part, "nodes": nodes, "source": record["source"], "ideas": ideas,
                "idea_ids": {str(i["id"]) for i in ideas},
                "sources": record.get("supplements", []),
                "source_ids": [s["id"] for s in record.get("supplements", [])],
                "used_sources": used, "cards": (cards or {}).get("cards", []),
                "termbase": [{"term": t["term"], "definition": t["definition"],
                              "define_here": t["part"] == pid}
                             for t in self.p.terms
                             if t["part"] == pid or (t["part"] < pid and enrich.term_pattern(
                                 t["term"]).search(text))]}

    def _write(self, outline: Outline, ctx: dict, record: dict, feedback: str) -> dict:
        part = ctx["part"]
        dumps = lambda value: json.dumps(value, ensure_ascii=False, indent=1) if value else ""
        user = (f"## LANGUAGE\n{self.p.language_name}\n\n"
                + _section("OUTLINE", render_outline(outline, part.id))
                + _section("NODES (JSON)", json.dumps(
                    [{"node": n, "title": outline.node(n).title} for n in ctx["nodes"]],
                    ensure_ascii=False))
                + _section("TERMBASE FOR THIS PART (JSON)", dumps(ctx["termbase"]))
                + _section("IDEAS TO TEACH (JSON)", dumps(
                    [{k: i[k] for k in ("id", "kind", "idea", "nodes") if k in i}
                     for i in ctx["ideas"]]))
                + _section("SOURCES (JSON)", dumps(
                    [{"id": s["id"], "title": s.get("title", ""), "text": s["text"]}
                     for s in ctx["sources"]]))
                + _section("COURSE NOTES", reader_text(ctx["cards"]) if ctx["cards"] else "")
                + _section("FEEDBACK", feedback)
                + "## SCRIPT\n" + CoursePipeline._numbered(outline, record))
        data = self.p._ask_json(self.p.rewrite_llm, self.p.prompts.recitation, user,
                                lambda d: validate_section(d, ctx),
                                f"recitation notes of part {part.id}")
        return Section.model_validate(data).model_dump(mode="json")

    def _process(self, outline: Outline, part: Node, record: dict, cards: dict | None) -> dict:
        ctx = self._context(outline, part, record, cards)
        best, feedback, scope, attempts, repairs = None, "", None, 0, []
        for _ in range(self.p.max_retries + 1):
            attempts += 1
            section = self._write(outline, ctx, record, feedback)
            if scope and best is not None:
                section = splice(best["section"], section, scope, ctx["nodes"])
            issues = section_issues(section, ctx, self.p.language, self._figure)
            count = len(blocking(issues))
            if best is not None:
                repairs.append({"attempt": attempts, "scope": scope or "all",
                                "before": best["blocking"], "after": count,
                                "kept": count < best["blocking"]})
            if best is None or count < best["blocking"]:
                best = {"section": section, "issues": issues, "blocking": count}
            if best["blocking"] == 0:
                break
            shown = blocking(best["issues"])
            nodes = sorted({i["node"] for i in shown if i["node"]}, key=ctx["nodes"].index)
            scope = nodes if nodes and all(i["node"] for i in shown) else None
            more = attempts <= self.p.max_retries
            self.p.log(f"[recitation] part {part.id}: {best['blocking']} problem(s), "
                       + ("keeping the best attempt" if not more else
                          f"rewriting the blocks of {scope_words(scope)} with feedback, keeping "
                          "the rest" if scope else "rewriting with feedback"))
            feedback = format_feedback(json.dumps(best["section"], ensure_ascii=False), shown,
                                       scope)
        section, issues = best["section"], best["issues"]
        # a figure that still fails would break the PDF: it is left out, and reported
        failing = {i["block"] for i in issues if i["type"] == "figure"}
        dropped = [section["blocks"][k - 1]["caption"] for k in sorted(failing)]
        section["blocks"] = [b for k, b in enumerate(section["blocks"], 1) if k not in failing]
        left = [i for i in blocking(issues) if i["type"] != "figure"]
        return {"part": part.id, "title": part.title, **section, "issues": issues,
                "blocking": len(left), "attempts": attempts, "repairs": repairs,
                "flagged": bool(left), "dropped_figures": dropped}

    def run(self, outline: Outline, records: list[dict], course: dict | None = None) -> list[dict]:
        """The notes of every part, cached in recitation/. A part whose notes keep failing the
        gate is left out of the document, named in the report and not cached."""
        by_part = {r["part"]: r for r in records}
        cards = {p["part"]: p for p in (course or {}).get("parts", [])}
        return self.p._map(lambda part: self.part_notes(outline, part, by_part[part.id],
                                                        cards.get(part.id)), outline.parts)

    def part_notes(self, outline: Outline, part: Node, record: dict,
                   cards: dict | None) -> dict:
        """The notes of one part (from its script and its final cards), cached in recitation/."""
        cache = self.p._dir("recitation") / f"part_{int(part.id):03d}.json"
        if cache.exists():
            return json.loads(cache.read_text(encoding="utf-8"))
        self.p.log(f"[recitation] part {part.id}/{len(outline.parts)}: {part.title}")
        try:
            result = self._process(outline, part, record, cards)
        except PipelineError as exc:
            self.p.log(f"[recitation] part {part.id}: {exc}")
            return {"part": part.id, "title": part.title, "error": str(exc)}
        cache.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result


def dry_recitation(user: str) -> str:
    """Stand-in for the writer in smoke tests: one prose block per section of the script, each
    idea on the block of its node."""
    ideas = []
    marker = "## IDEAS TO TEACH (JSON)\n"
    if marker in user:
        ideas = json.loads(user.split(marker, 1)[1].split("\n\n## ", 1)[0])
    blocks, current, first = [], None, ""
    for line in user.split("## SCRIPT\n", 1)[1].splitlines():
        heading = re.match(r"^#{3,5} (\d+(?:\.\d+)*) ", line)
        sentence = re.match(r"^\d+\. \[[^\]]*\] (.*)$", line)
        if heading:
            current = {"kind": "prose", "node": heading.group(1), "text": "", "ideas": [],
                       "sources": []}
            blocks.append(current)
        elif sentence and current is not None:
            current["text"] = (current["text"] + " " + sentence.group(1)).strip()
            first = first or sentence.group(1)
    blocks = [b for b in blocks if b["text"]]
    nodes = [b["node"] for b in blocks]
    for idea in ideas if blocks else []:
        place = next((k for k, n in enumerate(nodes) if n in map(str, idea.get("nodes", []))), 0)
        blocks[place]["ideas"].append(idea["id"])
    return json.dumps({"hook": first or "This part.", "blocks": blocks,
                       "takeaways": [first or "This part."], "pitfalls": []})


# ------------------------------------------------------------------- document

LABELS = {
    "en": {"kind": "Recitation notes", "glance": "At a glance", "definition": "Definition",
           "result": "Result", "example": "Example", "intuition": "Intuition",
           "warning": "Watch out", "proof": "Proof", "takeaways": "Key takeaways",
           "pitfalls": "Common pitfalls", "checks": "Check yourself", "exercises": "Exercises",
           "exercise": "Exercise", "answer": "Answer", "solution": "Solution",
           "solutions": "Answers and solutions", "back": "Back to the question",
           "notation": "Notation", "symbol": "Symbol", "meaning": "Meaning",
           "glossary": "Glossary", "references": "References", "about": "About these notes",
           "passage": "Supporting passage", "outside": "From outside the lecture",
           "relies": "Relies on", "confusable": "Not to be confused with", "section": "Section",
           "from": {"slides": "Built from the slides alone: no recording and no transcript.",
                    "transcript": "Built from the transcript of the lecture.",
                    "recording": "Built from the recording of the lecture.",
                    "with_slides": " The slides were used beside it."},
           "counts": "{parts} part(s), {ideas} idea(s) of the lecture, {sources} outside "
                     "source(s).",
           "read": "How to read these notes. Coloured boxes hold what you must be able to state: "
                   "definitions, results, worked examples. A term in colour links to its entry "
                   "in the glossary; a question links to its answer at the end, and the answer "
                   "links back. A bracketed number such as [1] marks a statement the lecture "
                   "did not make: it comes from the source cited, where the program found the "
                   "supporting passage word for word.",
           "about_text": "These notes were written by a language model from the lecture and "
                         "checked by code: every idea listed from the lecture is claimed by a "
                         "block of the notes. The companion file report.md lists what was "
                         "checked, what was repaired and what stays open.",
           "open": "Parts with open problems (see report.md): ",
           "missing": "Parts whose notes could not be written (see report.md): "},
    "fr": {"kind": "Notes de travaux dirigés", "glance": "En un coup d'œil",
           "definition": "Définition", "result": "Résultat", "example": "Exemple",
           "intuition": "Intuition", "warning": "Attention", "proof": "Démonstration",
           "takeaways": "À retenir", "pitfalls": "Erreurs fréquentes",
           "checks": "Vérifiez vos acquis", "exercises": "Exercices", "exercise": "Exercice",
           "answer": "Réponse", "solution": "Solution", "solutions": "Réponses et solutions",
           "back": "Retour à la question", "notation": "Notation", "symbol": "Symbole",
           "meaning": "Signification", "glossary": "Glossaire", "references": "Références",
           "about": "À propos de ces notes", "passage": "Passage à l'appui",
           "outside": "Hors du cours", "relies": "Repose sur",
           "confusable": "À ne pas confondre avec", "section": "Section",
           "from": {"slides": "Rédigé à partir des seules diapositives : ni enregistrement ni "
                              "transcription.",
                    "transcript": "Rédigé à partir de la transcription du cours.",
                    "recording": "Rédigé à partir de l'enregistrement du cours.",
                    "with_slides": " Les diapositives ont été utilisées en complément."},
           "counts": "{parts} partie(s), {ideas} idée(s) du cours, {sources} source(s) "
                     "extérieure(s).",
           "read": "Comment lire ces notes. Les encadrés en couleur contiennent ce que vous "
                   "devez savoir énoncer : définitions, résultats, exemples détaillés. Un terme "
                   "en couleur renvoie à son entrée dans le glossaire ; une question renvoie à "
                   "sa réponse en fin de document, et la réponse renvoie à la question. Un "
                   "numéro entre crochets tel que [1] signale un énoncé que le cours n'a pas "
                   "fait : il provient de la source citée, où le programme a retrouvé le "
                   "passage à l'appui mot pour mot.",
           "about_text": "Ces notes ont été rédigées par un modèle de langue à partir du cours "
                         "et contrôlées par du code : chaque idée relevée dans le cours est "
                         "revendiquée par un bloc des notes. Le fichier report.md indique ce "
                         "qui a été vérifié, ce qui a été corrigé et ce qui reste ouvert.",
           "open": "Parties avec des problèmes ouverts (voir report.md) : ",
           "missing": "Parties dont les notes n'ont pas pu être rédigées (voir report.md) : "},
}
_BOX = {"definition": "definition", "result": "result", "example": "example"}
_NOTE = {"intuition": "intuition", "warning": "warning"}


def _cite(block: dict, known: set[str]) -> str:
    ids = [s for s in block["sources"] if s in known]
    return r"~\cite{" + ",".join(ids) + "}" if ids else ""


def render_block(block: dict, labels: dict, linker: latex.Linker, known: set[str]) -> str:
    """One block as LaTeX."""
    kind, cite = block["kind"], _cite(block, known)
    if kind == "prose":
        return latex.block(block["text"], linker) + cite
    if kind in _BOX:
        body = (latex.steps(block["steps"], linker) if kind == "example"
                else latex.block(block["text"], linker))
        if kind == "example" and block["text"]:
            body = latex.block(block["text"], linker) + "\n" + body
        anchor = (rf"\hypertarget{{def:{latex.slug(block['title'])}}}{{}}"
                  if kind == "definition" else "")
        note = "\n\n" + r"\outside{" + labels["outside"] + cite + "}" if cite else ""
        return (rf"\begin{{l2abox}}{{{_BOX[kind]}}}{{{labels[kind]}}}"
                rf"{{{latex.inline(block['title'])}}}" + anchor + "\n" + body + note
                + "\n" + r"\end{l2abox}")
    if kind in _NOTE:
        title = labels[kind] + (": " + latex.inline(block["title"]) if block["title"] else "")
        return (rf"\begin{{l2anote}}{{{_NOTE[kind]}}}{{{title}}}" + "\n"
                + latex.block(block["text"], linker) + cite + "\n" + r"\end{l2anote}")
    if kind == "proof":
        head = latex.block(block["text"], linker) + "\n" if block["text"] else ""
        return (rf"\begin{{proof}}[{labels['proof']}]" + "\n" + head
                + latex.steps(block["steps"], linker) + "\n" + r"\end{proof}")
    if kind == "equation":
        return latex.display(block["latex"])
    caption = rf"\caption{{{latex.inline(block['caption'])}{cite}}}" if block["caption"] else ""
    if kind == "figure":
        drawing = (r"\begin{adjustbox}{max width=\linewidth}" + "\n" + block["tikz"].strip()
                   + "\n" + r"\end{adjustbox}" if block["tikz"].strip()
                   else latex.verbatim(block["ascii"], "BVerbatim"))
        return r"\begin{figure}[H]" + "\n" + r"\centering" + "\n" + drawing + "\n" + caption \
            + "\n" + r"\end{figure}"
    if kind == "code":
        return (latex.verbatim(block["code"], "Verbatim").replace(
            r"\begin{Verbatim}", r"\begin{Verbatim}[frame=leftline,framerule=1.5pt,"
                                 r"rulecolor=\color{muted!50},framesep=9pt,xleftmargin=4pt]", 1)
                + ("\n" + r"{\small\itshape " + latex.inline(block["caption"]) + r"\par}"
                   if block["caption"] else ""))
    return (r"\begin{table}[H]" + "\n" + r"\small" + "\n"
            + latex.table(block["columns"], block["rows"], linker) + "\n" + caption + "\n"
            + r"\end{table}")


def _heading(node: Node) -> str:
    command = {2: "subsection", 3: "subsubsection"}.get(node.level, "subsubsection")
    return rf"\{command}{{{latex.escape(node.title)}}}"


def _questions(part_id: str, record: dict, cards: dict | None) -> tuple[list[dict], list[dict]]:
    """(check questions, exercises) of a part: from the course material when it was made, else
    the recall questions of the script."""
    if cards:
        checks = [{"id": f"{card['id']}.{k}", "question": c["question"], "answer": c["answer"]}
                  for card in cards["cards"] for k, c in enumerate(card["checks"], 1)]
        return checks, cards.get("exercises", [])
    return [{"id": f"{part_id}.q{k}", "question": q["question"], "answer": q["answer"]}
            for k, q in enumerate(record.get("questions", []), 1)], []


def render_document(name: str, outline: Outline, records: list[dict], sections: list[dict],
                    course: dict | None, terms: list[dict], language: str,
                    source_kind: str = "transcript", with_slides: bool = False) -> str:
    """The whole recitation document as LaTeX."""
    labels = LABELS.get(language, LABELS["en"])
    by_part = {r["part"]: r for r in records}
    cards = {p["part"]: p for p in (course or {}).get("parts", [])}
    written = [s for s in sections if "blocks" in s]
    supplements = {s["id"]: s for r in records for s in r.get("supplements", [])}
    cited = [i for i in supplements
             if any(i in b["sources"] for s in written for b in s["blocks"])]
    known = set(cited)
    linker = latex.Linker([t["term"] for t in terms])
    ideas = sum(len(r.get("ideas", [])) for r in records)
    out = [latex.preamble(language, outline.title, labels["kind"]), r"\begin{document}", ""]

    # ---- title, the linked contents, how to read
    built = labels["from"][source_kind] + (labels["from"]["with_slides"] if with_slides else "")
    out += [r"{\sffamily\small\color{accent}\MakeUppercase{" + labels["kind"] + r"}\par}",
            r"\vspace{0.3em}",
            r"{\sffamily\bfseries\Huge " + latex.escape(outline.title) + r"\par}",
            r"\vspace{0.8em}",
            r"{\small\color{muted}" + latex.escape(built + " " + labels["counts"].format(
                parts=len(written), ideas=ideas, sources=len(cited))) + r"\par}",
            r"\vspace{1.2em}", rf"\begin{{l2anote}}{{accent}}{{{labels['glance']}}}",
            r"\begin{enumerate}[leftmargin=1.4em]"]
    for section in written:
        out.append(rf"\item \hyperref[part:{section['part']}]{{\textbf{{"
                   + latex.escape(section["title"]) + "}}. " + latex.inline(section["takeaways"][0]))
    out += [r"\end{enumerate}", r"\end{l2anote}", "",
            r"{\small\color{muted}" + latex.escape(labels["read"]) + r"\par}", ""]

    # ---- the parts
    answers = []
    for section in written:
        part = outline.node(section["part"])
        linker.new_section()
        out += ["", rf"\section{{{latex.escape(part.title)}}}\label{{part:{part.id}}}",
                r"\lead{" + latex.inline(section["hook"]) + "}"]
        node = part.id
        for block in section["blocks"]:
            if block["node"] != node:
                node = block["node"]
                out.append(_heading(outline.node(node)))
            out += [render_block(block, labels, linker, known), ""]
        out += [rf"\subsubsection*{{{labels['takeaways']}}}",
                latex.steps(section["takeaways"], linker, "itemize")]
        if section["pitfalls"]:
            out += [rf"\subsubsection*{{{labels['pitfalls']}}}",
                    latex.steps(section["pitfalls"], linker, "itemize")]
        checks, exercises = _questions(part.id, by_part[part.id], cards.get(part.id))
        if checks:
            out += [rf"\subsubsection*{{{labels['checks']}}}", r"\begin{enumerate}"]
            for check in checks:
                out.append(rf"\item \hypertarget{{q:{check['id']}}}{{}}"
                           + latex.inline(check["question"])
                           + rf" \jump{{a:{check['id']}}}{{{labels['answer']}}}")
                answers.append((check["id"], labels["answer"], latex.inline(check["question"]),
                                latex.block(check["answer"])))
            out.append(r"\end{enumerate}")
        if exercises:
            out.append(rf"\subsubsection*{{{labels['exercises']}}}")
            for k, exercise in enumerate(exercises, 1):
                tag = f"{part.id}.x{k}"
                out += [rf"\hypertarget{{q:{tag}}}{{}}\textbf{{{labels['exercise']} "
                        rf"{part.id}.{k}.}} " + latex.block(exercise["question"])
                        + rf" \jump{{a:{tag}}}{{{labels['solution']}}}", ""]
                answers.append((tag, f"{labels['exercise']} {part.id}.{k}",
                                latex.block(exercise["question"]),
                                latex.steps(exercise["steps"]) + "\n\n" + r"\textbf{"
                                + labels["answer"] + ".} " + latex.block(exercise["answer"])))

    # ---- the back matter
    out += ["", r"\appendix"]
    if answers:
        out.append(rf"\section{{{labels['solutions']}}}")
        for tag, head, question, body in answers:
            out += [rf"\hypertarget{{a:{tag}}}{{}}\textbf{{{head}.}} {{\itshape {question}}}"
                    rf" \jump{{q:{tag}}}{{{labels['back']}}}", "", body, "",
                    r"\medskip"]
    notation = (course or {}).get("notation") or []
    if notation:
        out += [rf"\section{{{labels['notation']}}}",
                latex.table([labels["symbol"], labels["meaning"]],
                            [[f"${n['symbol']}$", n["meaning"]] for n in notation])]
    if terms:
        out += [rf"\section{{{labels['glossary']}}}", r"\begin{description}[leftmargin=0pt]"]
        for term in sorted(terms, key=lambda t: t["term"].lower()):
            extra = ""
            for key in ("depends_on", "confusable"):
                names = [n for n in term.get(key, [])]
                if names:
                    label = labels["relies" if key == "depends_on" else "confusable"]
                    links = ", ".join(rf"\hyperlink{{term:{latex.slug(n)}}}{{{latex.escape(n)}}}"
                                      for n in names)
                    extra += rf" {{\small\color{{muted}}{label}: {links}.}}"
            out.append(rf"\item[\hypertarget{{term:{latex.slug(term['term'])}}}"
                       rf"{{{latex.escape(term['term'])}}}] "
                       + latex.inline(term["definition"])
                       + rf" {{\small(\hyperref[part:{term['part']}]{{{labels['section']} "
                         rf"{term['part']}}})}}" + extra)
        out.append(r"\end{description}")
    if cited:
        out += [r"\renewcommand{\refname}{" + labels["references"] + "}",
                rf"\begin{{thebibliography}}{{{len(cited)}}}"]
        for source_id in cited:
            item = supplements[source_id]
            out.append(rf"\bibitem{{{source_id}}} "
                       + latex.escape(item.get("title") or item["url"]) + ". "
                       + latex.url(item["url"]) + ". "
                       + rf"{{\small {labels['passage']}: ``{latex.escape(item.get('quote', ''))}''}}")
        out.append(r"\end{thebibliography}")
    flagged = [s["part"] for s in written if s.get("flagged")]
    missing = [s["part"] for s in sections if "blocks" not in s]
    out += [rf"\section*{{{labels['about']}}}",
            r"{\small\color{muted}" + latex.escape(
                labels["about_text"]
                + (" " + labels["open"] + ", ".join(flagged) + "." if flagged else "")
                + (" " + labels["missing"] + ", ".join(missing) + "." if missing else ""))
            + r"\par}", "", r"\end{document}", ""]
    return "\n".join(out)


def report_lines(sections: list[dict], build: dict | None) -> list[str]:
    """The section of report.md about the recitation notes."""
    lines = ["## Recitation notes", ""]
    written = [s for s in sections if "blocks" in s]
    blocks = sum(len(s["blocks"]) for s in written)
    lines.append(f"The notes hold {blocks} block(s) in {len(written)} part(s). Code checked "
                 "that every idea of the lecture is claimed by a block, that the numbers of "
                 "each idea are in the blocks that claim it, and that every TikZ figure "
                 "compiles alone.")
    if build is None:
        lines.append("The PDF was not asked for; `recitation.tex` is the document.")
    elif build.get("pdf"):
        lines.append(f"`recitation.pdf` was built with {build['engine']}"
                     + (f"; the log holds {len(build['errors'])} error(s), which LaTeX passed "
                        "over:" if build["errors"] else "."))
        lines += [f"- {e}" for e in build["errors"][:20]]
    else:
        lines.append("The PDF was not built" + (": no LaTeX is installed."
                                               if not build.get("engine") else
                                               "; the first error of the log follows.")
                     + " `recitation.tex` is the document.")
        lines += [f"- {e}" for e in build.get("errors", [])[:3]]
    lines.append("")
    for section in sections:
        if "blocks" not in section:
            lines.append(f"- Part {section['part']}: the notes could not be written "
                         f"({section['error']}). The part is absent from the document.")
            continue
        for issue in section["issues"]:
            lines.append(f"- Part {section['part']}, {issue['type']}: {issue['detail']}")
        for caption in section.get("dropped_figures", []):
            lines.append(f"- Part {section['part']}: the figure \"{caption}\" did not compile "
                         "after the repairs and was left out.")
        for repair in section.get("repairs", []):
            scope = repair["scope"] if repair["scope"] == "all" else scope_words(repair["scope"])
            lines.append(f"- Part {section['part']}, attempt {repair['attempt']}: rewrote "
                         f"{'the whole part' if scope == 'all' else scope}; blocking problems "
                         f"{repair['before']} to {repair['after']}; "
                         f"{'kept' if repair['kept'] else 'discarded'}.")
    return lines + [""]
