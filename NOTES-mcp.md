# Notes: MCP server and Agent Skill (work in progress)

## Done
- `src/lecture2audio/mcp_server.py`: MCP server (official SDK `mcp>=2.3.0`, `MCPServer`, stdio, negotiates revision 2026-07-28). Tools: `build` (returns a run handle at once; `wait=true` blocks with progress), `status` (events.jsonl + calls.jsonl), `list_runs`, `read_result` (fixed file list, path traversal and symlinks refused, key values redacted), `stop_run`. Resources `lecture2audio://runs` and `lecture2audio://runs/{run}/{file}`.
- `lecture2audio mcp [--out DIR]` in `cli.main`; extra `[mcp]` in `pyproject.toml`.
- `tests/test_mcp.py`: 15 tests (tool functions, one dry build through `build`, in-process protocol tests skipped without `mcp`). All pass in `/tmp/l2a-mcp-venv`.
- `skills/lecture2audio/`: SKILL.md (six spec fields), `scripts/run.py`, `references/` (stages, outputs, options, mcp), `assets/` (example lecture, mcp.json).
- Checked by hand: the real stdio server lists the tools and runs a dry build.

## Left
- Tasks extension: SDK 2.3.0 ships no implementation of `io.modelcontextprotocol/tasks`; `build` returns a handle and the agent polls `status`.
- A test that the skill frontmatter has only the spec fields and that `references/stages.md` names every stage.
- ADR `docs/adr/0008-mcp-server-and-skill.md`, README section "Use from other agents", update of the ADR-0001 mention of `.claude/skills/lecture2audio/`.
- Run the full suite and the dry build; `uv lock` for the new extra.

## Resume
`uv venv /tmp/l2a-mcp-venv && VIRTUAL_ENV=/tmp/l2a-mcp-venv uv pip install -e '.[mcp]'`, then
`PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /tmp/l2a-mcp-venv/bin/python -m unittest discover -s tests`.
