"""The shape of the course material, as Pydantic models.

One definition serves three purposes: it validates the model's JSON (the gate before any
judge), it normalises the cards (stripped texts, every optional field present), and its JSON
Schema (`CARDS_SCHEMA`) can be given to a provider's structured-output option.
"""
from __future__ import annotations

import re
from typing import Annotated, Literal

from latex2mathml.converter import convert as _latex_to_mathml
from pydantic import (BaseModel, ConfigDict, Field, StringConstraints, ValidationError,
                      field_validator, model_validator)

MAX_FRAMES = 8
MAX_PARAGRAPHS = 12

Text = Annotated[str, StringConstraints(strip_whitespace=True)]

# Mermaid diagram kinds the vault supports (Obsidian renders them natively).
MERMAID_KINDS = ("graph", "flowchart", "sequenceDiagram", "stateDiagram", "stateDiagram-v2",
                 "classDiagram", "erDiagram", "gantt", "pie", "mindmap", "timeline",
                 "xychart-beta", "quadrantChart", "gitGraph", "journey", "block-beta")
MERMAID_FORBIDDEN = re.compile(r"<script|javascript:|^\s*click\s|\bcallback\b|<iframe|<img|"
                               r"https?:", re.I | re.M)
HTML_TAG = re.compile(r"</?[A-Za-z][^>]*>")
# Real HTML in an ASCII figure. Placeholders such as <src> or <data> are figure labels, not HTML.
HTML_ELEMENT = re.compile(r"</?(?:a|abbr|b|br|button|code|details|div|em|embed|font|form|h[1-6]|"
                          r"hr|i|iframe|img|input|li|link|meta|object|ol|p|pre|script|small|"
                          r"span|strong|style|sub|summary|sup|svg|table|tbody|td|th|thead|tr|u|"
                          r"ul|video|audio|html|body|head)\b[^>]*>|&[a-z]+;|&#\d+;", re.I)
INLINE_MATH = re.compile(r"\$([^$\n]+)\$")
_CODE_SPAN = re.compile(r"`[^`\n]*`")


def latex_error(source: str) -> str | None:
    """None when the LaTeX converts to MathML (so it is well formed), else the reason."""
    try:
        _latex_to_mathml(source)
    except Exception as exc:  # the converter raises its own family of errors
        return f"{type(exc).__name__}: {exc}"
    return None


def math_in_text(text: str) -> list[str]:
    """Errors for every $...$ segment of a prose text that is not valid LaTeX."""
    return [f"${m.group(1)}$: {why}" for m in INLINE_MATH.finditer(text)
            if (why := latex_error(m.group(1)))]


def html_in_text(text: str) -> bool:
    """Prose is plain Markdown: raw HTML would not be shown as written. Math, code between
    backquotes and placeholders such as <condition> are not HTML (real slides about
    programming are full of "x < y" and "<expression>")."""
    return bool(HTML_ELEMENT.search(_CODE_SPAN.sub("", INLINE_MATH.sub("", text))))


