# What a run writes

Everything is in `out/<run>/`, where `<run>` is the folder or file name of the input in lower
case with hyphens (`My Lecture/` gives `my-lecture`). The MCP tool `read_result` and the
resources `lecture2audio://runs/{run}/{file}` serve the six files marked "MCP".

## Files to read

| File | MCP | What it is |
|---|---|---|
| `report.md` | yes | The quality report. Read it first. |
| `script.md` | yes | The verified script of every part. Sentences filled from the web are tagged `external` with a source id. |
| `glossary.md` | yes | One definition per term, in dependency order. |
| `questions.md` | yes | The check questions with their answers. |
| `ideas.csv` | yes | The idea ledger, one row per atomic idea of the lecture. |
| `termbase.json` | yes | The termbase behind the glossary, as data. |
| `index.md` | no | Entry page of the run. |
| `reference.md` | no | The web sources used to fill gaps. |
| `<run>.m4a` | no | The whole audio with chapters (`ffprobe -v error -show_chapters <run>.m4a`). |
| `vault/` | no | Obsidian notes (or the `--vault-dir` folder): an index note that embeds the audio and one note per part. |
| `recitation.pdf`, `recitation.tex` | no | Recitation notes typeset with LaTeX. |
| `project.pdf` | no | The project, with `--project`. |
| `anki.csv` | no | Front, Back, Tags, with `--export-anki`. |
| `events.jsonl` | no | One line per change of stage (see `stages.md`). |
| `calls.jsonl` | no | One line per network request (model calls, searches, page reads), keys replaced by `[key]`. Can be large. |

## Sections of `report.md`

- **Checks on the whole lecture**: the planted-fault tests. Each judge of completeness (the
  verifier, the gap search, the idea listing, the course judge) is given a planted fault; a
  judge that misses it is not trustworthy on that run. Report a failure to the user.
- **Gaps in the lecture and how they were filled**: untaught slide ideas, undefined terms,
  assumed fundamentals, skipped steps, and the checked sources used for each.
- **Idea ledger (ideas.csv)**: every idea that did not arrive.
- **Repairs**: every bounded repair of a script or of the cards.
- **Terminology**: terms used before their part, confusable words the sources never use.
- **Model use in this run**: calls and tokens per model.
- **Course material**, **Recitation notes**: counts and the problems still open.
- **Stages of this run**: which stages were computed, reused from the cache or not run.
- **Order changes made by the outline**: every move inside a part.
- **Removed as noise**: every unit left out, in full.

## Statuses in `ideas.csv`

Columns: `idea_id,node_id,kind,statement,source_units,covered_in_card,status`.

| Status | Meaning | Action |
|---|---|---|
| `verified` | A card teaches the idea and the judge confirmed it. | None. |
| `scripted` | The script teaches it; the cards are not judged yet. | None, unless the run ended. |
| `pending` | No stage has answered for it yet. | Check whether the run finished. |
| `missing_script` | The verified script does not teach it. | Tell the user; look at its `source_units` in `transcript.txt`. |
| `missing_cards` | The script teaches it, the cards do not. | Tell the user. |
| `disputed` | A judge said "taught" but code did not find the idea's numbers in the text. | Tell the user; check by hand. |
