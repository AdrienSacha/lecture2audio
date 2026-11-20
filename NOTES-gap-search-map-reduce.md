# Gap search as a map-reduce (ADR-0006 action item 6)

## Done

- `pipeline.run_coverage` runs one request per range of 12 slides with text (`SLIDES_PER_CHUNK`) and one per part, in parallel through `self._map`. Every request sees the whole transcript; the TASK section narrows what it lists (`SLIDE_TASK`, `PART_TASK`). The transcript comes first so the requests share a prefix.
- Reduce in code: `_scope_chunk` (drops what a request was not asked for), `_merge_coverage` (fresh ids, dedupe by `_gap_key`: term gaps by kind and term, others by node, kind, term and detail), `_in_lecture_order` (renumbering of a fresh search only), then the existing gate.
- A slide range keeps the bounded repair of slides it left out (`_repair_slides`, per range). A request that keeps failing is named in `coverage.json` (`failed_chunks`) and in `report.md`, and only it is asked again on the next run, with the saved ids kept (the research cache is keyed by them). Every request failing: not cached, as before.
- `audit_coverage` runs the same requests on the blanked lecture, limited to the part of the blanked node and the slides with an idea taught there.
- Tests: `tests/test_gaps.py` (`SlideRepair`, `MapReduce`), `tests/test_course.py` (`CoverageAudit`). The full suite passed: 276 tests, 1 skipped.

## Left

- Run the dry build and check `report.md` (it had started when this note was written).
- Update ADR-0006 action item 6.

## Resume

`uv venv /tmp/l2a-gaps-venv && VIRTUAL_ENV=/tmp/l2a-gaps-venv uv pip install -e .`, then the tests and the dry build from CLAUDE.md with `/tmp/l2a-gaps-venv/bin/python`.
