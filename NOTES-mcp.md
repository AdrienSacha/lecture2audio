# Notes: MCP server and Agent Skill

## Done
- `src/lecture2audio/mcp_server.py` and `lecture2audio mcp [--out DIR]`: the MCP server (official SDK `mcp>=2.3.0`, extra `[mcp]`, stdio, negotiates 2026-07-28). Tools `build`, `status`, `list_runs`, `read_result`, `stop_run`; resources `lecture2audio://runs` and `lecture2audio://runs/{run}/{file}`.
- `skills/lecture2audio/`: the Agent Skill (SKILL.md, scripts/run.py, references/, assets/).
- `tests/test_mcp.py` (18 tests); `docs/adr/0008-mcp-server-and-skill.md`; README section "Use from other agents"; ADR-0001 mention superseded; `uv.lock` refreshed.
- Full suite: 278 tests, OK (1 skipped) in `/tmp/l2a-mcp-venv`; the dry build passes.

## Left
- The MCP tasks extension, once the Python SDK implements it (`build` returns a handle and the agent polls `status` for now).
- `lecture2audio key set` and `calls.jsonl` come from uncommitted work in the main checkout (`keys.py`, `trace.py`); the server reads them when they exist and works without them.
- Not tried in a real host (Claude Code, Cursor) yet; checked with the SDK's own stdio client.
