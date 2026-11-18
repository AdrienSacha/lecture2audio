# lecture2audio

Turns the transcript (or the recording) of a lecture into a clear, well-ordered spoken explanation that you can listen to, and into an Obsidian vault of course material that embeds the audio. It is not a podcast and not a summary: it keeps everything the teacher taught, removes the noise (pauses, sips of water, false starts, "as you can see on the board"), organises the lecture into parts, points and sub-points, announces each part's points before explaining them, defines each term before its first use, asks you questions at the end of each part, and prepares numbers, formulas and code so that a voice model reads them correctly.

**Quick start** (no key, no model): `uv venv .venv && uv pip install -e .`, then `.venv/bin/python -m lecture2audio build examples/sample_transcript.txt --llm dry --tts fake --max-part-words 60` (needs `ffmpeg`), or `.venv/bin/python -m lecture2audio app` for the local web page on `127.0.0.1:8765`. A real run needs a provider key (see Install). Real provider calls and the real voices have not been tested; see Status.

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
4. **Verify** (`prompts/verify.md`, a different model by default). Code checks first: the sections must match the outline exactly, and a number in a `lecture` sentence must appear in the transcript or the slides. The verifier then gives a status for every node (complete, incomplete, misplaced) and lists faithfulness, definition, recap, question and listening problems. A failing part is rewritten with the remarks, at most twice. When the problems lie in some sections only, only those sections are rewritten and the program keeps the others word for word, so a repair cannot damage what passed. The best attempt is kept, because a repair can make a correct script worse; `report.md` lists every repair and what it changed. A part that still fails is flagged in `report.md`.
5. **Spoken form** (`prompts/spoken.md`, then `spoken.py`). The model writes numbers, symbols, formulas and code as words; then rule-based code guarantees that no digit or symbol is left (English and French).
6. **Voice.** Each block is cut at sentence ends, synthesised, cached by its text, joined with pauses (longer between parts, a silence after each quiz question), loudness-normalised, and written as m4a with one chapter per part, per point, and per "summary and questions". Each part is also written as its own recording (`parts/`), for the player at the top of its note in the vault.
7. **Course material** (`prompts/course.md`, `course.py`, `course_render.py`). The verified script of each part becomes cards (one idea each: an "In short" text in everyday words, formal definition, concrete instance, full explanation, worked example, proof when the lecture gives one, figure, check questions), and each part ends with exercises with full worked solutions, as in a university recitation. Figures are one to eight still frames, each a Mermaid diagram (architecture, state machines, protocol exchanges) or an ASCII box drawing (bit layouts, memory, registers, pointer traces). Code checks the cards, a second model judges every card, figure and table against the sentences it covers (a repair rewrites only the cards of the sections at fault and keeps the others word for word), and the vault is written for Obsidian: one note per part with its audio player, collapsed prerequisite reminders and collapsed answers, one note per term, an index with the whole recording, and a "Full Lecture" note that transcludes the parts so the lecture reads as one chapter.

**Grounding by construction.** Every lecture sentence of the script names the ideas it states, so code proves that every idea is taught before any model judges the script, and a small local fact-checker of another model family (MiniCheck, optional) checks each sentence against the transcript units it comes from. A simulated first-year reader reads the notes and every place where it gets lost goes back to the writer once. The voice is transcribed back chunk by chunk, and a chunk heard wrong is voiced again.

**The idea ledger** (`ideas.csv`) links every stage. One call per part (`prompts/ideas.md`, in parallel) lists every atomic idea the teacher taught, with the units that say it (T3.12 is the twelfth idea of part 3), so the list is never cut short by an output limit on a long lecture. Each idea is written to `out/<name>/ideas.csv` with its node and its transcript units, status `pending`; the verifier checks it off against the script (`scripted` or `missing_script`); the course judge checks it off against the cards and names the card that teaches it (`covered_in_card`, `verified` or `missing_cards`). `report.md` lists every idea that did not reach the end, and every part that has no idea at all (a sign that the list was cut short). `--export-anki` turns the ledger and the check questions into `anki.csv` (Front, Back, Tags) for spaced repetition.

Checks cover the checkers themselves. Once per run, a fault is planted for each judge of completeness and the judge is asked again: an omission in an accepted script (the verifier), a blanked unit (the gap search), an extra fact (the idea listing), and an emptied card (the course judge). If one does not notice, `report.md` says that its passes are less trustworthy. The report also notes terms that the outline planned but no part defined, and warns when more than 20% of the words were marked as noise; all noise is listed at the end of the report so that you can check it.

## What you give it, what you get

Give it whatever you have; it detects what the files are.

| You have | It does |
|---|---|
| A recording (with or without slides) | Transcribes it, then everything below |
| A transcript or captions (`.txt`, `.srt`, `.vtt`, or a transcript as PDF) | Everything below |
| Slides only (`.pdf`, `.pptx`) | Treats the slides as the lecture and researches what they state without explaining |
| A folder holding any of these, and a glossary | Finds each file by its name and type |

