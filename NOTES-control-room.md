# Control room: state of the work

## Done
- `trace.py`: every request is a `start` line and an `end` line with the same `id`, plus `progress`
  lines (about once a second: characters of reasoning and answer received, the last words) while a
  model streams. Each line carries its context: stage (from `stages.Tracker`), part (or gap, or
  slide), role and prompt, seat (writer or checker model), attempt, repair, try, audit,
  conversation and turn of a research loop, and `parent` (the call that caused it). `gate` lines
  record the code checks on an answer; a `run` line (with the pid) opens each session. The dry
  model is recorded too (host "dry"). Keys are redacted; a file the trace cannot write is ignored.
- Context is a ContextVar: `TextPipeline._map` and `slides.read_slides` copy it per item, so
  parallel parts never mix. `_ask_json` names the prompt and the seat; the retry loops of the script,
  the cards and the recitation set the attempt and the repair; the research loops link turns and
  their searches and pages.
- `events.jsonl` lines carry a time.
- `room.py` + endpoints in `app.py`: `/api/room/runs`, `/api/room/stream` (SSE: hello with the stage
  graph, stages, call summaries with byte offsets, the idea graph, whether a process is working on
  the run, read with `ps`, never signalled), `/api/room/detail` (full lines at given offsets, only
  line starts). Only run folders under the output folder; keys redacted again.
- The run screen gets a `snapshot` event on (re)connection: the current state at once, no replay.
- `assets/app.html`: the control room (Flow: stage graph, who talks to whom with calls in flight,
  timeline; Ideas: the idea ledger as a graph by part with status colours; an inspector with the
  full prompt, answer, reasoning, linked calls; an "in flight now" list with timers; counters; a
  process pill). Buttons: header "Control room" (shows "Live: ..." for a run started on the
  command line), "Watch the AI work" on the run screen, "What the AI did" on the done screen.
- `tests/room_fixture.py`: a made-up run (research loop, parallel parts, repair, rejected answer,
  model error), at once or live (`--live 6`).
- Tests: `tests/test_trace.py`, `RoomTest` in `tests/test_app.py`, trace tests in `test_llm.py`.

## Left
- Restart the scratch server and check the process pill (the server running during the last test
  still had the old app.py loaded).
- Test for the run screen snapshot with a job in progress.
- ADR-0005 and docs/research-notes.md entry; CLAUDE.md conventions (in the report only).
- Screenshots in docs/screenshots/.

## How to resume
- Env: `uv venv /tmp/l2a-trace-venv && VIRTUAL_ENV=/tmp/l2a-trace-venv uv pip install -e .`
- Tests: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /tmp/l2a-trace-venv/bin/python -m unittest discover -s tests`
- Demo: `python tests/room_fixture.py /tmp/room-out`, then
  `PYTHONPATH=src python -m lecture2audio app --port 8799 --out /tmp/room-out --no-open` and open
  `http://127.0.0.1:8799/#room=room-demo`.
