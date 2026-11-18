"""The project of a lecture (`--project`): one thing to build in about twenty checkable steps.

One model call over the whole lecture (the outline and the idea ledger). Code checks the shape,
that every step names ideas that exist, and reports the ideas no step exercises; it then
writes `project.tex`. Like the recitation notes, the model writes content and code writes the
LaTeX.
"""
from __future__ import annotations

import json

from pydantic import Field, ValidationError, model_validator

from . import latex
from .course_schema import Model, Text, describe, html_in_text
from .outline import Outline, render_outline
from .pipeline import PipelineError, TextPipeline, _section

MIN_STEPS, MAX_STEPS = 6, 30


class Step(Model):
    task: Text = Field(min_length=1)
    hint: Text = ""
    check: Text = Field(min_length=1)
    solution: Text = ""
    ideas: list[Text] = Field(min_length=1)


class Milestone(Model):
    title: Text = Field(min_length=1)
    goal: Text = Field(min_length=1)
    steps: list[Step] = Field(min_length=1)


class Project(Model):
    title: Text = Field(min_length=1)
    pitch: Text = Field(min_length=1)
    outcome: Text = Field(min_length=1)
    prerequisites: list[Text] = Field(default=[], max_length=8)
    materials: list[Text] = Field(default=[], max_length=8)
    milestones: list[Milestone] = Field(min_length=2, max_length=8)
    extensions: list[Text] = Field(default=[], max_length=6)

    @model_validator(mode="after")
    def _texts(self):
        texts = [self.title, self.pitch, self.outcome, *self.prerequisites, *self.materials,
                 *self.extensions]
        for milestone in self.milestones:
            texts += [milestone.title, milestone.goal]
            for step in milestone.steps:
                texts += [step.task, step.hint, step.check, step.solution]
        if any(html_in_text(t) for t in texts):
            raise ValueError("write plain Markdown: no HTML tags in the text")
        errors = [e for t in texts for e in latex.math_errors(t)]
        if errors:
            raise ValueError("math is LaTeX the program can typeset: " + "; ".join(errors[:4]))
        return self


def validate_project(data, idea_ids: set[str]) -> list[str]:
    """The gate: the shape, a number of steps a student can carry out, ideas that exist."""
    try:
        project = Project.model_validate(data)
    except ValidationError as exc:
        return describe(exc)
    errors = []
    steps = [s for m in project.milestones for s in m.steps]
    if not MIN_STEPS <= len(steps) <= MAX_STEPS:
        errors.append(f"the project has between {MIN_STEPS} and {MAX_STEPS} steps in all; "
                      f"this one has {len(steps)}")
    for k, step in enumerate(steps, 1):
        if unknown := [i for i in step.ideas if i not in idea_ids]:
            errors.append(f'step {k}: "ideas" holds ids of IDEAS; unknown: {unknown}')
    return errors[:8]


def run_project(pipeline: TextPipeline, outline: Outline) -> dict | None:
    """The project, cached in project.json; None when the lecture has no listed idea or the
    model's answer keeps failing the gate (logged, not cached)."""
    cache = pipeline.work_dir / "project.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    ideas = pipeline.coverage.get("transcript_ideas", [])
    if not ideas:
        pipeline.log("[project] no idea was listed for this lecture: no project is written")
        return None
    pipeline.log(f"[project] designing a project on {len(ideas)} idea(s)")
    ids = {str(i["id"]) for i in ideas}
    user = (f"## LANGUAGE\n{pipeline.language_name}\n\n"
            + _section("OUTLINE", render_outline(outline))
            + _section("TERMS", ", ".join(t["term"] for t in pipeline.terms))
            + "## IDEAS (JSON)\n" + json.dumps(
                [{k: i[k] for k in ("id", "kind", "idea") if k in i} for i in ideas],
                ensure_ascii=False, indent=1))
    try:
        data = pipeline._ask_json(pipeline.rewrite_llm, pipeline.prompts.project, user,
                                  lambda d: validate_project(d, ids), "project")
    except PipelineError as exc:
        pipeline.log(f"[project] {exc}")
        return None
    project = Project.model_validate(data).model_dump(mode="json")
    used = {i for m in project["milestones"] for s in m["steps"] for i in s["ideas"]}
    project["unused_ideas"] = [str(i["id"]) for i in ideas if str(i["id"]) not in used]
    project["idea_count"] = len(ideas)
    cache.write_text(json.dumps(project, ensure_ascii=False, indent=2), encoding="utf-8")
    return project


def dry_project(user: str) -> str:
    """Stand-in for the designer in smoke tests: one step per idea, in two milestones."""
    ideas = json.loads(user.split("## IDEAS (JSON)\n", 1)[1])
    steps = [{"task": i["idea"], "hint": "", "check": f"The result agrees with idea {i['id']}.",
              "solution": "", "ideas": [i["id"]]} for i in ideas[:MAX_STEPS]]
    while len(steps) < MIN_STEPS:  # a very short lecture: the steps repeat to reach the gate
        steps.append(dict(steps[len(steps) % len(ideas)]))
    half = len(steps) // 2
    return json.dumps({"title": "Project", "pitch": ideas[0]["idea"], "outcome": ideas[-1]["idea"],
                       "prerequisites": [], "materials": [],
                       "milestones": [{"title": "First half", "goal": steps[half - 1]["task"],
                                       "steps": steps[:half]},
                                      {"title": "Second half", "goal": steps[-1]["task"],
                                       "steps": steps[half:]}],
                       "extensions": []})


