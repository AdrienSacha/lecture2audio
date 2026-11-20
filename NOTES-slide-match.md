# Slide match: notes for resuming

## Done
- `sources.slide_match(slides, transcript)`: TF-IDF cosine (sublinear tf, IDF over transcript
  windows + slides) of each slide against 200-word transcript windows (half overlapping);
  English and French stopwords, plural s cut, pure numbers and hex constants dropped,
  letter-digit identifiers kept (x0, rv32i). A slide matches at cosine >= 0.12
  (`MATCH_SLIDE`); the deck is "low" when under 50 % of scored slides match (`MATCH_DECK`).
  Each slide gets its best window (`start`/`end` share, `words` range) and a title.
- Calibration (share of slides matching): OSCA l2 transcript vs ISA.pdf 0.04 (2/52);
  own-lecture MIT OCW pairs 0.96 (python lec2), 0.67-0.71 (stats lec4 vs MLE slides);
  other lectures 0.00-0.21 (worst: python lec10 slides vs lec2 transcript 0.21);
  stats lec4 vs intro slides (lectures 1-2) 0.36-0.40, flagged low (desired: only the
  matching slides kept).
- `cli.run`: after the transcript is read, before any model call; writes
  `out/<name>/slide_match.json`; logs `[slides] the slides seem to belong to another
  lecture: ...` (`report.MISMATCH_TAG`); blanks unmatched slides (numbers kept) unless
  `--slides-all`; never stops the run. Skipped when the slides are the lecture.
- `report.md`: bold warning right after the first sentence (the app reads the first
  sentence as the script state, so it stays first); section "How the slides match the
  transcript" lists unmatched slides.
- Tests: `tests/test_slide_match.py` (matched, mismatched, partial, French, blanks, report,
  two CLI end-to-end runs). Full suite passed before the new file (260 OK); new file 10 OK.

## Left
- Rerun the full suite + dry build, then a final commit (not WIP).
- Optional: pass each slide's likely place to the outline / gap search prompts
  (pipeline.py, owned by other agents; data is in slide_match.json).
- Optional: research-notes.md entry with the calibration; app hook in `app.summary()`:
  a log line starting with `report.MISMATCH_TAG` -> warn item (app.py/app.html not edited).

## Resume
`uv venv /tmp/l2a-match-venv && VIRTUAL_ENV=/tmp/l2a-match-venv uv pip install -e .`, then
`PYTHONDONTWRITEBYTECODE=1 L2A_PDF=0 PYTHONPATH=src /tmp/l2a-match-venv/bin/python -m unittest discover -s tests`.
