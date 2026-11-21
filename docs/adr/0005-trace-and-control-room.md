# ADR-0005: A trace with context, and a control room that reads it

**Status:** Accepted
**Date:** 21 November 2026
**Deciders:** The project owner, who asked to see, live and afterwards, everything the models did.

## Context

The owner wanted "intelligible AI": to watch a run as it goes, see which stage, part and role each model call serves, what was sent and answered (reasoning included), which searches and pages the researcher used, and how much it cost in tokens and time. `calls.jsonl` already recorded every network request in full, but one line per finished call, with no context: a line could not say which part or attempt it belonged to, and a call in flight was invisible until it ended, so a call of several minutes looked like a stalled run. The app followed a run only through its log, and a page that was reloaded replayed every event from the start.

## Decision

1. **The trace carries context, set by code.** A `contextvars.ContextVar` in `trace.py` holds the stage (set by `stages.Tracker`), the part (or gap, slide or slide range) a worker serves, the prompt and its role, the seat (writing or checking model), the attempt and the repair of the retry loops, the try of a code gate, the audit, and the conversation and turn of a research tool loop. `TextPipeline._map`, `slides.read_slides` and the audits that run beside the main path give each thread a copy of the caller's context. The role is named by the caller (`_ask_json` knows its prompt); the phrase of the prompt is only a fallback.
2. **Each request has an id and two lines**, `start` (what was sent) and `end` (the answer or the error, tokens, seconds), plus a `progress` line about once a second while a model streams (characters of reasoning and answer, the last words). `parent` links a search or page to the model turn that asked for it, and a turn to the previous one. Code gates write a `gate` line linked to the answer they judged. Each session starts with a `run` line holding the pid.
3. **The trace only observes.** It never changes control flow (ADR-0003); a trace file that cannot be written is ignored. Keys are redacted when written and again when served.
4. **The control room is a view of the app** (`assets/app.html`, hand-written SVG and canvas, no external code), fed by `/api/room/stream` (Server-Sent Events) from the run's own files: the stage graph from `stages.STAGES`, `events.jsonl`, summaries of `calls.jsonl` (`trace.summary`, with byte offsets; the full line is read on demand from `/api/room/detail`), and the idea graph from `ideas.csv`, the script and the cards (`room.idea_map`). It works for runs started on the command line, for past runs, and for runs without `calls.jsonl` (stages only).
5. **A page shows the present state at once.** The run screen gets one `snapshot` event on (re)connection instead of a replay; the room parses every record before it paints and animates only what happens after it opened. Whether a process still works on a run is read from `ps` (the pid of the `run` line, or a lecture2audio command naming the run), never by sending a signal; a run whose process is gone is drawn as stopped at its last write.

## Consequences

- `calls.jsonl` grows by the progress lines (one per second per call in flight) and by the start lines, which hold the request once; the end line holds the answer once.
- The server keeps the summaries of each file it has streamed (`room.Summaries`), so a reload is instant; the first open of a large trace (about 90 MB) takes a few seconds. Progress lines are kept in that cache, which grows by about 30 MB on a ten-hour run.
- Lines written by the first trace format (one line per call, no id or context) are still read, as calls of the whole lecture.
- Open risks: `ps` costs up to a second on macOS (shared for four seconds); a process detected by its command line can be wrong when two runs share a name; the detection is advisory only.
