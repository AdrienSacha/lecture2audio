# The stages of a run

The stages and their order are fixed by code (`src/lecture2audio/stages.py`); no model picks the
next stage. Each stage caches its result under `out/<run>/`, so running the same command again
reuses every finished stage. `--redo <stage>` discards that stage and every stage that depends
on it. `events.jsonl` holds one line per change of stage: `{"stage": "outline", "state":
"running"}`, then `{"stage": "outline", "state": "done", "seconds": 12.3}`. A stage that is
`running` with no process behind it means the run stopped there; the same command continues it.

| Stage | Needs | Cache under `out/<run>/` | `--redo` | What it does |
|---|---|---|---|---|
| slides | the input | `slides/` | yes | Reads each slide by sight (formulas as LaTeX, figures described); only with a model that reads images. |
| transcribe | the input | `transcript.txt` | yes | Speech recognition of a recording, or the transcript or slides as given. Correct misheard terms here, then run again. |
| outline | transcribe, slides | `outline.json` | yes | Parts, points and sub-points; every transcript unit used once (in a node or as noise). |
| gaps | outline | `coverage.json`, `coverage_audit.json`, `termbase.json` | yes | Gap search over the whole lecture, its planted-hole audit, and one definition per term. |
| research | gaps | `research/` | yes | Facts from the web for each gap; every quote is found word for word in a page by code. |
| ideas | outline | `ideas/`, `ideas_audit.json` | yes | Atomic ideas, one call per part, and the planted-fact audit. Seeds `ideas.csv`. |
| rewrite | outline, gaps, research, ideas | `rewrite/`, `audit.json` | yes | The script of each part, verified idea by idea, bounded repairs, the planted-omission audit. |
| factcheck | rewrite, ideas | `factcheck/` | no | MiniCheck on each lecture sentence (reported, never acted on). |
| spoken | rewrite | `spoken/` | yes | The exact words for the voice. |
| audio | spoken | `audio/` | yes | Voiced chunks, the voice round trip, the joined recording with chapters. |
| parts | audio | `parts/` | no | One recording per part for the vault. |
| course | rewrite, ideas | `course/` | yes | Cards, exercises, the judge, the cold read and one revision. |
| vault | course | `vault/` | no | The Obsidian notes. |
| recitation | rewrite, ideas, course | `recitation/` | yes | Recitation notes of each part, as blocks checked by code. |
| project | outline, ideas, gaps | `project.json` | yes | One thing to build in checkable steps (only with `--project`). |
| documents | recitation, project | `recitation.tex`, `recitation.pdf`, `project.tex`, `project.pdf` | no | LaTeX typeset by code, PDFs rebuilt only when the `.tex` changed. |
| ledger | ideas, rewrite, course | `ideas.csv`, `anki.csv` | no | The idea ledger and the Anki export, rebuilt from the stage results on every run. |

`lecture2audio text` stops before `spoken`, `audio` and `parts`.
