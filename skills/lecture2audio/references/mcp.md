# The MCP server

`lecture2audio mcp [--out DIR]` serves lecture2audio over stdio to any MCP host. It needs the
extra `[mcp]` (`uv pip install -e '.[mcp]'`, official MCP Python SDK 2.3 or later, protocol
revision 2026-07-28). `--out` defaults to `$L2A_OUT_DIR`, else `./out` in the folder the host
starts the server in, so give an absolute path.

## Register it

Claude Code (user scope, available in every project):

```bash
claude mcp add --scope user lecture2audio -- /path/to/lecture2audio/.venv/bin/lecture2audio mcp --out /path/to/out
```

Cursor (`~/.cursor/mcp.json`, or `.cursor/mcp.json` in a project), Claude Desktop
(`claude_desktop_config.json`) and most other hosts take the same block; a template is in
`assets/mcp.json`:

```json
{"mcpServers": {"lecture2audio": {"command": "/path/to/.venv/bin/lecture2audio",
                                  "args": ["mcp", "--out", "/path/to/out"]}}}
```

Do not put API keys in this block. The server passes its environment to the runs it starts, and
the command line reads `~/.lecture2audio.env` itself.

## Tools

| Tool | Arguments | Returns |
|---|---|---|
| `build` | `path`; optional `llm`, `language`, `audio`, `research`, `name`, `redo`, `tts`, `wait` | At once: `run`, `folder`, `pid`, the command line. With `wait=true`: when the run ends, its status and the paths of its results, with progress per stage. |
| `status` | `run` | `state` (`running`, `finished`, `stopped`, `incomplete`), `current_stage`, `stages` with seconds (from `events.jsonl`), `cached_stages`, `calls` (requests by kind, errors, seconds, tokens, from `calls.jsonl`), `results`, `log_tail`. |
| `list_runs` | none | Every run in the output folder, newest first. |
| `read_result` | `run`, `file` | The text of `script.md`, `report.md`, `glossary.md`, `questions.md`, `ideas.csv` or `termbase.json`. Any other name, or a path, is refused. |
| `stop_run` | `run` | Interrupts a run started by an MCP server; its finished stages stay cached. |

Resources: `lecture2audio://runs` (the run list, JSON) and
`lecture2audio://runs/{run}/{file}` for the same six files.

## Behaviour

- `build` starts the command line as a subprocess in its own session: the run survives the host
  closing, and its state is read from disk. Its log is `out/<run>/mcp-run.log`.
- A second `build` on a run that is still going is refused.
- Runs started from a shell are listed and read like the others, but `status` cannot tell
  whether their process is alive: an open stage is reported as `stopped`.
