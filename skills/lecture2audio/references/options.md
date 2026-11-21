# Options of the command line

`lecture2audio [build|text|series] INPUT [options]`. With no command, `build` runs. INPUT is a
folder (its recording or transcript, slides and glossary are found by name and extension), a
transcript (`.txt .md .srt .vtt`), a recording (`.m4a .mp3 .wav .mp4 ...`) or slides alone
(`.pdf .pptx`). `lecture2audio --help` and `lecture2audio build --help` list every option.

## The ones an agent needs

| Option | Effect |
|---|---|
| `text` (command) | Stop before the voice: script, notes, PDFs and report only. Faster and runs anywhere. |
| `--llm dry --tts fake` | No model and no voice: checks the setup for free. |
| `--llm deepseek` (default), `anthropic`, `openai`, `kimi`, `openrouter`, `compat`, `local` | Model provider. Each reads only its own key variable. |
| `--language en` (default), `fr`, `auto` | Language of the lecture and of the outputs. |
| `--research off` | Do not search the web for the gaps; they are only listed in `report.md`. |
| `--redo STAGE` | Discard a stage and everything after it (see `stages.md`). |
| `--transcribe-only` | Stop after `transcript.txt`, so misheard terms can be corrected; then run again. |
| `--slides FILE`, `--glossary FILE` | Choose files other than the ones found in the folder. |
| `--out DIR` | Output folder (default `./out`). |
| `--name NAME` | Run name (default: the input's folder or file name). |
| `--vault-dir DIR` | Write the notes into the user's Obsidian vault (default `$L2A_VAULT_DIR`). |
| `--no-open-obsidian` | Do not open Obsidian at the end (the skill script and the MCP server pass it). |
| `--export-anki` | Also write `anki.csv`. |
| `--project` | Also design a project (`project.pdf`). |
| `--no-pdf` | Write the `.tex` files without building PDFs (no LaTeX installed). |
| `--no-audit` | Skip the planted-fault tests. Do not use it unless the user asks: they are how a run proves its judges work. |
| `--tts say` or `--tts openai` | A voice that works off Apple chips. |
| `series A/ B/ C/` | Several lectures as one course with shared terms (no audio). |

## Keys

`lecture2audio key set deepseek` (or `anthropic`, `openai`, ...) stores a key in
`~/.lecture2audio.env`, a file only its owner can read; a variable set in the shell wins. An
agent never handles the key itself.
