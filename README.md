# lecture2audio

Turns the transcript (or the recording) of a lecture into a clear, well-ordered spoken explanation that you can listen to, and into an Obsidian vault of course material that embeds the audio. It is not a podcast and not a summary: it keeps everything the teacher taught, removes the noise (pauses, sips of water, false starts, "as you can see on the board"), organises the lecture into parts, points and sub-points, announces each part's points before explaining them, defines each term before its first use, asks you questions at the end of each part, and prepares numbers, formulas and code so that a voice model reads them correctly.

## How it works

```
recording ─► 1 transcribe ─┐
transcript ────────────────┴─► 2 outline ─► 3 rewrite ─► 4 verify ─► 5 spoken form ─► 6 voice ─► m4a
                                 (whole       (each part, in parallel)      │
                                  lecture)        ▲   code gates first,     │
                                                  └── then the verifier; ───┘
                                                      keep the best attempt
```

1. **Transcribe** (optional). Whisper through `mlx-whisper` (Apple Silicon) or `faster-whisper`. The glossary is passed as a hint for technical words. The transcript is saved so that you can correct it by hand.
2. **Outline** (`prompts/outline.md`). The transcript is cut into numbered units of about one sentence. One model call groups them into a tree of at most three levels (part, point, sub-point) and marks the units that are noise. Code then checks the tree: every unit is used exactly once, parts follow the lecture's order, no part is longer than `--max-part-words`, and a node with a single child is folded into its parent. Inside a part, the model may move a passage under the point it belongs to (when the teacher comes back to it); every such move is listed in `report.md`. If the model's outline keeps failing the checks, parts are cut by size instead, and the report says so.
3. **Rewrite** (`prompts/rewrite.md`), one part at a time. The model writes one section per node of the outline, a preview phrase for each point, a recap and two or three questions. Every sentence is tagged `lecture` (taught by the teacher) or `added` (a transition, a definition's purpose, a flag such as "the recording is unclear here"). Code assembles the spoken previews ("This part has three points. First, ... Second, ..."), so they always match the sections. Formulas and code go verbatim into `reference.md`.
4. **Verify** (`prompts/verify.md`, a different model by default). Code checks first: the sections must match the outline exactly, and a number in a `lecture` sentence must appear in the transcript or the slides. The verifier then gives a status for every node (complete, incomplete, misplaced) and lists faithfulness, definition, recap, question and listening problems. A failing part is rewritten with the remarks, at most twice. The best attempt is kept, because a repair can make a correct script worse. A part that still fails is flagged in `report.md`.
5. **Spoken form** (`prompts/spoken.md`, then `spoken.py`). The model writes numbers, symbols, formulas and code as words; then rule-based code guarantees that no digit or symbol is left (English and French).
6. **Voice.** Each block is cut at sentence ends, synthesised, cached by its text, joined with pauses (longer between parts, a silence after each quiz question), loudness-normalised, and written as m4a with one chapter per part, per point, and per "summary and questions". Each part is also written as its own recording (`parts/`), for the player at the top of its note in the vault.
7. **Course material** (`prompts/course.md`, `course.py`, `course_render.py`). The verified script of each part becomes cards (one idea each: formal definition, concrete instance, full explanation, worked example, figure, check questions). Figures are one to eight still frames, each a Mermaid diagram (architecture, state machines, protocol exchanges) or an ASCII box drawing (bit layouts, memory, registers, pointer traces). Code checks the cards, a second model judges every card, figure and table against the sentences it covers, and the vault is written for Obsidian: one note per part with its audio player, collapsed prerequisite reminders and collapsed answers, one note per term, an index with the whole recording, and a "Full Lecture" note that transcludes the parts so the lecture reads as one chapter.

**The idea ledger** (`ideas.csv`) links every stage. One call per part (`prompts/ideas.md`, in parallel) lists every atomic idea the teacher taught, with the units that say it (T3.12 is the twelfth idea of part 3), so the list is never cut short by an output limit on a long lecture. Each idea is written to `out/<name>/ideas.csv` with its node and its transcript units, status `pending`; the verifier checks it off against the script (`scripted` or `missing_script`); the course judge checks it off against the cards and names the card that teaches it (`covered_in_card`, `verified` or `missing_cards`). `report.md` lists every idea that did not reach the end, and every part that has no idea at all (a sign that the list was cut short). `--export-anki` turns the ledger and the check questions into `anki.csv` (Front, Back, Tags) for spaced repetition.

Two checks cover the whole lecture. Once per run, an omission is planted in an accepted part and the verifier is asked again: if it does not notice, `report.md` says that its passes are less trustworthy. The report also notes terms that the outline planned but no part defined, and warns when more than 20% of the words were marked as noise; all noise is listed at the end of the report so that you can check it.

## Install

```bash
cd ~/code/lecture2audio
uv venv .venv && uv pip install -e ".[voice,whisper-mac,pdf]"   # whisper instead of whisper-mac off a Mac
brew install ffmpeg                   # needed for the audio
export DEEPSEEK_API_KEY=...           # the default model, and the web search for the gaps
export L2A_VAULT_DIR=~/Obsidian/Courses   # optional: write the notes into your Obsidian vault
```

Defaults, chosen for quality: DeepSeek V4.1 Flash writes (thinking mode on), DeepSeek V4 Pro checks, DeepSeek's search API researches the gaps; speech recognition (Whisper large-v3) and the voice (Ming-3.0-tts-0.5B through mlx-audio, `mlx-community/Ming-3.0-tts-0.5B-bf16`) run on your Mac. Obsidian is the interface for reading, the graph of terms, and review.

Optional parts:

| Need | Install |
|---|---|
| Ming voice (local, default, Apple chips) | `uv pip install -e ".[voice]"` |
| Transcribe recordings, Apple Silicon | `pip install -e ".[whisper-mac]"` |
| Transcribe recordings, other machines | `pip install -e ".[whisper]"` |
| Slides as PDF | `uv pip install -e ".[pdf]"` |
| Slides as PowerPoint | `uv pip install -e ".[pptx]"` |

## Use

Try the whole flow without any key or model (the "dry" model keeps the text as it is, the Mac voice reads it):

```bash
lecture2audio build examples/sample_transcript.txt --llm dry --tts say --language en --max-part-words 60
```

A real run, from one folder that holds the recording or the transcript, the slides and a glossary (found by extension and name: `.m4a .mp3 .wav .mp4`, `.txt .srt .vtt`, `.pdf .pptx` or `slides.md`, `glossary.txt`):

```bash
lecture2audio ./week-03/
```

Without a command, `build` runs. With `L2A_VAULT_DIR` set (or `--vault-dir`), the notes go into your vault and Obsidian opens the lecture's index when the run ends (`--no-open-obsidian` to stop that). The same with explicit files:

```bash
lecture2audio build lecture.txt --slides slides.pdf --glossary glossary.txt --export-anki
```

With OpenAI, or a local server such as Ollama:

```bash
lecture2audio build lecture.txt --llm openai
lecture2audio build lecture.txt --llm local --rewrite-model qwen3:32b
lecture2audio build lecture.txt --llm anthropic --verify-llm openai   # verifier from another family
```

French lecture: `--language fr` (detected automatically by default). From a recording, correcting the transcript first:

```bash
lecture2audio build lecture.m4a --glossary glossary.txt --transcribe-only
# edit out/lecture/transcript.txt, then run the same command without --transcribe-only
```

To look at the script before spending time on audio: `lecture2audio text lecture.txt ...` stops before the voice.

Results are in `out/<name>/`:

| File | What it is |
|---|---|
| `<name>.m4a` | The audio, with chapters for parts, points, and summary and questions |
| `index.md` | The tree of the lecture with the time each part and point starts. A map, not notes |
| `script.md` | The script, organised by the outline; added sentences in italics |
| `questions.md` | The questions of every part, answers at the end, to test yourself on a later day |
| `reference.md` | Formulas, code and tables, copied verbatim |
| `report.md` | Open problems, retries, the verifier test, the ideas that did not reach the cards, order changes, model use, everything removed as noise |
| `ideas.csv` | The idea ledger: `idea_id,node_id,kind,statement,source_units,covered_in_card,status` |
| `anki.csv` | With `--export-anki`: Front, Back, Tags, for Anki's File > Import |
| `vault/` | The course material for Obsidian (or in `--vault-dir`), with the recordings in `audio/`; `--no-course` skips it |

Intermediate results are in `outline.json`, `rewrite/`, `spoken/` and `audio/`; a rerun reuses them. You can edit `outline.json` or `rewrite/part_003.json` by hand and run again with `--redo rewrite` or `--redo spoken`. `--redo transcribe|outline|rewrite|spoken|audio` discards that stage and the next ones. If the transcript or `--max-part-words` changes, old results are discarded automatically.

## Options

- **Language models.** `--llm deepseek` (default: `deepseek-flash`, V4.1 Flash with thinking mode, for the outline, the rewrite and the spoken form, `deepseek-v4-pro` for the verifier; `DEEPSEEK_API_KEY`). `--llm anthropic` uses `claude-sonnet-5-5` and `claude-opus-5-5`. Change models with `--rewrite-model`, `--verify-model` or the environment variables `L2A_REWRITE_MODEL`, `L2A_VERIFY_MODEL`). `--llm local --rewrite-model <model>` talks to any OpenAI-compatible server on your machine (Ollama by default, `--base-url` to change). The verifier can use another provider: `--verify-llm openai --verify-model <model>` or `--verify-llm local ...`. Judges tend to favour text written by their own model family, so a verifier from another family is a stronger check.
- **Structure.** `--max-part-words` (default 2500) sets the longest part. `--workers` (default 3) sets how many parts are processed at the same time. `--max-retries` (default 2) sets how many repairs a part may get. `--no-audit` skips the planted-omission test.
- **Gaps and terms.** Before the rewrite, the program lists what the lecture leaves out: slide ideas never taught, terms never defined, fundamentals taken for granted, and skipped steps. It then searches the web for every gap and keeps a fact only if its quote is found word for word in the page. With `--llm anthropic` or `openai` the provider's own web search is used. With any other model that can call tools (`--llm deepseek`, `kimi`, `openrouter`, `compat`, `local`), the program runs the searches and reads the pages itself, through a search engine of your choice (`--search deepseek`, `tavily`, `brave`, or a free self-hosted `searxng`). The audio teaches these facts and opens each such passage with a short spoken phrase; `script.md` and `reference.md` list the sources. Every technical term gets one definition (`glossary.md`), is defined at its first use, and is checked in every sentence, so that a near word never replaces it. `--research off` keeps the gap list but skips the web. Every gap is researched; `--research-depth` (default 30) sets how many searches and page reads each gap may use. `--redo gaps` or `--redo research` runs these stages again.
- **Questions.** `--quiz-pause` (default 8 seconds) sets the silence after each question. `--no-quiz` leaves the questions out of the audio; they stay in `questions.md`.
- **Voice.** `--tts ming` (Ming-3.0-tts, local, default; `--voice US_James`, `US_Emma`, `UK_George`, `UK_Charlotte`, `US_Stella`, or `file.wav` to imitate that voice), `say` (macOS built-in, lower quality), `openai` (`OPENAI_API_KEY`), `fake` (tones, for tests), or `command` for any other model:
  `--tts command --tts-command "python my_voice.py --text-file {text_file} --out {out} --language {language}"`. The script must write a WAV file to `{out}`.
