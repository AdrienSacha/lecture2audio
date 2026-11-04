# ADR-0001: A fixed chain of model calls, checked by code and by an idea ledger, with Obsidian as the only interface

**Status:** Accepted
**Date:** 4 November 2026
**Deciders:** The project owner (decisions of November 2026); the evidence is in `docs/research-notes.md` and `docs/learning-science.md`.

## Context

lecture2audio turns a lecture (up to about six hours, in English or French) into three products: a spoken explanation with chapters, an Obsidian vault of course notes, and review material. The governing requirement is completeness: everything the teacher taught must reach the products, and only noise may be removed. A summary is a failure.

Four forces shape the design:

1. **Language models omit content silently.** A model asked to rewrite or check a long text drops ideas without saying so, and a judge asked many criteria at once misses omissions (research notes, sections 2.12 to 2.14). Completeness cannot rest on a model's word.
2. **Output limits.** A single call that lists every idea of a six-hour lecture can be cut short by the output limit, and nothing downstream would know that an idea was never listed.
3. **Cost and reruns.** A full run makes several hundred model calls. A failure in one part must not force the other parts to be redone.
4. **One user, one reading tool.** The owner reads, browses and reviews in Obsidian. A separate browser interface duplicated that work and was a second surface to maintain.

## Decision

1. **A fixed chain of stages**, each a model call followed by code gates: outline (whole lecture), gap search (whole lecture), idea listing (per part), termbase, research, rewrite and verify (per part), spoken form, course cards and their judge (per part), voice. Parts run in parallel after the outline. Code, not a model, computes every verdict.
2. **An idea ledger** (`out/<name>/ideas.csv`) as the thread through the chain. `run_ideas` lists the atomic ideas in one call per part, with ids scoped to the part (`T3.12`) and the units that say each idea. The verifier must check off every idea of its part against the script, and the course judge must check off every idea against the cards and name the card that teaches it. Code refuses a set of cards that leaves an idea unclaimed before any judge reads them. The CSV is a view that is rebuilt from the cached stage results on every run, not a separate source of truth. `report.md` lists every idea that did not reach the end and every part with no idea.
3. **Obsidian as the only interface.** The program is a command line that takes one folder and writes the vault: one note per part with its own recording, collapsed answers and prerequisite reminders, one note per term, an index with the full recording, and a note that transcludes every part.

## Options considered

### Option A: Fixed chain with code gates and an idea ledger (chosen)

| Dimension | Assessment |
|-----------|------------|
| Complexity | Medium: many stages, but each is small and has one gate |
| Cost | About 10 to 20 calls per part in the worst case (three attempts each for rewrite and cards); a six-hour lecture of about 24 parts needs 250 to 450 calls |
| Scalability | Linear in the number of parts; parts run in parallel; every stage is cached |
| Team familiarity | High: plain Python, JSON caches, one prompt file per stage |

**Pros:** completeness becomes a list that code counts; every failure is local to one part and one stage; reruns reuse every finished result; the verifier itself is audited by a planted omission.
**Cons:** the ledger can only prove that what was listed arrived, not that the list was complete; many prompts to keep in step with their gates and with the dry handler.

### Option B: Agents that plan, write and review in conversation

| Dimension | Assessment |
|-----------|------------|
| Complexity | High: emergent control flow, hard to reproduce |
| Cost | Higher at equal quality |
| Scalability | Poor: the conversation grows with the lecture |
| Team familiarity | Low |

**Pros:** flexible on unusual lectures.
**Cons:** 2026 studies found that a single strong model matches or beats multi-agent setups at equal token budget (arXiv 2601.12307, 2604.02460, 2606.05670), and this task is sequential. Verdicts would come from models talking to models, which is the failure the chain exists to avoid.

### Option C: One long-context call per lecture

| Dimension | Assessment |
|-----------|------------|
| Complexity | Low |
| Cost | Lowest |
| Scalability | Fails on long lectures: output limits, and recall that falls with input length (section 2.13) |
| Team familiarity | High |

**Pros:** simple.
**Cons:** no place to check anything; one omission anywhere and the whole output must be trusted or redone.

### Sub-decision: where the ideas are listed

- **Inside the whole-lecture gap search** (the first version of the ledger): one call, but the list is cut short on a long lecture and the gap search's output grows with it.
- **One call per part** (chosen): the list scales with the lecture, parts run in parallel, a failing part is retried alone, and a code gate checks that every node with units has an idea. The gap search stays whole-lecture, because a gap such as a term used in part 2 and defined in part 5 needs the whole lecture; it no longer lists the ideas.

### Sub-decision: what the ledger is

- **A database that stages write to** (SQLite or an appended CSV): auditable, but it can drift from the caches when a stage is redone.
- **A view rebuilt from the stage caches** (chosen): it always matches them; editing it by hand has no effect, which the documentation says.

## Trade-off analysis

The chain trades call count for checkability. Every additional judge costs calls, but each one answers a single, countable question (one status per node, one status per idea, one status per card), and code combines the answers. That is what lets the program say, for each idea of the lecture, where it was taught and who confirmed it.

Listing ideas per part trades a whole-lecture view for completeness. An idea that spans two parts is listed in each; the ledger counts it as reached when any part teaches it. Ideas are not deduplicated across parts; a repeated idea is checked twice, which costs little.

Obsidian trades a guided interface for a tool the owner already uses daily. The cost is that options are reached only through the command line, which the single-folder input and the skill file (`.claude/skills/lecture2audio/SKILL.md`) reduce.

## Consequences

- Easier: proving which card teaches which idea; finding the transcript units behind any line of the notes; rerunning one part; exporting review cards (`--export-anki`) tagged by idea.
- Harder: every prompt change must change its gate, `dry_run_handler` and the tests together (`CLAUDE.md`); prompts are routed by a phrase, which a careless edit can break.
- To revisit:
  - **The extractor is not audited.** The ledger proves only that listed ideas arrived. An idea that `run_ideas` never lists is caught only by the verifier's node-by-node check against the units. The verifier has a planted-omission test; the extractor and the course judge have none.
  - **"Verified" is a model's claim confirmed by a second model.** The card claims the idea and the judge agrees. No code yet checks that a verified idea's numbers and terms appear in the text of its card.
  - **The gap search reads the whole lecture.** Its output is now small, but its input on a six-hour lecture is long, and recall falls with length.
  - **No real model has run the chain.** Every check so far used the dry model or test handlers.

## Action items

1. [x] List the ideas per part (`run_ideas`, `prompts/ideas.md`, `validate_idea_list`), with ids scoped to the part.
2. [ ] Run one real lecture per provider (DeepSeek, Anthropic) and read `report.md`, `ideas.csv` and the vault; tune the prompts on what they show.
3. [ ] Add a planted-idea test for the extractor: add one unit with a distinctive fact to a copy of a part, and check that `run_ideas` lists it; report the result as the verifier test is reported.
4. [ ] Add a planted-omission test for the course judge: remove a claimed idea's sentences from its card, and check that the judge calls the idea missing.
5. [ ] Add a code check for verified ideas: the numbers of the idea's statement must appear in the text of the card that claims it (reusing `unsupported_numbers`).
6. [ ] Measure, on a long real lecture, whether the gap search misses gaps in late parts; if so, split it into a per-part search plus a whole-lecture pass over the termbase only.
