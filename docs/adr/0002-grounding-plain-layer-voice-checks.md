# ADR-0002: Claim-anchored script, abstaining idea list, plain-language layer, recitation material, and checks from independent models

**Status:** Accepted
**Date:** 4 November 2026
**Deciders:** The project owner. Evidence: section 2.17 of `docs/research-notes.md`.

## Context

ADR-0001 made completeness countable: a ledger of atomic ideas, checked off by the verifier and the course judge. Its open risks were that the checks were model claims confirmed by other models, that the idea list could pad or miss, and that nothing independent of the writer and judge families looked at the result. The owner then asked for the best material possible: a course in the style of an MIT recitation (labelled blocks, proofs, exercises with solutions) with, for every idea, a version a reader new to the field understands at once, and a voice that does not misread.

## Decision

1. **Claim-anchored script.** Every "lecture" sentence of the script lists the ideas it states (`ideas`). Code then enforces two invariants before any judge: every idea is cited by a sentence (`idea_uncited`, blocking), and a sentence that cites none is listed as possibly stating something the ledger missed (`unledgered`, a warning). Provenance becomes a property of the data, as in claim-anchored summarization (Guan, 2026), and the verifier's semantic check becomes a second line.
2. **An idea list that may abstain.** The idea listing must account for every unit: in an idea, or in `no_content` (teaches nothing, or garbled). There is no minimum count, and feedback says fewer is fine, because a minimum makes models pad their lists with real but irrelevant text (Barali and Paudel, 2026). Unaccounted units are asked again; the best attempt is kept and the rest is reported.
3. **Shorter parts** (1,500 words by default): faithfulness falls towards the end of long outputs, and splitting removes the effect (Yang et al., 2025).
4. **"In short" on every card**: one to three sentences of everyday words, the same facts, no analogy, checked by code (numbers, analogy, length as a warning) and by the judge (faithfulness). Readability formulas are not used as targets because they do not predict comprehension, while faithfulness, background and coherence do (Guo et al., 2025).
5. **Recitation material**: proofs when the lecture gives them, and up to four exercises per part with complete worked solutions, solved again by the judge; near-duplicate questions are flagged by code.
6. **A simulated first reader, then one revision**: the cold read now also reports confusing sentences and missing context, and its loose ends go back to the writer once; the revision is kept only if it has no more blocking problems (Jung et al., 2026).
7. **An independent fact-checker**: MiniCheck (Tang et al., 2024), local, of another model family, checks each lecture sentence against the transcript units of the ideas it cites. Its findings are reported, never acted on.
8. **Voice**: Qwen3-TTS for French (Ming reads English only); every chunk is transcribed back and voiced again when heard wrong, keeping the best take (Wang et al., 2026). **Speech recognition**: decoding without the previous text, skipping unsure silence, and a code filter for known hallucination phrases and loops, every removal reported (Zolkepli, 2026); Parakeet offered as an alternative.

## Options considered

### For grounding: post-hoc attribution by a judge versus citation by construction

Post-hoc attribution asks a model where a sentence came from, after the fact; 11% of sentences then get no citation at all (Guan, 2026). Citation by construction costs one field per sentence and lets code prove coverage. Chosen: by construction, citing ideas rather than units, because sentence-level citation of raw positions degrades citation quality (Wang et al., 2026, cited by Guan).

### For the idea list: a required minimum per node versus abstention

A required minimum (every node with units has an idea) is the gate of the first version. It pressures the model to pad. Abstention with unit accounting asks for the same completeness without that pressure. Chosen: abstention.

### For independence: a second LLM judge (jury) versus a small fact-checker of another family

A panel of judges built on one model mostly recalibrates its threshold and shares its errors (Ji, 2026). A second hosted judge doubles the verifier cost. A local fact-checker of another family is cheap and independent. Chosen: the fact-checker, reported only; a cross-family verifier remains available with `--verify-llm`.

### For the voice: check everything by listening versus trusting the model

| Option | Cost | Effect |
|---|---|---|
| No check | none | skips and invented words reach the audio |
| Round trip, report only (before) | one recognition per chunk | problems listed, not fixed |
| Round trip with new takes (chosen) | one recognition per chunk, plus new takes for failures | most failures replaced by a better take; the rest listed |

## Consequences

- Easier: proving coverage idea by idea without a model; finding where each script sentence comes from; trusting that a reported "verified" has a citation, a judge's agreement and no lost number.
- Harder: the rewrite prompt asks for one more field; cached results from before this change lack it and are treated as citing nothing (warnings only) until `--redo rewrite`.
- Costs: one revision call per part with loose ends; one recognition per voiced chunk; MiniCheck's model download (about 3 GB) when installed.
- To revisit:
  - Calibrating the judges with a few labelled lectures, as conformal risk control does for omissions (Bedi et al., 2026), once real lectures have been labelled.
  - A terminology repair of the transcript with the whole lecture as context (Zhu et al., 2026), for recordings with many technical terms.
  - Parakeet as the default recognizer, after a comparison on the owner's own recordings.

## Action items

1. [x] Claim-anchored script with code invariants.
2. [x] Abstaining idea list with unit accounting.
3. [x] "In short", proofs, exercises, near-duplicate check.
4. [x] Cold-read revision round.
5. [x] MiniCheck fact-check, reported.
6. [x] Qwen3-TTS for French; round trip with new takes; ASR guards; Parakeet option.
7. [ ] Run one real lecture and read `report.md`, `ideas.csv` and the vault.
8. [ ] Terminology repair of transcripts (Agentic-GER).
9. [ ] Judge calibration on labelled lectures.