It writes, under `out/<name>/`:

- `recitation.pdf` (and `.tex`): the lecture re-taught as a handout, with definitions, results, proofs, worked examples, figures, check questions and exercises with solutions, a glossary and references. Every term, question and citation is a link.
- `project.pdf` with `--project`: one thing to build in about twenty steps, each with a check you can run alone.
- `<name>.m4a`: the spoken explanation, with chapters (`build`; `text` stops before the voice).
- `vault/`: the course notes for Obsidian. `ideas.csv`: every idea of the lecture and where it is taught. `report.md`: what was checked, repaired and left open.

## The app

```
.venv/bin/python -m lecture2audio app
```

or double-click `app/Lecture2Audio.app`. Drop the files on the page, choose the options, follow the stages, then read the PDF and listen in the same page. The server answers only on this machine. Put your API keys in `~/.lecture2audio.env` (`DEEPSEEK_API_KEY=...`) for the double-click launcher, or type one in the page for a single run.

## Install

```bash
git clone <repository-url> && cd lecture2audio
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
| OCR: read scanned or image-only slides (PDF pages without text, pictures in PowerPoint) | `uv pip install -e ".[ocr]"` |
| Parakeet speech recognition (no hallucination over silence) | `uv pip install -e ".[parakeet]"` |
| Local fact-checker (MiniCheck, about 3 GB) | `uv pip install -e ".[factcheck]"` |

## Use

Try the whole flow without any key or model (the "dry" model keeps the text as it is, tones stand in for the voice):

```bash
lecture2audio build examples/sample_transcript.txt --llm dry --tts fake --language en --max-part-words 60
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
| `report.md` | Open problems, every repair and what it rewrote, the planted-fault tests, the ideas that did not reach the cards, order changes, model use, which stages were reused or computed, everything removed as noise |
| `ideas.csv` | The idea ledger: `idea_id,node_id,kind,statement,source_units,covered_in_card,status` |
| `anki.csv` | With `--export-anki`: Front, Back, Tags, for Anki's File > Import |
| `vault/` | The course material for Obsidian (or in `--vault-dir`), with the recordings in `audio/`; `--no-course` skips it |

Intermediate results are in `outline.json`, `rewrite/`, `spoken/` and `audio/`; a rerun reuses them. You can edit `outline.json` or `rewrite/part_003.json` by hand and run again with `--redo rewrite` or `--redo spoken`. `--redo transcribe|outline|gaps|research|ideas|rewrite|spoken|audio|course` discards that stage and every stage that depends on it; the dependencies are declared once, in `src/lecture2audio/stages.py`. If the transcript or `--max-part-words` changes, old results are discarded automatically.

## Options

- **Language models.** `--llm deepseek` (default: `deepseek-flash`, V4.1 Flash with thinking mode, for the outline, the rewrite and the spoken form, `deepseek-v4-pro` for the verifier; `DEEPSEEK_API_KEY`). `--llm anthropic` uses `claude-sonnet-5-5` and `claude-opus-5-5`. Change models with `--rewrite-model`, `--verify-model` or the environment variables `L2A_REWRITE_MODEL`, `L2A_VERIFY_MODEL`). `--llm local --rewrite-model <model>` talks to any OpenAI-compatible server on your machine (Ollama by default, `--base-url` to change). The verifier can use another provider: `--verify-llm openai --verify-model <model>` or `--verify-llm local ...`. Judges tend to favour text written by their own model family, so a verifier from another family is a stronger check.
- **Structure.** `--max-part-words` (default 1500, because long outputs lose faithfulness towards their end) sets the longest part. `--workers` (default 3) sets how many parts are processed at the same time. `--max-retries` (default 2) sets how many repairs a part may get. `--no-audit` skips the planted-omission test.
- **Gaps and terms.** Before the rewrite, the program lists what the lecture leaves out: slide ideas never taught, terms never defined, fundamentals taken for granted, and skipped steps. It then searches the web for every gap and keeps a fact only if its quote is found word for word in the page. With `--llm anthropic` or `openai` the provider's own web search is used. With any other model that can call tools (`--llm deepseek`, `kimi`, `openrouter`, `compat`, `local`), the program runs the searches and reads the pages itself, through a search engine of your choice (`--search deepseek`, `tavily`, `brave`, or a free self-hosted `searxng`). The audio teaches these facts and opens each such passage with a short spoken phrase; `script.md` and `reference.md` list the sources. Every technical term gets one definition (`glossary.md`), is defined at its first use, and is checked in every sentence, so that a near word never replaces it. `--research off` keeps the gap list but skips the web. Every gap is researched; `--research-depth` (default 30) sets how many searches and page reads each gap may use. `--redo gaps` or `--redo research` runs these stages again.
- **Slides and OCR.** A PDF is read for its text layer. A page with almost no text (a scan, a photo of printed slides, a slide pasted in as an image) and every picture of a PowerPoint slide are read by OCR when `.[ocr]` is installed (RapidOCR, pure pip, runs on the CPU, nothing to install outside Python; about two seconds a page, kept in `slides_ocr.json` so a second run does not read them again). `report.md` has a "Slides" section that says which slides were read by OCR and which stayed empty. OCR reads the text of a picture; it does not describe a diagram, and it can misread symbols and formulas. `--no-ocr` skips it. A deck exported with one page per animation step (LaTeX Beamer, PowerPoint builds) lists each slide several times; when every line of a page is on the next page (or begins a longer line there), only the last, complete step is used, and the report counts the pages merged. Not covered: a PDF page that has some text and also a diagram with text inside it is taken as it is.
- **Questions.** `--quiz-pause` (default 8 seconds) sets the silence after each question. `--no-quiz` leaves the questions out of the audio; they stay in `questions.md`.
- **Voice.** `--tts auto` (default) uses Ming-3.0-tts in English and Qwen3-TTS in French, which Ming does not speak. You can also choose one directly: `ming` (local; `--voice US_James`, `US_Emma`, `UK_George`, `UK_Charlotte`, `US_Stella`, or `file.wav` to imitate that voice), `qwen3` (local), `say` (macOS built-in, lower quality), `openai` (`OPENAI_API_KEY`), `fake` (tones, for tests), or `command` for any other model. `--tts-model` picks another Hugging Face model for the local voices. When speech recognition is installed, every chunk is transcribed back (`--roundtrip`, on by default), and a chunk heard wrong is voiced again up to `--voice-retries` times (default 2), keeping the best take. For a command voice:
  `--tts command --tts-command "python my_voice.py --text-file {text_file} --out {out} --language {language}"`. The script must write a WAV file to `{out}`.
