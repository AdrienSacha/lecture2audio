# ADR-0008: lecture2audio as an MCP server and an Agent Skill

**Status:** Accepted
**Date:** 21 November 2026
**Deciders:** The project owner. Carries out item 8 of the decision in ADR-0006 (evidence: `docs/research/2026-11-speed-and-agents.md`, section 5 and 6).

## Context

Other agents (Claude Code, Cursor, Claude Desktop, Codex, Gemini CLI) should be able to run a lecture and read its results. Two standard forms exist: an Agent Skill (a directory per the [agentskills.io specification](https://agentskills.io/specification)) that teaches an agent with a shell to drive the command line, and an MCP server (revision [2026-07-28](https://modelcontextprotocol.io/specification/latest)) for any MCP host. A run takes tens of minutes and spends API credit. The earlier skill file `.claude/skills/lecture2audio/SKILL.md`, cited in ADR-0001, was a single local, untracked file.

## Decision

1. **MCP server** `src/lecture2audio/mcp_server.py`, started with `lecture2audio mcp [--out DIR]`, stdio transport, built on the official MCP Python SDK (`mcp>=2.3.0`, extra `[mcp]`, imported only when the server starts). It negotiates revision 2026-07-28.
   - Tools: `build` (starts the command line as a subprocess, in its own session, and returns a run handle at once; `wait=true` blocks and reports progress per stage), `status` (stages from `events.jsonl`, request counts from `calls.jsonl`, cached stages, last log lines), `list_runs`, `read_result`, `stop_run`. Resources: `lecture2audio://runs` and `lecture2audio://runs/{run}/{file}`.
   - Like the app, the server never runs the pipeline in its own process, so an agent can do nothing the command line cannot, and every gate, judge and audit runs unchanged.
   - Safety: run names match a fixed pattern and must resolve to a direct child of the output folder; only six named files can be read (`script.md`, `report.md`, `glossary.md`, `questions.md`, `ideas.csv`, `termbase.json`), and symbolic links out of the run folder are refused. Options are checked against their allowed values before a command is built (no shell). API keys are never arguments or results: the subprocess inherits the environment and the command line loads the key file itself; any key value in a returned text becomes `[key]`.
2. **Long runs without the tasks extension.** The [tasks extension](https://modelcontextprotocol.io/extensions/tasks/overview) is the standard handle for long operations, but SDK 2.3.0 implements no part of it and host support varies. `build` therefore returns a run name, and `status` reads the run's state from disk, which also survives the host or the server restarting. When the SDK ships the extension, `build` should return a `CreateTaskResult` to clients that declare it, keeping the current behaviour for the others.
3. **Agent Skill** `skills/lecture2audio/` (tracked, supersedes `.claude/skills/lecture2audio/`): `SKILL.md` with the six specification fields only, `scripts/run.py` (runs the command line, sends its log to a file and prints a summary of `events.jsonl`, `calls.jsonl`, `ideas.csv` and the headings of `report.md`), `references/` (stages, outputs, options, MCP registration) and `assets/` (a tiny lecture, an MCP configuration template). The skill is written by hand and kept focused: agent-written and exhaustive skills lowered pass rates in SkillsBench ([arXiv 2602.12670](https://arxiv.org/abs/2602.12670)).
4. **No sampling** (deprecated in 2026-07-28) and no model-selected skills inside the harness (ADR-0006).

## Consequences

- Any MCP host or shell agent can run a lecture, follow it and read its report, with the same results as the command line.
- Tests (`tests/test_mcp.py`) cover the tool functions without the SDK, a dry run through `build`, the protocol in process when `mcp` is installed, and the skill: its frontmatter, and its references against `stages.STAGES`, `READABLE` and `ledger.STATUSES`, so they cannot drift.
- `status` cannot tell whether a run started from a shell is still alive; an open stage of such a run shows as `stopped`.
- To revisit: the tasks extension once the SDK supports it; a `--search mcp:<server>` option to consume MCP for research (ADR-0006, optional).
