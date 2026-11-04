---
name: lecture2audio
description: Turn a lecture (recording or transcript, with optional slides and glossary, all in one folder) into a verified spoken script, Ming-3.0 audio with chapters, an Obsidian vault of course notes that embeds the audio, an idea ledger (ideas.csv) and optionally Anki cards. Use when the user asks to process, transcribe, study, convert or "make notes and audio" from a lecture, a course folder, a class recording or lecture slides.
---

# lecture2audio: one command per lecture

## The command

Put the lecture's files in one folder, then run:

```bash
.venv/bin/python -m lecture2audio ./lecture_folder/
```

No command means `build`. The folder is read by extension and name (`sources.discover_inputs`):

| Input | Found as |
|---|---|
| Transcript | `.srt`, `.vtt`, `.txt`, `.md` (a name containing "transcript" wins); preferred to a recording |
| Recording | `.m4a`, `.mp3`, `.wav`, `.mp4`, ... (the largest one) |
| Slides | `.pdf`, `.pptx`, or a `.md`/`.txt` whose name contains "slides" |
| Glossary | a `.txt`/`.md`/`.csv` whose name contains "glossary", "terms", "lexique" |

The log line `[inputs]` says which files were used and which were skipped. Give `--slides`, `--glossary` or the file itself as the input to choose another.

Useful options:

- `--vault-dir ~/Obsidian/Courses` (or env `L2A_VAULT_DIR`): write the notes into the user's Obsidian vault; Obsidian then opens the lecture's index at the end (macOS; `--no-open-obsidian` to prevent it).
- `--export-anki`: also write `anki.csv` (Front, Back, Tags).
- `text` instead of the implicit `build`: stop before the voice (script, vault, reports only).
- `--transcribe-only`: stop after `transcript.txt`, so misheard terms can be corrected; run the same command again afterwards.
- `--language fr` for a French lecture (English is the default).
- `--llm dry --tts fake`: a run with no key and no model, to check the setup.
- `series lecture1/ lecture2/ ...`: several lectures as one course with shared terms.

The default models are DeepSeek (`DEEPSEEK_API_KEY`); `--llm anthropic` needs `ANTHROPIC_API_KEY`. The default voice, Ming-3.0-tts through mlx-audio, runs only on Apple chips; elsewhere use `--tts openai` or `--tts say`.

## What to read after a run

Everything is in `out/<folder name>/`:

1. `report.md`: open problems, the verifier test, and the section "Idea ledger", which lists every idea of the lecture that did not reach the cards. Report these to the user first.
2. `ideas.csv`: one row per atomic idea (`idea_id,node_id,kind,statement,source_units,covered_in_card,status`). Status `verified` means a card teaches it and a judge confirmed it; `missing_script`, `missing_cards` or `pending` need a look at the transcript units given.
3. `vault/` (or the `--vault-dir` folder): the notes. `<name> – Index.md` embeds the full recording and links every part; `<name> – Full Lecture.md` reads the lecture as one chapter.
4. `<name>.m4a`: the whole audio with chapters (`ffprobe -v error -show_chapters <file>.m4a`).

To redo a stage after editing its input, add `--redo outline|gaps|research|rewrite|course|spoken|audio`.

## Rules for the agent

- Run the command once and wait for it to finish; a long lecture takes many minutes. Do not start a second run on the same output folder at the same time.
- Do not edit `ideas.csv` by hand: it is rebuilt from the stage results on every run.
- Never print or store API keys; they are read from the environment only.
