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
INLINE_MATH = re.compile(r"\$([^$\n]+)\$")


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
    """Prose is plain Markdown: raw HTML would not be shown as written."""
    return bool(HTML_TAG.search(INLINE_MATH.sub("", text)))


class Model(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class Frame(Model):
    caption: Text = Field(min_length=1)
    mermaid: str = Field(min_length=1, max_length=4000)

    @field_validator("mermaid")
    @classmethod
    def _well_formed(cls, value):
        code = "\n".join(l for l in value.strip().splitlines() if not l.strip().startswith("%%"))
        if not code.startswith(MERMAID_KINDS):
            raise ValueError(f"a Mermaid diagram starts with one of {list(MERMAID_KINDS)}")
        if MERMAID_FORBIDDEN.search(value):
            raise ValueError("Mermaid code may not hold scripts, callbacks or links")
        return value.strip()


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


class Card(Model):
    node: Text
    kind: Literal["definition", "concept", "procedure", "remark"]
    title: Text = Field(min_length=1)
    terms: list[Text] = []
    definition: Text = ""
    instance: Text = ""
    explanation: list[Text] = Field(min_length=1, max_length=MAX_PARAGRAPHS)  # paragraphs
    formulas: list[Text] = []  # LaTeX, one per line
    notation: list[Notation] = []
    example: Example | None = None
    diagram: Diagram | None = None
    code: list[Snippet] = []
    table: Table | None = None
    checks: list[Check] = Field(default=[], max_length=4)
    covers: list[int] = Field(min_length=1)

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
        texts = [self.title, self.definition, self.instance, *self.explanation]
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
        return self


class Cards(Model):
    cards: list[Card] = Field(min_length=1)


CARDS_SCHEMA = Cards.model_json_schema(by_alias=True)


def describe(error: ValidationError, limit: int = 8) -> list[str]:
    """Pydantic errors as short sentences a model can act on."""
    return [f"{'.'.join(str(p) for p in item['loc'])}: {item['msg']}"
            for item in error.errors()[:limit]]