- **Speech recognition and checks.** `--stt auto|mlx|faster|parakeet` chooses the recognizer for recordings (Parakeet does not invent words over silence but takes no glossary). Known hallucination phrases and loops are removed and listed in `report.md`. `--factcheck auto|minicheck|off` checks every lecture sentence against the transcript units it cites with MiniCheck, a small local model; its findings are reported, never acted on.
- **Prompts.** `src/lecture2audio/prompts/*.md` are plain files; edit them, or copy the folder and pass `--prompts-dir`.

## Why it is built this way

The structure of the audio follows research on learning. Previews and spoken structure statements improve retention (signaling: Schneider et al. 2018, 103 studies; Chaudron and Richards 1986 for lectures). Removing extraneous material and introducing terms before the explanation follow Mayer's coherence and pre-training principles. The questions at the end of each part, with a silence before the answer, follow the strongest result on retention: being tested (Yang et al. 2021, 222 classroom studies; Dunlosky et al. 2013).

The architecture follows 2026 work on language-model systems. It is a fixed chain of calls with code checks between them rather than agents that talk to each other, because recent controlled studies found that a single strong model matches or beats multi-agent setups at equal cost, and this task is sequential. Code gates run before the model judge because they are free and catch most format faults. The retry loop keeps the best attempt because repeated repairs can damage a correct output, and a repair rewrites only the sections at fault when it can, so that it cannot damage the rest. The stages form a graph declared in code (`stages.py`), and a model never chooses the next stage. The planted-omission test exists because a verifier can drift into passing everything without anyone noticing. `docs/research-notes.md` gives every source, what it found, and what it changes in the build, plus the ideas not built yet.

## Status

Tested automatically (no key or model needed; 260 tests, one skipped, see `CLAUDE.md` for the command), and passing on 18 November 2026 on macOS with the dry model and the fake voice. The local app was also driven over HTTP and in a browser with the dry model (upload, run, stage events, results, and the readable error for a missing key). Request construction, streaming, retries and errors of the DeepSeek, OpenRouter, OpenAI and Anthropic clients are tested against fake servers, not the real APIs. The tests cover: the outline checks (coverage, order, depth, size, folding, moves, noise assignment, fallback), the structure gates, the verifier's per-node statuses, the retry loop that keeps the best attempt, the bounded repair, the planted-fault tests, parallel parts, caching and `--redo`, the stage graph, previews in English and French, the spoken blocks, number and symbol conversion, chunking, joining with pauses, chapters, the idea ledger and the Anki export, the course cards and exercises, the folder input, the vault (figures, audio embeds, the continuous reading note), and the whole command line in English and French.

Not tested yet, because they need keys, models or a Mac: the real calls to every provider (DeepSeek, Anthropic, OpenAI, the others and local servers); the Ming, Qwen3-TTS, `say` and OpenAI voices; `mlx-whisper`, `faster-whisper` and Parakeet; the round trip with real voices; MiniCheck; PDF and PowerPoint slides on real files of yours (OCR has been tested on generated slides only); opening Obsidian. The prompts have not yet been tuned on real lectures: run one lecture, read `report.md` and `index.md`, listen, and adjust the prompts where the result is not what you want.
