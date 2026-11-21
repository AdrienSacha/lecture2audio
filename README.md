# lecture2audio

lecture2audio turns a lecture (recording, transcript, slides, or slides alone) into a spoken explanation, LaTeX recitation notes and an Obsidian course vault, then checks that nothing the teacher taught was dropped. It does not summarize. Only noise is removed, and gaps in the lecture are filled from web pages whose quotes are checked by code.

## What it produces

Under `out/<name>/`:

- `script.md` and `<name>.m4a`: the spoken script and its audio, with one chapter per part and point.
- `recitation.pdf` (from `.tex`): the lecture re-taught as a handout with definitions, proofs, worked examples and exercises.
- `vault/`: course notes for Obsidian that embed the audio.
- `ideas.csv`: the idea ledger, one row per atomic idea (`idea_id,node_id,kind,statement,source_units,covered_in_card,status`).
- `report.md`: what was checked, repaired and left open, including every idea that did not make it to the end.

## How it works

The pipeline is a fixed chain of model calls with code checks between them. The stage graph is declared in `src/lecture2audio/stages.py`.

```
transcribe -> outline -> gaps -> research -+
                 |                         v
                 +--> ideas -----------> rewrite (per part, parallel) -> spoken -> audio
                                           |
                                           +--> course -> vault / recitation / ideas.csv
```

Checks along the way:

- Code gates first. Every model reply must pass a format and consistency check before any model judges it. The outline, for instance, must use every transcript unit exactly once, and a number in a "lecture" sentence must appear in the source.
- A second model checks each part. The verifier gives a status for every node of the outline against the transcript. By default it is a different model from the writer (DeepSeek V4 Pro checks V4.1 Flash), and `--verify-llm` can point it at another provider. A failing part gets at most two repairs, and the best attempt is kept.
- Verdict computed by code. The verifier must answer for every node. The pass or fail result is derived from those statuses and the issues, never taken from the model's own verdict.
- Idea ledger. One call per part lists the atomic ideas, each tied to its transcript units. Every script sentence taken from the lecture must cite idea ids, so code can find an idea that no sentence cites. The ledger is updated as each part finishes its script and its cards, and `report.md` lists ideas that did not arrive.
- Gap research with quote checking. Slide ideas never taught, undefined terms and skipped steps are searched on the web. A fact is accepted only if its quote appears word for word in a page that was actually returned or fetched (`enrich.check_facts`). Such sentences are tagged as external with a source and announced aloud.
- Planted-omission audits. Once per run a fault is planted and the checker is asked again: an omission in an accepted script, a blanked unit for the gap search, an extra fact for the idea listing, an emptied card for the course judge. If a checker misses it, `report.md` says its passes are less trustworthy. `--no-audit` skips them.

Two optional second opinions are only reported: MiniCheck on each sentence against its source units, and a voice round trip that transcribes the audio back. The round trip does re-voice chunks it heard wrong.

## Why a fixed chain and not interacting agents

The task is sequential. The studies I rely on report that a single strong model matches multi-agent setups at equal token budget, and that letting a model pick the next stage lowers repeated-run success. See `docs/adr/0001-pipeline-architecture.md` and `docs/adr/0003-declared-stage-graph.md`, with sources in `docs/research-notes.md`.

## Quick start

```bash
git clone https://github.com/AdrienSacha/lecture2audio && cd lecture2audio
uv venv .venv && uv pip install -e .
brew install ffmpeg   # needed for the audio
```

Dry run, with no key and no model (the "dry" model passes text through and tones stand in for the voice):

```bash
.venv/bin/python -m lecture2audio build examples/sample_transcript.txt \
  --llm dry --tts fake --language en --max-part-words 60 --out /tmp/l2a
```

Then read `/tmp/l2a/sample-transcript/report.md` and `ideas.csv`.

The local app serves one page on 127.0.0.1:8765 and runs the same command line:

```bash
.venv/bin/python -m lecture2audio app
```

A real run on a folder holding the recording or transcript, slides and a glossary:

```bash
export DEEPSEEK_API_KEY=...
.venv/bin/python -m lecture2audio ./week-03/
```

Voice, Whisper and PDF reading are optional extras (`".[voice,whisper-mac,pdf]"`, see `pyproject.toml`). PDFs need LuaLaTeX with latexmk, or tectonic. `text` in place of `build` stops before the voice.

## Providers

| `--llm` | Key variable | Notes |
|---|---|---|
| `deepseek` (default) | `DEEPSEEK_API_KEY` | Flash writes with thinking on, V4 Pro checks |
| `anthropic` | `ANTHROPIC_API_KEY` | `claude-sonnet-5-5` and `claude-opus-5-5` by default |
| `openai` | `OPENAI_API_KEY` | `gpt-6.1-sol` and `gpt-6-astra` by default |
| `openrouter`, `kimi`, `compat` | `OPENROUTER_API_KEY`, `MOONSHOT_API_KEY`, `L2A_COMPAT_API_KEY` | `--rewrite-model` is required; `compat` also needs `--base-url` |
| `local` | none | Any OpenAI-compatible server, with `--rewrite-model` |

## Use from other agents

lecture2audio can be driven by another agent in two standard forms (ADR-0008).

- **Agent Skill**: `skills/lecture2audio/` follows the [Agent Skills specification](https://agentskills.io/specification). Copy or link it into the agent's skills folder (for Claude Code, `~/.claude/skills/lecture2audio`). Its script `scripts/run.py` runs the command line and prints a short summary of the run; `references/` explains the stages, the options and every output file.
- **MCP server**: install the extra with `uv pip install -e '.[mcp]'`, then register `lecture2audio mcp --out /absolute/path/to/out` in the host. In Claude Code: `claude mcp add --scope user lecture2audio -- /path/to/.venv/bin/lecture2audio mcp --out /path/to/out`. In Cursor or Claude Desktop, add the block of `skills/lecture2audio/assets/mcp.json` to `mcp.json` or `claude_desktop_config.json`. The tools are `build` (returns a run name at once), `status`, `list_runs`, `read_result` and `stop_run`; the result files are also resources at `lecture2audio://runs/{run}/{file}`.

API keys never pass through the agent: the runs read them from the environment, as the command line does.

## Status

- The test suite has 278 tests and runs offline with no key and no model; it needs ffmpeg. One test is skipped. Run it with `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests`.
- Not tested: real calls to any provider (the tests use the dry model and a scripted harness), real voices, and Whisper transcription.
- The prompts have not been tuned on real lectures. `tests/real_data.py` can fetch MIT OpenCourseWare material to try them on.
- There are no evaluation numbers and no baseline yet. The planted-omission audits tell you whether a checker noticed one fault in a given run, not how often the pipeline loses an idea.
- Slide PDFs lose formulas in their text layer. Reading slides by sight needs a provider that accepts images.

MIT license. Further design notes are in `docs/`.
