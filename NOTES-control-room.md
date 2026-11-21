# Control room: state of the work

Design: `docs/adr/0005-trace-and-control-room.md`. Screenshots: `docs/screenshots/`.

## Done
- `trace.py`: start, progress (about once a second while streaming) and end lines per request,
  with id, parent and context (stage, part, role, prompt, seat, attempt, repair, try, audit,
  conversation, turn); `gate` lines for code checks; a `run` line with the pid per session. The
  dry model is recorded too (host "dry"). Keys redacted; an unwritable file is ignored.
- Context propagation: `TextPipeline._map`, `slides.read_slides`, and the audits run beside the
  main path (`cli.beside`) copy the context per item; the research loops link turns and tools.
- `events.jsonl` lines carry a time.
- `room.py` and `/api/room/runs`, `/api/room/stream`, `/api/room/detail` in `app.py`: run
  folders only, Host/Origin checks, keys redacted again, summaries cached per file so a reload is
  instant, process state from `ps` (read only), the stage of an untimed last line dated by the
  file's modification time.
- The run screen gets one `snapshot` event on (re)connection, not a replay.
- The control room in `assets/app.html`: stage graph, who talks to whom with calls in flight,
  timeline, an "in flight now" list with timers and streamed characters, a process pill, the idea
  graph, an inspector with the full prompt, answer, reasoning and linked calls.
- `tests/room_fixture.py` writes a made-up run, at once or live (`--live 6`).

## Left
- First open of a very large trace (about 90 MB) takes about 6 s (JSON parsing); reloads are
  instant.
- The progress lines stay in the server's summary cache (about 30 MB on a ten-hour run).

## How to resume
- Env: `uv venv /tmp/l2a-trace-venv && VIRTUAL_ENV=/tmp/l2a-trace-venv uv pip install -e .`
- Tests: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /tmp/l2a-trace-venv/bin/python -m unittest discover -s tests`
- Demo: `python tests/room_fixture.py /tmp/room-out`, then
  `PYTHONPATH=src python -m lecture2audio app --port 8799 --out /tmp/room-out --no-open` and open
  `http://127.0.0.1:8799/#room=room-demo`.