LABELS = {
    "en": {"kind": "Project", "outcome": "What you will have at the end",
           "prerequisites": "Before you start", "materials": "What you need",
           "milestone": "Milestone", "goal": "Done when", "step": "Step", "hint": "Hint",
           "check": "Check", "solution": "Outline", "solutions": "Outlines of the steps",
           "back": "Back to the step", "extensions": "Going further",
           "how": "How to work. Do the steps in order: each one uses the results of the "
                  "steps before it. Do not move on until the check of a step passes. The hint "
                  "points at the idea to use; the outline at the end is for when you have "
                  "tried and are stuck. The numbers in brackets after a step are the ideas of "
                  "the lecture it exercises (ideas.csv)."},
    "fr": {"kind": "Projet", "outcome": "Ce que vous aurez à la fin",
           "prerequisites": "Avant de commencer", "materials": "Ce qu'il vous faut",
           "milestone": "Étape clé", "goal": "Terminé quand", "step": "Étape",
           "hint": "Indice", "check": "Vérification", "solution": "Esquisse",
           "solutions": "Esquisses des étapes", "back": "Retour à l'étape",
           "extensions": "Pour aller plus loin",
           "how": "Comment travailler. Faites les étapes dans l'ordre : chacune utilise les "
                  "résultats des précédentes. Ne passez pas à la suivante tant que la "
                  "vérification n'est pas satisfaite. L'indice désigne l'idée à utiliser ; "
                  "l'esquisse en fin de document sert quand vous avez essayé et que vous "
                  "êtes bloqué. Les numéros entre crochets après une étape sont les idées du "
                  "cours qu'elle fait travailler (ideas.csv)."},
}


def render_project(project: dict, lecture_title: str, language: str) -> str:
    """The project as a LaTeX document."""
    labels = LABELS.get(language, LABELS["en"])
    out = [latex.preamble(language, lecture_title, labels["kind"]), r"\begin{document}", "",
           r"{\sffamily\small\color{accent}\MakeUppercase{" + labels["kind"] + " · "
           + latex.escape(lecture_title) + r"}\par}", r"\vspace{0.3em}",
           r"{\sffamily\bfseries\Huge " + latex.inline(project["title"]) + r"\par}",
           r"\vspace{0.8em}", r"\lead{" + latex.inline(project["pitch"]) + "}",
           rf"\begin{{l2anote}}{{accent}}{{{labels['outcome']}}}",
           latex.block(project["outcome"]), r"\end{l2anote}"]
    for key in ("prerequisites", "materials"):
        if project[key]:
            out += [rf"\subsubsection*{{{labels[key]}}}", latex.steps(project[key], kind="itemize")]
    out += [r"{\small\color{muted}" + latex.escape(labels["how"]) + r"\par}"]
    number, outlines = 0, []
    for m, milestone in enumerate(project["milestones"], 1):
        out += ["", rf"\section{{{latex.inline(milestone['title'])}}}",
                r"{\small\sffamily\color{muted}" + labels["goal"] + ": "
                + latex.inline(milestone["goal"]) + r"\par}"]
        for step in milestone["steps"]:
            number += 1
            ideas = ", ".join(latex.escape(i) for i in step["ideas"])
            jump = (rf" \jump{{o:{number}}}{{{labels['solution']}}}" if step["solution"] else "")
            out += ["", rf"\hypertarget{{s:{number}}}{{}}"
                    rf"\begin{{l2anote}}{{example}}{{{labels['step']} {number}}}",
                    latex.block(step["task"])
                    + rf" {{\small\sffamily\color{{muted}}[{ideas}]}}" + jump, ""]
            if step["hint"]:
                out += [rf"{{\small\textbf{{{labels['hint']}.}} " + latex.inline(step["hint"])
                        + r"\par}", ""]
            out += [rf"\textbf{{{labels['check']}.}} " + latex.inline(step["check"]),
                    r"\end{l2anote}"]
            if step["solution"]:
                outlines.append((number, latex.block(step["solution"])))
    if project["extensions"]:
        out += [rf"\section*{{{labels['extensions']}}}",
                latex.steps(project["extensions"], kind="itemize")]
    if outlines:
        out += [r"\appendix", rf"\section{{{labels['solutions']}}}"]
        for number, body in outlines:
            out += [rf"\hypertarget{{o:{number}}}{{}}\textbf{{{labels['step']} {number}.}} "
                    + body + rf" \jump{{s:{number}}}{{{labels['back']}}}", ""]
    return "\n".join(out + [r"\end{document}", ""])


def report_lines(project: dict | None, build: dict | None) -> list[str]:
    """The section of report.md about the project."""
    lines = ["## Project", ""]
    if project is None:
        return lines + ["No project was written (no idea was listed, or the model's answer "
                        "kept failing the checks; the log of the run says which).", ""]
    steps = sum(len(m["steps"]) for m in project["milestones"])
    unused = project.get("unused_ideas", [])
    lines.append(f"\"{project['title']}\": {steps} step(s) in {len(project['milestones'])} "
                 f"milestone(s). The steps exercise {project.get('idea_count', 0) - len(unused)} "
                 f"of the {project.get('idea_count', 0)} idea(s) of the lecture."
                 + (" `project.pdf` was built." if build and build.get("pdf") else
                    " `project.tex` is the document; the PDF was not built."))
    if unused:
        lines.append("Ideas that no step exercises: " + ", ".join(unused[:60])
                     + (" ..." if len(unused) > 60 else "") + ".")
    return lines + [""]