- **Prompts.** `src/lecture2audio/prompts/*.md` are plain files; edit them, or copy the folder and pass `--prompts-dir`.

## Why it is built this way

The structure of the audio follows research on learning. Previews and spoken structure statements improve retention (signaling: Schneider et al. 2018, 103 studies; Chaudron and Richards 1986 for lectures). Removing extraneous material and introducing terms before the explanation follow Mayer's coherence and pre-training principles. The questions at the end of each part, with a silence before the answer, follow the strongest result on retention: being tested (Yang et al. 2021, 222 classroom studies; Dunlosky et al. 2013).

The architecture follows 2026 work on language-model systems. It is a fixed chain of calls with code checks between them rather than agents that talk to each other, because recent controlled studies found that a single strong model matches or beats multi-agent setups at equal cost, and this task is sequential. Code gates run before the model judge because they are free and catch most format faults. The retry loop keeps the best attempt because repeated repairs can damage a correct output. The planted-omission test exists because a verifier can drift into passing everything without anyone noticing. `docs/research-notes.md` gives every source, what it found, and what it changes in the build, plus the ideas not built yet.

## Status

Tested automatically (no key or model needed, see `CLAUDE.md` for the command): the outline checks (coverage, order, depth, size, folding, moves, noise assignment, fallback), the structure gates, the verifier's per-node statuses, the retry loop that keeps the best attempt, the planted-omission test, parallel parts, caching, previews in English and French, the spoken blocks, number and symbol conversion, chunking, joining with pauses, chapters, the idea ledger and the Anki export, the folder input, the vault (figures, audio embeds, the continuous reading note), and the whole command line with the dry model and the fake voice.

Not tested yet, because they need keys, models or a Mac: the real calls to the Anthropic, OpenAI and local servers; the `say` and OpenAI voices; `mlx-whisper` and `faster-whisper`; the round trip; PDF and PowerPoint slides; opening Obsidian. The prompts have not yet been tuned on real lectures: run one lecture, read `report.md` and `index.md`, listen, and adjust the prompts where the result is not what you want.
