# ADR-0004: Recitation notes in LaTeX, slides as a source, a project, and a local app

**Status:** Accepted
**Date:** 14 November 2026
**Deciders:** The project owner (requests of 13 and 14 November 2026).

## Context

Until now the program needed something spoken (a recording or a transcript) and its reading product was an Obsidian vault of cards. The owner asked for four things: notes of the quality of a university recitation handout, typeset with LaTeX, with clickable references; a run from slides alone, with the input detected; a project that makes the student apply the lecture; and an app with a real interface.

Runs on real lectures from MIT OpenCourseWare (slides of 18.650 and 6.0001, a PowerPoint deck of 12.010, captions and transcripts) with the pass-through model showed three facts that shaped the design:

1. The text of a slide PDF does not hold its formulas. In the 18.650 deck a sum sign is extracted as "L" and an integral as "J"; in a 6.0001 deck the font maps "notation" to "notaAon". Four PDF readers gave the same result: the loss is in the file, not in the reader.
2. Real slide text holds every character that breaks LaTeX (`%`, `&`, `_`, `<`, control characters, private-use bullet glyphs).
3. The app could only follow a run by reading the wording of the log.

## Decisions

### 1. The model writes content, code writes LaTeX

The recitation stage (`recitation.py`, one call per part) returns blocks: prose, definition, result, proof, example, intuition, warning, equation, figure, code, table. Each block names the ideas of the ledger it teaches and the outside sources it relies on. Code escapes every text field, sets the blocks in a fixed preamble (`assets/preamble.tex`), and creates both ends of every link: term to glossary, question to answer and back, citation to reference, contents to part.

Rejected: letting the model write the `.tex` file. One unescaped character stops the build; links cannot be guaranteed; and the content could not be checked before it is typeset. Also rejected: rendering the cards of the course stage directly. The cards are written under rules that suit review (one idea each, no connective prose); a handout needs the connective prose.

Code checks before any typesetting: every idea of the part is claimed by a block (blocking), the numbers of an idea are in the blocks that claim it (reported), every TikZ figure compiles alone with `pdflatex` (blocking; a figure that still fails after the repairs is left out and reported). Repairs are bounded to the nodes at fault, as for the script and the cards.

Math and TikZ are the only raw TeX that reaches the document. Both are refused when they hold a command that reads files, writes files or defines macros (`latex.FORBIDDEN`), and the PDF is built without shell escape. Slide text can carry instructions aimed at the model, so model output is not trusted to be harmless TeX.

Exercises and check questions are not written again: they come from the course stage, where a judge has solved them.

### 2. Slides alone are a lecture, and a PDF is read by sight

`sources.input_kind` and `discover_inputs` decide what was given. With slides and nothing spoken, the units of the lecture are the bullets and sentences of the slides (`slide_units`), and the outline, gap search and rewrite prompts receive a `SOURCE KIND` section. The explanation the slides lack comes from the gap search and the web research, under the same rule as before: only facts whose quote code finds in a page.

When the model reads images, each page is rendered and transcribed by the model, formulas as LaTeX and figures described (`slides.py`, stage `slides`, one call per slide, cached). Code compares each reading with the text layer of the page: a reading that keeps less than 60 % of the page's words is asked again once, then replaced by the text layer and reported. `--read-slides auto` uses sight for Anthropic and OpenAI, whose models are known to read images, and the text layer otherwise.

### 3. The project is one call over the idea ledger

`--project` asks for one thing to build in steps, each with a check the student can run alone and the ideas it exercises (`project.py`). Code checks the shape and the idea ids and reports the ideas no step exercises. The number of steps has a wide range (6 to 30) and the prompt says "fewer for a short lecture", because a required count makes a model pad.

### 4. A local app, not a hosted service

`lecture2audio app` serves one page on 127.0.0.1 and runs the command line in a subprocess. `app/Lecture2Audio.app` starts it by double-click.

Why local: the voice and speech models run on the owner's Mac through MLX and need its memory; recordings of lectures and API keys stay on the machine; there is one user. A hosted service would need accounts, a job queue, object storage, a GPU for the voice and server-side keys, which is another product.

What keeps hosting possible later: a run is a pure function of its input folder and options, and all its state is the output folder, so the same subprocess can run in a container behind a queue; the server already separates jobs, files and events; the voice has a hosted backend (`--tts openai`).

Security of the local server: it binds 127.0.0.1 only, refuses a foreign `Host` or `Origin` (DNS rebinding), requires a per-process token on every POST (cross-site requests), serves only files under the output folder, never serves the uploads back, and passes API keys only through the environment of the subprocess.

### 5. A run reports its stages as data

`stages.Tracker` writes one JSON line per change of stage to `events.jsonl` and, with `L2A_EVENTS`, to standard output. The app follows these lines. The time per stage goes to `report.md`.

## Consequences

- The reading product now exists three times: the script (for the voice), the cards (for review in Obsidian) and the recitation notes. The notes are built from the script and the cards, so the cost is one more call per part.
- Obsidian is no longer the only interface (this replaces decision 3 of ADR-0001).
- `pypdf` and `python-pptx` are required packages. A PDF needs a LaTeX installation (MacTeX, TeX Live or tectonic); without one, the `.tex` file is still written.

## Open risks

1. **No real model has run any of this.** Every run used the pass-through model, because no API key was available. The prompts for the recitation notes, the project and the slide reading are untested; their quality is unknown. This is the first thing to do.
2. **`cli.run` still sequences the stages by hand.** The graph in `stages.py` declares the order and the tracker reports it, but the function is one long sequence. It should become one function per stage, run from the declaration.
3. **Three writers per part** (script, cards, notes) repeat work. Once real runs exist, measure whether the cards can be derived from the notes' blocks, or the notes from the cards, without a second writing call.
4. **Slides that are pictures only** (scanned decks) have no text layer to check a reading against; such readings are accepted unchecked.
5. **The voice was not reviewed.** The default voice models were left as they were.
