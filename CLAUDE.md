# lecture2audio

Turns a lecture transcript into a clear spoken explanation for studying. It is not a summarizer: everything the teacher taught must stay in the script. Only noise is removed. Gaps the lecture leaves (untaught slide ideas, undefined terms, assumed fundamentals, skipped steps) are filled from web sources whose quotes code has checked, and marked as such.

# Commands

- All tests (no key, no model, needs ffmpeg): `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests`
- One test file: `PYTHONPATH=src python3 -m unittest tests.test_outline`
- Whole pipeline without models: `PYTHONPATH=src python3 -m lecture2audio build examples/sample_transcript.txt --llm dry --tts fake --language en --max-part-words 60 --out /tmp/l2a`, then read `/tmp/l2a/sample-transcript/report.md` and `index.md`
- One folder, no flags: `PYTHONPATH=src python3 -m lecture2audio ./lecture_folder/ --llm dry --tts fake` (no command means `build`; the folder's recording or transcript, slides and glossary are found by `sources.discover_inputs`)
- The vault and the ledger of a run: `/tmp/l2a/sample-transcript/vault/` (open it as an Obsidian vault) and `/tmp/l2a/sample-transcript/ideas.csv`
- Chapters of the result: `ffprobe -v error -show_chapters <file>.m4a`
- After any change: run the tests and the dry build above. The work is done when both pass.

# Conventions

- Use the latest official SDKs and state-of-the-art libraries rather than hand-written code (user's decision, November 2026): the `openai` SDK for every OpenAI-compatible API (OpenAI, DeepSeek, Kimi, OpenRouter, local servers) and the `anthropic` SDK for Claude, with their streaming, retries and typed errors. Add a dependency when it is the best tool for the job; pin a minimum version in `pyproject.toml` and install with `uv` into `.venv`. Prefer the most efficient correct code.
- Heavy model packages (mlx-audio, mlx_whisper, faster_whisper, pypdf) are imported inside the function that needs them, so the text pipeline starts fast and runs on machines without them.
- Prompts live in `src/lecture2audio/prompts/*.md`. The JSON each prompt asks for is checked by a gate in `pipeline.py`, `outline.py` or `enrich.py` (`validate_part`, `validate_verify`, `validate_spoken`, `parse_outline`, `validate_coverage`, `validate_idea_list`, `validate_termbase`, `validate_research`, `validate_uses`). When a prompt's output format changes, change its gate, `dry_run_handler` in `llm.py`, and the tests in the same change.
- `dry_run_handler` and the test harness recognise each prompt by a phrase in it: "You build the outline", "strict reviewer", "exact words a voice should say", "You find the gaps", "You list the atomic ideas", "You build the termbase", "You research one gap", "You check terminology". `test_pipeline.Harness` sends any unknown prompt to the rewrite handler. Keep these phrases when editing prompts.
- Languages: English and French. Any fixed wording spoken by the program (part titles, previews, quiz) is in `WORDING` in `pipeline.py`; add both languages together.
- Every stage caches its result under `out/<name>/` (`outline.json`, `coverage.json`, `ideas/`, `termbase.json`, `research/`, `rewrite/`, `audit.json`, `spoken/`, `audio/`, `parts/`, `course/`). A new stage needs its own cache and an entry in `LATER` in `cli.py`. `ideas.csv` and `anki.csv` are not caches: they are rebuilt from the stage results on every run (`ledger.py`).
- The idea ledger (`ideas.csv`, columns `idea_id,node_id,kind,statement,source_units,covered_in_card,status`) is the thread through the stages: `ledger.Ledger` is seeded after `run_ideas` (one call per part, ids scoped to the part: `T3.12`) and rewritten as each part finishes its script (`run_parts(on_done=...)`) and its cards (`CoursePipeline.run(on_done=...)`); each update rebuilds every row from the seed and the results so far (`apply_script`, `apply_cards`) and replaces the file atomically, so it is current and never says more than the saved results. A stage that sees ideas must answer for every one of them (`validate_ideas`, `validate_judgement`); `report.md` lists every idea that did not arrive.
- A figure frame has exactly one of `mermaid` and `ascii` (`course_schema.Frame`); the template wraps each frame in a `[!figure]` callout with a ```mermaid or ```text block.
- Output files written for the user (`script.md`, `index.md`, `questions.md`, `glossary.md`, `report.md`, the vault notes) use full sentences in a formal register.
- Obsidian is the only interface (the browser GUI was removed in November 2026). Vault templates are in `src/lecture2audio/assets/*.j2`; labels in both languages are in `LABELS` in `course_render.py`.

# Design decisions (keep unless the user decides otherwise)

Full sources and the ideas not built yet: `docs/research-notes.md`. Update it when a decision changes.

- A fixed chain of model calls with code checks between them, not agents that talk to each other. 2026 studies found that a single strong model matches or beats multi-agent setups at equal token budget (arXiv 2601.12307, 2604.02460, 2606.05670), and this task is sequential.
- One outline call plans the whole lecture. Parts are then independent, so they run in parallel (`--workers`).
- The outline never moves material across parts, and every move inside a part is logged in `report.md`. Every unit is used exactly once (in a node or in noise); noise is listed in full at the end of `report.md`.
- At most three levels: part, point, sub-point. A node with a single child is folded into its parent by code.
- Code gates run before any model judge, because they cost nothing and catch most format faults (arXiv 2609.31937).
- The verifier must give a status for every node of the checklist; the verdict is computed by code from those statuses and the issues, never taken from the model.
- The retry loop keeps the best attempt and replaces it only by a strictly better one, and feedback is built from the best attempt, because a repair can damage a correct script (arXiv 2607.17641). Default: two repairs at most.
- One planted-omission test per run checks that the verifier is not passing everything (arXiv 2610.11464). Do not remove it to save a call. The same holds for every other judge of completeness: the gap search (a blanked unit, `audit_coverage`), the idea listing (a planted fact, `audit_ideas`) and the course judge (an emptied card, `CoursePipeline.audit_judge`, scored on the judge's own verdict, not on the code checks after it). `--no-audit` skips them all.
- A judge's "taught" is checked by code: the numbers of the idea that the transcript holds must be in the script (`_verify`) or in the cards named, figures, tables and code included (`_judge`, `numbers_lost`; exponents and indices are skipped because they are said in words). When they are not, code does not overrule the judge: the idea is `disputed` in the ledger, listed in `report.md`, and the issue `idea_numbers` is soft. An audit scores the judge's own verdict (`judged`), never the code checks after it. A failed audit (model error) is not cached, so it runs again next time.
- Previews ("This part has three points. First, ...") are assembled by code from the model's phrases, so they always match the sections.
- Gaps (slide idea not taught, term never defined, fundamental assumed, step skipped) are filled only with facts found online whose quote code finds word for word in a page (`check_facts`). Such sentences are tagged `external` with a source id, marked aloud by a natural phrase once per passage, and listed in `script.md` and `reference.md`. Research is `native` (the provider's own web search, Anthropic and OpenAI) or `loop` (the program runs `web_search` and `fetch_page` for any model that calls tools: DeepSeek, Kimi, OpenRouter, any compatible API, local; engines in `web.make_search`). Every gap is researched; `--research-depth` sets the tool calls per gap, there is no cap on gaps.
- One definition per term (`termbase.json`). Every term a definition relies on has its own entry, and code moves it before the terms that need it (`build_termbase`). Code flags a confusable word the sources never use (`stray_terms`) and a term used before its part; a separate judge checks every occurrence against the definition, because one judge for many criteria mixes them up.

# Gotchas

- `.venv` was created on the user's Mac; it does not run in a Linux sandbox. Use the system `python3` for tests there.
- Run everything with `.venv/bin/python` (or `uv run lecture2audio ...`): the speech and voice packages live only there. The default voice is Ming-3.0-tts-0.5B (MIT) through mlx-audio (`--tts ming`, built-in voice `US_James`); its weights (about 3 GB) live in the Hugging Face cache, `~/.cache/huggingface/hub`. Fish Audio Drama 3 (best open voice) was tried and removed: its 4-bit build needs about 24 GB and swapped this 32 GB Mac.
- Defaults (user's choice, quality first): `--llm deepseek` with `deepseek-flash` (V4.1 Flash, thinking on) writing and `deepseek-v4-pro` checking, DeepSeek search for research, `--language en`, Whisper large-v3, `--tts ming`. For `--llm anthropic`, `claude-sonnet-5-5` and `claude-opus-5-5`; for `--llm openai`, `gpt-6.1-sol` and `gpt-6-astra`; kimi, openrouter, compat and local have no default. Real API calls have not been tested yet for any provider.
- Each hosted provider reads only its own key variable (`PRESETS` in `llm.py`), and the local client never sends `OPENAI_API_KEY` (it uses `L2A_LOCAL_API_KEY`), so no key leaks to another host.
- `--vault-dir` defaults to `L2A_VAULT_DIR`, and then `--open-obsidian` is on: the run ends with `open "obsidian://open?path=..."` (macOS only; elsewhere the link is printed). Tests remove `L2A_VAULT_DIR` from the environment so that they never write into a real vault.
- `--course` is on by default (`--no-course` skips the vault); the audio of each part is encoded a second time into `parts/` and copied into the vault's `audio/`, which doubles the disk use of the audio.
- The atomic ideas are listed one call per part (`run_ideas`, `prompts/ideas.md`), so no output limit cuts the list on a long lecture; the gap search stays one call over the whole lecture because a gap (a term used in part 2, defined in part 5) needs it. A part whose list keeps failing its gate is left empty, named in `report.md` and not cached. The architecture and its open risks are in `docs/adr/0001-pipeline-architecture.md`.