class Model(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class Frame(Model):
    """One still frame of a figure: EITHER a Mermaid diagram (structure, states, protocol
    flows) OR an ASCII box drawing (bit layouts, memory, registers, pointer traces)."""
    caption: Text = Field(min_length=1)
    mermaid: str = Field(default="", max_length=4000)
    ascii: str = Field(default="", max_length=4000)

    @field_validator("mermaid")
    @classmethod
    def _well_formed(cls, value):
        if not value.strip():
            return ""
        code = "\n".join(l for l in value.strip().splitlines() if not l.strip().startswith("%%"))
        if not code.startswith(MERMAID_KINDS):
            raise ValueError(f"a Mermaid diagram starts with one of {list(MERMAID_KINDS)}")
        if MERMAID_FORBIDDEN.search(value):
            raise ValueError("Mermaid code may not hold scripts, callbacks or links")
        if "```" in value:
            raise ValueError("give the Mermaid code without the ``` fence")
        return value.strip()

    @field_validator("ascii")
    @classmethod
    def _plain_drawing(cls, value):
        if "```" in value or "~~~" in value:
            raise ValueError("give the ASCII drawing without a ``` fence: the program adds it")
        if HTML_ELEMENT.search(value):
            raise ValueError("an ASCII drawing holds no HTML tags or entities")
        # keep the leading spaces: they align the boxes
        lines = [line.rstrip() for line in value.replace("\t", "    ").splitlines()]
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()
        return "\n".join(lines)

    @model_validator(mode="after")
    def _one_kind(self):
        if bool(self.mermaid) == bool(self.ascii):
            raise ValueError('a frame has exactly one of "mermaid" and "ascii" (the other is '
                             '"" or left out)')
        return self


class Diagram(Model):
    """A figure shown as numbered still frames, one change per frame."""
    title: Text = ""
    frames: list[Frame] = Field(min_length=1, max_length=MAX_FRAMES)


class Snippet(Model):
    language: Text = ""
    code: str = Field(min_length=1, max_length=4000)
    caption: Text = ""

    @field_validator("code")
    @classmethod
    def _no_fence(cls, value):
        if "```" in value:
            raise ValueError("give the code without the ``` fence")
        return value.rstrip()


class Table(Model):
    columns: list[Text] = Field(min_length=1, max_length=12)
    rows: list[Annotated[list[Text], Field(min_length=1, max_length=12)]] = Field(
        min_length=1, max_length=40)
    caption: Text = ""

    @model_validator(mode="after")
    def _rectangular(self):
        if any(len(r) != len(self.columns) for r in self.rows):
            raise ValueError("every row has as many cells as there are columns")
        return self


class Notation(Model):
    symbol: Text = Field(min_length=1)  # LaTeX
    meaning: Text = Field(min_length=1)

    @field_validator("symbol")
    @classmethod
    def _latex(cls, value):
        if why := latex_error(value):
            raise ValueError(f"not valid LaTeX ({why})")
        return value


class Example(Model):
    title: Text = Field(min_length=1)
    steps: list[Text] = Field(min_length=1)


class Check(Model):
    question: Text = Field(min_length=1)
    answer: Text = Field(min_length=1)


class Exercise(Model):
    """A practice problem in the style of a recitation: answered from the part alone, with a
    complete worked solution."""
    question: Text = Field(min_length=1)
    steps: list[Text] = Field(min_length=1, max_length=12)
    answer: Text = Field(min_length=1)
    covers: list[int] = Field(min_length=1)


class Card(Model):
    node: Text
    kind: Literal["definition", "concept", "procedure", "remark"]
    title: Text = Field(min_length=1)
    plain: Text = ""  # "In short": the idea in plain words, for a reader new to the field
    terms: list[Text] = []
    definition: Text = ""
    instance: Text = ""
    explanation: list[Text] = Field(min_length=1, max_length=MAX_PARAGRAPHS)  # paragraphs
    formulas: list[Text] = []  # LaTeX, one per line
    notation: list[Notation] = []
    example: Example | None = None
    proof: list[Text] = Field(default=[], max_length=16)  # steps of a proof the lecture gives
    diagram: Diagram | None = None
    code: list[Snippet] = []
    table: Table | None = None
    checks: list[Check] = Field(default=[], max_length=4)
    covers: list[int] = Field(min_length=1)
    ideas: list[Text] = []  # ids of the ledger's ideas (T1, T2, ...) this card teaches

    @field_validator("explanation")
    @classmethod
    def _no_empty(cls, value):
        if any(not s for s in value):
            raise ValueError("explanation paragraphs may not be empty")
        return value

    @field_validator("formulas")
    @classmethod
    def _formulas(cls, value):
        errors = [f"{f!r}: {why}" for f in value if (why := latex_error(f))]
        if errors:
            raise ValueError("formulas are LaTeX: " + "; ".join(errors))
        return [f for f in value if f]

    @model_validator(mode="after")
    def _texts(self):
        texts = [self.title, self.plain, self.definition, self.instance, *self.explanation,
                 *self.proof]
        if self.example:
            texts += [self.example.title, *self.example.steps]
        for check in self.checks:
            texts += [check.question, check.answer]
        if self.diagram:
            texts += [self.diagram.title, *(f.caption for f in self.diagram.frames)]
        if self.table:
            texts += [*self.table.columns, *(c for r in self.table.rows for c in r)]
        if any(html_in_text(t) for t in texts):
            raise ValueError("write plain Markdown: no HTML tags in the text")
        errors = [e for t in texts for e in math_in_text(t)]
        if errors:
            raise ValueError("inline math between $ $ is LaTeX: " + "; ".join(errors))
        self.covers = sorted(set(self.covers))
        self.ideas = list(dict.fromkeys(i for i in self.ideas if i))
        return self


class Cards(Model):
    cards: list[Card] = Field(min_length=1)
    exercises: list[Exercise] = Field(default=[], max_length=6)

    @model_validator(mode="after")
    def _exercise_texts(self):
        for exercise in self.exercises:
            texts = [exercise.question, exercise.answer, *exercise.steps]
            if any(html_in_text(t) for t in texts):
                raise ValueError("exercises are plain Markdown: no HTML tags")
            errors = [e for t in texts for e in math_in_text(t)]
            if errors:
                raise ValueError("inline math between $ $ is LaTeX: " + "; ".join(errors))
            exercise.covers = sorted(set(exercise.covers))
        return self


CARDS_SCHEMA = Cards.model_json_schema(by_alias=True)


def describe(error: ValidationError, limit: int = 8) -> list[str]:
    """Pydantic errors as short sentences a model can act on."""
    return [f"{'.'.join(str(p) for p in item['loc'])}: {item['msg']}"
            for item in error.errors()[:limit]]
