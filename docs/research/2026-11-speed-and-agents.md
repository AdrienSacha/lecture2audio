# Speed, scale and agent technologies for the lecture2audio harness (November 2026)

**Question.** A run on a 15,642-word transcript had spent seven minutes and was still listing ideas. The owner asked why it is so slow, wants much bigger runs with many more model calls finished much sooner, and asked how agent skills, the Model Context Protocol (MCP), subagents and agent SDKs fit with this product.

**Short answer.** The slowness comes from how the harness schedules its calls, not from the provider. DeepSeek allows 2,500 concurrent requests to `deepseek-flash` and 500 to `deepseek-v4-pro` per account, and the harness uses three. It also waits for every part to finish each stage before any part starts the next one. It runs reasoning at `high` effort on every call, including mechanical ones. It regenerates whole answers when a single detail is wrong, and it keeps reported-only audits on the critical path. Fixing these keeps every design decision of ADR-0001 to ADR-0003, and should cut the model-bound time of a 20-part lecture by a factor of about five to eight. The time saved can buy more checks. Skills and MCP are ways to package and connect a harness. They do not make the harness faster. lecture2audio should expose itself as both a skill and an MCP server, and it can consume MCP for search.

Date of research: 20 November 2026. Sources are cited inline. Where the evidence is from 2025 it is marked as background because nothing newer was found. Claims about this repository refer to the code at commit `98c8ed8`.

---

## 1. Terms used in this document

- **Harness.** The program around a language model that builds each request, runs the tools the model asks for, checks the answers, keeps state and decides what happens next. A 2026 source study of eleven production harnesses defines an agent as "a model plus a harness" ([Barbaste et al., 2026, arXiv 2609.00006](https://arxiv.org/abs/2609.00006)). lecture2audio is a harness in this sense. Its loop is fixed by code (`stages.py`, `cli.run`), its tools are web search, page fetching, LaTeX, speech synthesis and recognition, its checks are the code gates and judges, and its state is the cache under `out/<name>/`.
- **Critical path.** The longest chain of calls that must happen one after another. The wall-clock time of a run cannot be shorter than it, however many calls run in parallel elsewhere.
- **Stage barrier.** A point where every part must finish a stage before any part may begin the next one.
- **Concurrency.** The number of model requests open at the same time. **Workers** (`--workers`) is the harness's own cap on it.
- **Prefix cache.** The provider stores the internal state computed for the beginning of a prompt. A later request that begins with exactly the same text skips that work and is billed less.
- **Reasoning tokens.** Tokens the model writes while thinking, before the answer. They take generation time like any other output token.

---

## 2. Where the time goes: a time model of one run

### 2.1 How calls are scheduled today (from the code)

`cli.run` calls the stages one after another. Inside a stage, `TextPipeline._map` runs the parts on a thread pool of `--workers` threads (default 3, `cli.py` line 154) and returns only when every part is done. The order of calls in `cli.run` is:

1. `run_outline`: one whole-lecture call.
2. `run_coverage` (the gap search): one whole-lecture call. Its answer must list an idea for every slide (`enrich.validate_coverage`), and `_ask_json` re-sends the whole request up to three times when a gate fails.
3. `run_ideas`: one call per part, three at a time, with up to `max_retries + 1` re-asks per part for unaccounted units.
4. `audit_ideas`: one more call, serial.
5. `run_termbase`: one whole-lecture call (up to three tries).
6. `run_research`: one research loop per gap, three at a time. Each loop is up to `--research-depth` (30) model turns, and each turn is a thinking call at `high` effort (`llm.research_loop`).
7. `audit_coverage`: the whole gap search again on a lecture with one unit blanked, serial, up to three tries.
8. `run_parts`: per part, up to three attempts. Each attempt makes three model calls in sequence: rewrite (`deepseek-flash`), verify (`deepseek-v4-pro`) and the terminology check (`deepseek-v4-pro`).
9. `run_factcheck` (local), then `audit_verifier`: one verifier call, serial.
10. `run_spoken`: one call per part.
11. `CoursePipeline.run`: per part, up to three attempts of write plus judge. Then the cold read (one call per part), the revision (write plus judge for each part with loose ends), a second cold read of the revised parts, and `audit_judge` (one call, serial). Each of these is its own barrier.
12. `RecitationPipeline.run`: per part, up to three writes, each followed by TikZ compilation.
13. `run_project` (optional, one call), then the LaTeX documents.
14. Audio: local speech synthesis of every chunk (Ming or Qwen3-TTS through mlx-audio), then the voice round trip, which transcribes every chunk again with Whisper and re-voices failures. It starts only after every other stage has finished.

Both models run with `thinking: enabled` and `reasoning_effort: high` (`llm.HostedLLM`), and every request may use 16,000 answer tokens plus 32,000 tokens of reasoning room.

### 2.2 What one call costs in time

Generation time dominates. A call takes roughly the time to the first token, plus (reasoning tokens + answer tokens) divided by the decode speed of one stream. DeepSeek does not publish an output speed ([DeepSeek pricing page](https://api-docs.deepseek.com/quick_start/pricing), checked: speed not stated). The measured run gives an estimate:

- The outline (one call) took 46 s.
- The gap search took 320 s for up to three full attempts, so about 100 s per attempt.
- The idea listing covered about 8 parts in about 3 minutes at 3 workers, so about 65 s per call.
- The stream was measured at about 10 KB/s. If that figure counts the raw server-sent-event stream, each streamed token carries an envelope of 100 to 200 bytes, so it corresponds to roughly 50 to 100 tokens per second. This is an inference, not a measurement.

A typical call of this harness therefore costs **c ≈ 60 to 120 s**. Most of it is spent generating reasoning and answer tokens, not reading the prompt. This has a consequence that matters below: caching the prompt reduces cost and time to first token, but it cannot remove most of the latency. Only fewer calls in sequence, shorter outputs or less reasoning can.

### 2.3 Calls on the critical path, today

Take P = 20 parts and W = 3 workers. Each per-part stage runs in ⌈20/3⌉ = **7 waves**, and each wave lasts as long as its slowest part. A part's chain inside each stage is:

| Stage | Calls in sequence per part (typical, worst) | Waves today | Calls on the critical path today |
|---|---|---|---|
| ideas | 1 (3) | 7 | 7 to 21 |
| rewrite + verify + terms | 3 per attempt, 2 attempts typical (3) | 7 | 42 to 63 |
| spoken | 1 | 7 | 7 |
| course write + judge | 2 per attempt, 2 typical (3) | 7 | 28 to 42 |
| cold read | 1 | 7 | 7 |
| revision (write + judge) and second cold read | 3 | 7 | 21 |
| recitation | 1 to 3 | 7 | 7 to 21 |
| **Total, per-part stages** | **about 18 typical** | | **about 120 to 180** |

The whole-lecture calls add about 5 to 11 more in sequence: the outline, the gap search (1 to 3), the termbase (1 to 3), and the four audits (`audit_ideas`, `audit_coverage` with 1 to 3 tries, `audit_verifier`, `audit_judge`), all of which sit on the critical path although their results are only reported.

Research adds G gaps ÷ 3 waves of a loop of up to 30 thinking turns. With 30 gaps and 10 turns of about 30 s each, that is 10 waves of 5 minutes, or **about 50 minutes**. In the measured run the gap search failed, so research did not run at all.

At c ≈ 75 s, the model-bound critical path is **about 125 to 190 call latencies, or 2.5 to 4 hours**, before research and before audio. The run seen by the owner was at minute 7 of this.

### 2.4 The mechanisms, ranked

1. **Stage barriers combined with three workers (largest).** The 7 waves are repeated in every per-part stage, about 8 times. With all 20 parts in flight at once, each stage costs one wave. Removing the barriers as well lets a part go from ideas to recitation without waiting for the others, so the critical path becomes the chain of the slowest single part, about 18 calls.
2. **The worker cap is three orders of magnitude below what the provider allows.** DeepSeek limits concurrency per account to **2,500 concurrent requests for `deepseek-flash` and 500 for `deepseek-v4-pro`**, with no requests-per-minute or tokens-per-minute figure. Exceeding the limit returns HTTP 429, and higher limits can be requested at no extra cost ([DeepSeek rate limit](https://api-docs.deepseek.com/quick_start/rate_limit)). The harness opens 3.
3. **Whole-answer retries of whole-lecture calls.** The gap search must give every slide an idea. In the measured run all three attempts left out the same slides (20, 37, 62, 75 and 91). Each retry regenerated about 100 s of output to fix five entries. Because the failure was systematic, the retries could not succeed. The stage then fell back to "no gaps", which also skipped the research and the gap-search audit (`audit_coverage` returns early when `coverage` holds an error). The harness already repairs a part's script by section (`repair_plan`, `splice_repair`). The gap search lacks the same bounded repair.
4. **Large outputs from single calls.** The gap search asks for every atomic idea of every slide (about 95 slides) in one answer, on top of the gaps. Output length is what sets the call's duration (§2.2). The same work split by slide range would run in parallel and give each call a short answer.
5. **Reasoning at `high` on every call.** DeepSeek's thinking mode defaults to `high`, and accepts `low`, `high` and `max` ([DeepSeek thinking mode](https://api-docs.deepseek.com/guides/thinking_mode)). The spoken form (a near-verbatim conversion), the terminology check (one status per listed occurrence) and the cold read are mechanical, but they pay the same reasoning cost as the rewrite. Tran and Kiela (April 2026) found accuracy on multi-hop reasoning flattening after about 1,000 to 2,000 thinking tokens, and some models overthinking beyond that ([arXiv 2604.02460](https://arxiv.org/abs/2604.02460), §5.1). That study did not measure this task, so the gain must be checked with the planted audits before it is trusted.
6. **Audits on the critical path.** Four audits are reported, never acted on, yet each blocks the run while it executes. `audit_coverage` alone is a full second gap search (one to three whole-lecture calls).
7. **Audio is last and serial.** The voice needs only the spoken form of a part (`stages.py`: `audio` needs `spoken`), yet it starts after the course, the recitation and the documents. Its duration on Apple Silicon was not measured in this run and may be the single largest item; ADR-0003 already noted that "the course and the voice" could run at the same time.
8. **Prompt structure that defeats the prefix cache (minor for speed, larger for cost).** See §3.3.

What is **not** the bottleneck: the provider's rate limits (above), and prompt reading, which at a few thousand to a few tens of thousands of tokens is a small share of a 60 to 120 s call.

---

## 3. What makes it faster and bigger without losing quality

### 3.1 Raise concurrency, with one limiter per provider

The default of 3 has no basis in the provider's limits. The right shape is a **single concurrency limiter per model and account**, shared by every stage, rather than a thread count per stage. Otherwise per-part pipelining (§3.2) would multiply threads. The limiter should back off on HTTP 429 and honour `retry-after`. The SDKs already retry 429 with backoff (`MAX_RETRIES = 4`, `llm.py`).

Recommended defaults, with the evidence for each:

| Provider | Published limit | Recommended default cap | Why |
|---|---|---|---|
| DeepSeek `deepseek-flash` | 2,500 concurrent per account ([source](https://api-docs.deepseek.com/quick_start/rate_limit)) | 64 | Enough for every part of a six-hour lecture (about 24 parts) plus every research loop at once, far below the limit, and leaves room for other programs on the same account (the limit is per account, "regardless of which API Key is used"). |
| DeepSeek `deepseek-v4-pro` | 500 concurrent per account (same source) | 32 | The checker is called once per attempt per part; 32 covers every part. |
| Anthropic (Opus 5.5, Sonnet 5.5, Start tier) | 4,000 RPM, 2M input tokens per minute, 400k output tokens per minute, plus "ramp limits" against sudden jumps ([Anthropic rate limits](https://platform.claude.com/docs/en/api/rate-limits)) | 16, growing by one step per minute | The output-token budget (400k per minute) is the binding limit at about 10k output tokens per call. The ramp-limit advice is to grow gradually. Cached input does not count toward the input limit. |
| OpenAI | Per-tier limits; traffic that ramps too fast is downgraded ([priority/fast mode guide](https://developers.openai.com/api/docs/guides/priority-processing)) | 16 | Same reasoning; the exact limits depend on the account tier. |
| Local server | One machine | 1 to 2 | A local server serialises requests anyway. |

DeepSeek holds a waiting request open with keep-alive comments and closes it if inference has not started within 10 minutes ([source](https://api-docs.deepseek.com/quick_start/rate_limit)). The client timeout of 1,800 s in `HostedLLM` covers this.

Expected gain from concurrency alone (keeping the stage barriers): each per-part stage drops from 7 waves to 1, so the per-part part of the critical path drops from about 120 to 180 calls to about 18 to 26 calls, a factor of about 6 to 7 on that portion. Stragglers (the slowest part of each stage) take back some of this.

### 3.2 Remove the stage barriers: a streaming graph per part

The stage graph (`stages.py`) already says what each stage needs. Per part, most needs are local:

- `ideas[k]` needs the outline.
- `rewrite[k]` needs `ideas[k]`, the **whole** termbase (terms defined earlier, terms of later parts for `term_order`), and the research results of the gaps whose node lies in part k.
- `spoken[k]` needs `rewrite[k]`. `audio[k]` needs `spoken[k]`.
- `course[k]` needs `rewrite[k]` and `ideas[k]`. The cold read of part k needs only its cards and the termbase. The revision needs the cold read of part k.
- `recitation[k]` needs `rewrite[k]` and `course[k]`.

The only true whole-lecture barriers are the outline, the termbase (and the gap search that feeds it) and the final documents. Everything else can flow part by part: part 1 can be voiced while part 20 is still being verified. This is the streaming form of a dependency graph that LLMCompiler used for function calls, where dispatching tasks as soon as their inputs exist gave speedups of 1.4 to 3.7 times over sequential execution ([Kim et al., ICML 2024, arXiv 2312.04511](https://arxiv.org/abs/2312.04511), Table 1; background evidence from 2024). Its analysis also explains the limit: the speedup is capped by the parts that cannot be parallelised and by stragglers (Appendix E.1).

The scheduler stays code. Each node of the graph is a (stage, part) pair, and a node starts when the nodes it needs are done. No model chooses what runs next, so the evidence behind ADR-0003 still holds: in STAGE, letting the model pick the next node cost 12 to 56 points of success across three repeated runs, and giving each node the whole procedure instead of its own part cost 22.5 to 74 points ([Luo et al., revised 1 November 2026, arXiv 2608.22538](https://arxiv.org/abs/2608.22538), Table 2).

**Expected gain.** The critical path becomes: outline (1) → gap search (about 1.3 with a chunked search and a targeted repair) → termbase (1) → the slowest part's chain (about 18 calls) → documents. That is about 21 calls instead of about 130 to 190, a factor of about **6 to 9**: from 2.5 to 4 hours to about 25 to 35 minutes of model-bound time at c ≈ 75 s, before research. Research becomes about one loop long (all gaps in parallel), about 5 minutes instead of about 50. Audio overlaps the model calls instead of following them, so the end of the run is the later of the last part's recitation and the last part's voice.

### 3.3 Prefix caching

DeepSeek caches prompt prefixes on disk for every user by default. A request hits the cache only when it fully matches a cached prefix, cached units are cut at the end of each user input and model output, and unused entries are cleared after "a few hours to a few days". A cache hit costs about one fiftieth of a miss: $0.003 against $0.15 per million input tokens for `deepseek-flash` off-peak, and $0.022 against $0.66 for `deepseek-v4-pro` ([DeepSeek context caching](https://api-docs.deepseek.com/guides/kv_cache), [pricing](https://api-docs.deepseek.com/quick_start/pricing)). Anthropic offers automatic caching with a top-level `cache_control`, a minimum of 512 tokens on Opus 5.5 and Sonnet 5.5, writes at 1.25 times the input price and reads at 0.05 times on those models; cached reads do not count toward the input-tokens-per-minute limit ([Anthropic prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching), [rate limits](https://platform.claude.com/docs/en/api/rate-limits)). OpenAI caches automatically above 1,024 tokens on current models, with discounts "up to 95%" ([OpenAI prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching)).

What the harness already gets right: `_ask_json` appends the rejection note at the **end** of the user message, so a retry reuses the cached prefix. The verify request places the script last, so its prefix is stable across repair attempts.

What defeats the cache:

- In `_rewrite`, the `FEEDBACK` section comes **before** `TRANSCRIPT OF THIS PART`, so every repair changes the prefix and re-reads the transcript. Placing feedback last makes repairs hit the cache.
- The whole-lecture calls (gap search, termbase, gap-search audit) carry the same lecture after different system prompts. Because the system message comes first, they share no prefix. Putting the lecture (slides and transcript) in an identical first block and the task instructions after it would let the second and third whole-lecture calls hit the cache. This changes where instructions sit, which can affect behaviour and must be checked with the audits.
- Within a part, the rewrite, verify, course and recitation requests each begin with different sections. A common opening block per part (language, glossary, slides of the part, transcript of the part) followed by the task would turn most of a part's calls into cache hits.

**Expected gain.** Mostly cost: input tokens become about 50 times cheaper on hits. Time to first token improves somewhat; the decode-bound part of each call does not change (§2.2). Anthropic's documentation states "improved time-to-first-token for long documents" without a figure. Barbaste et al. found that every major coding harness structures prompts around cache boundaries (the "prompt caching" pattern in 6 of 11 systems, and Claude Code's "prompt-cache-sharing forks"), which shows how central the practice has become ([arXiv 2609.00006](https://arxiv.org/abs/2609.00006), Table 11).

### 3.4 Repair the gap search by slide, and split it

Two changes, in the spirit of the bounded repairs the harness already has (ADR-0001, `repair_plan`):

1. **Targeted repair.** When the only gate error is "slides [20, 37, 62, 75, 91] have no idea listed", keep the answer and ask a short follow-up call for those slides only, with their text and the transcript nodes near them, then merge. Code checks the merge as it checks a full answer. A slide that still gets no idea after the targeted call is listed in `report.md` instead of discarding the whole gap search. This turns three 100-second failures into one 100-second call plus one call of a few seconds of output.
2. **Map-reduce by slide range.** Split the slides into ranges of about 15, give each call its slides and the transcript of the nodes that cover them, and run the ranges in parallel. Keep one whole-lecture call for what needs the whole lecture: terms used before their definition and fundamentals assumed across parts. Its output is small. ADR-0001 already lists this split as action item 8.

A third change follows from the measured failure: a failed gap search should not silently disable the research. Research per part can run on the gaps of the ranges that succeeded.

**Expected gain.** The gap stage drops from about 100 to 320 s to about 30 to 60 s, and research is no longer lost on a single gate error.

### 3.5 Route effort by difficulty, inside the fixed graph

"Model routing" in 2026 usually means choosing a cheaper model for easier requests. Two findings bound how far to take it:

- A per-step router inside an agent changes what happens next: swapping models mid-trajectory rewrote 61 to 94% of later actions in SWE-bench agents, and replay-based evaluation of routers mispredicted every outcome that mattered ([Gonuguntla, COLM 2026 workshop, arXiv 2608.08239](https://arxiv.org/abs/2608.08239)). So a routing change must be evaluated by running the harness, not by replaying logs.
- In this harness the routes are fixed per stage, not chosen per request, so the routing decision is a configuration that can be measured with the planted audits.

Proposed: keep `high` for the rewrite, the verifier, the course writer and judge, and the idea listing; use `low` for the spoken form, the terminology check and the cold read; keep thinking on everywhere, as the owner decided (CLAUDE.md, citing arXiv 2604.03616). Expected gain: shorter calls on three of the eight per-part stages. The size is unknown until measured, because DeepSeek does not publish reasoning lengths per effort level.

### 3.6 Take the audits off the critical path, keep every one

CLAUDE.md forbids removing the planted-omission tests to save calls. They do not need removing: they need running **beside** the main path. `audit_ideas` can start as soon as one part's ideas exist; `audit_coverage` as soon as the gap search is done; `audit_verifier` as soon as one accepted part exists; `audit_judge` likewise for cards. Their results go to `report.md` as now. Expected gain: 4 to 8 whole-call latencies (about 5 to 10 minutes) removed from the critical path, at no loss.

### 3.7 Batch APIs and off-peak pricing: for cost, not speed

- Anthropic Message Batches: 50% off, "most batches completing within 1 hour", and the discount stacks with caching ([source](https://platform.claude.com/docs/en/build-with-claude/batch-processing)).
- OpenAI Batch: 50% off within 24 hours; Flex processing is being phased out and is not offered on the newest models ([batch](https://developers.openai.com/api/docs/guides/batch), [flex](https://developers.openai.com/api/docs/guides/flex-processing)).
- DeepSeek: no batch API was found; off-peak hours cost half the peak price, peak being 01:00 to 04:00 and 06:00 to 10:00 UTC on weekdays ([pricing](https://api-docs.deepseek.com/quick_start/pricing)).

A batch makes each stage take up to an hour, and a dependent chain of 18 calls would take many hours. Batches therefore suit only an explicit "overnight" mode, for example re-running a whole course. They are the opposite of what the owner asked for.

### 3.8 Speculative and predicted outputs

OpenAI's Predicted Outputs work only with the GPT-4o and GPT-4.1 families and not with reasoning models or tools ([source](https://developers.openai.com/api/docs/guides/predicted-outputs)). They do not apply to the models this harness uses. The bounded repairs the harness already does (`splice_repair`, `splice_cards`) are the practical equivalent: code keeps the unchanged sections word for word, and the model regenerates only what failed.

### 3.9 Spending the saved time on "bigger"

The owner wants more calls, not fewer. Once the critical path is short, extra calls that run in parallel cost money but almost no time. In order of expected value:

1. **Factored verification per node** (research notes, idea 1): one verifier call per node, in parallel, instead of one per part. It multiplies verifier calls by the number of nodes but adds no length to the critical path.
2. **A second verifier from another family**, as a reported second opinion (research notes, idea 3). Judges from the same family favour each other's output (research notes, §2.8).
3. **Deeper research.** With all gaps in parallel, `--research-depth` can rise without lengthening the run much.
4. **Several lectures at once.** `run_series` prepares lectures one after another. Pass 1 (up to the termbase) of every lecture is independent and can run in parallel.

---

## 4. How the best harnesses work in late 2026, for a task like this

The architecture of the leading systems has converged on a few traits:

- **Hand-written loops, no agent framework.** None of the eleven production harnesses studied (Claude Code, Codex CLI, Gemini CLI, OpenHands and others, pinned to July 2026) imports a general agent framework such as LangChain or LangGraph; "the field runs on hand-rolled async loops and deterministic retrieval" ([Barbaste et al., arXiv 2609.00006](https://arxiv.org/abs/2609.00006)). lecture2audio's plain Python with JSON caches is in line with this.
- **Coordinator and workers for breadth.** Coordinator-worker delegation emerged independently in seven of the eleven systems, but it is "mostly used for breadth-first exploration phases (parallel codebase research) rather than for parallel implementation" (same source, §11 and §15.1). Codex adds map-reduce fan-out ("one sub-agent per CSV row under a shared JSON-schema result contract"); Claude Code forks children that share the parent's prompt cache. Anthropic's own guidance (2025, background) reports its multi-agent research system beating a single agent by 90.2% on broad research, at about 15 times the tokens of a chat, and warns that it fits poorly where agents must share context or depend on each other ([Anthropic engineering](https://www.anthropic.com/engineering/multi-agent-research-system)).
- **Code owns control on procedural work.** On procedures with dependencies, a reviewed graph with code-owned transitions and narrow per-node context beat a single agent holding everything in all 18 benchmark and model pairs, by 7.5 to 66 points of success on all three repeated runs (STAGE, [arXiv 2608.22538](https://arxiv.org/abs/2608.22538)).
- **Single model per decision, many decisions in parallel.** Studies from 2026 keep finding that a single model matches or beats multi-agent conversations at equal budget on sequential reasoning ([Xu et al., January 2026, arXiv 2601.12307](https://arxiv.org/abs/2601.12307); [Tran and Kiela, April 2026, arXiv 2604.02460](https://arxiv.org/abs/2604.02460); [Fu et al., revised October 2026, arXiv 2606.05670](https://arxiv.org/abs/2606.05670)). Xu et al. attribute part of the single model's advantage to cache reuse. All three were re-read for this note and say what CLAUDE.md attributes to them. The counter-evidence is specific: Tran and Kiela find multi-agent designs competitive when the context is heavily corrupted; Fu et al. report one workflow (EvoAgent) ahead of the single agent in pooled accuracy (79.70% against 76.65%) and, in a separate observational case study, a Claude Code workflow with agent teams answering 66.67% of GAIA tasks against 46.67% for the best fixed workflow, at fewer tokens. GAIA is open-ended retrieval, the same breadth-first kind of work as gap research.
- **Prompt-cache-aware structure and deferred loading.** Stable prompt prefixes, and tools or skills loaded only when needed, are standard: prompt caching appears in six of the eleven systems, and deferred loading of tools or skills in most of them (Barbaste et al., Table 11).

**What this means for lecture2audio.** The best design for this task is what the repository already has, run at the scale the provider allows: a fixed graph owned by code; narrow context per part; code gates before model judges; and **fan-out** wherever the work splits into independent pieces (parts, gaps, slide ranges, nodes for factored verification), with the provider's concurrency, not three threads, as the limit. The open-ended piece, web research per gap, is the one place where an agent loop belongs, and it already is one (`research_loop`), launched by code once per gap.

---

## 5. Agent skills, MCP, subagents and agent SDKs: what each is today

### 5.1 Agent Skills

An Agent Skill is a directory, not a single file. The open specification ([agentskills.io/specification](https://agentskills.io/specification), last edited 4 August 2026) defines:

- `SKILL.md` (required): YAML frontmatter with `name` and `description` (required) and `license`, `compatibility`, `metadata` and the experimental `allowed-tools` (optional), followed by Markdown instructions.
- `scripts/`: executable code the agent can run ("Python, Bash, and JavaScript" are common).
- `references/`: documentation loaded on demand.
- `assets/`: templates, images, data files, schemas.
- **Progressive disclosure**: about 100 tokens of metadata for every installed skill at start-up; the body (under 5,000 tokens recommended) when the skill is activated; the files "only when required". Anthropic adds that scripts run as tools without their code entering the context ([Anthropic engineering](https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills)).
- A validator, `skills-ref validate`.

Harnesses extend the format. Claude Code accepts about 20 frontmatter fields, including `hooks`; a skill can pre-approve tools, run "dynamic context" shell lines before the model reads it, and register hooks that keep running for the rest of the session with the user's full permissions; plugins can bundle skills with MCP server configurations and executables ([agenticskills.io: vetting a skill](https://agenticskills.io/learn/vet-agent-skill-before-install)). On 22 October 2026 Anthropic donated the standard to the Agentic AI Foundation, a Linux Foundation project ([agenticskills.io: governance](https://agenticskills.io/learn/agent-skills-linux-foundation)). Skills lead MCP in adoption among production harnesses (9 of 11 against 8 of 11, [arXiv 2609.00006](https://arxiv.org/abs/2609.00006)).

Evidence on value (2026): curated skills raised the average pass rate from 33.9% to 50.5% across 18 model and harness configurations, focused skills (at most three modules) beat exhaustive ones, and 13 of 87 tasks got worse; **skills written by the agent itself fell below having no skill** in all three configurations tested, by 8 to 11.5 points ([SkillsBench, revised June 2026, arXiv 2602.12670](https://arxiv.org/abs/2602.12670)). Skills "often provide no benefit, and can even hurt performance while incurring additional token costs" when used indiscriminately ([Xu et al., September 2026, arXiv 2609.32274](https://arxiv.org/abs/2609.32274)). A benchmark of skill selection from an installed library and of memory effects was published on 17 November 2026 with its results still pending ([SkillBuffBench, arXiv 2611.22017](https://arxiv.org/abs/2611.22017)).

### 5.2 The Model Context Protocol

MCP is a protocol by which a host application connects a model to outside capabilities. The current revision is **2026-07-28** ([specification](https://modelcontextprotocol.io/specification/latest)):

- JSON-RPC 2.0 messages, now "stateless, self-contained requests" with capability negotiation per request (earlier revisions opened a session with `initialize`).
- Server features: **tools** (functions the model may call), **resources** (data the host may read, such as files) and **prompts** (templates the user may pick). Client feature: **elicitation** (the server asks the user for input).
- **Deprecated** in this revision: sampling (servers asking the client's model for completions; the advice is to call provider APIs directly), roots, logging and dynamic client registration, removable from 2027-07-28 onward ([deprecations](https://modelcontextprotocol.io/specification/2026-07-28/deprecated)).
- Transports: stdio (a subprocess) and Streamable HTTP (one endpoint, replies as JSON or a server-sent-event stream scoped to the request) ([transports](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports)).
- **Tasks**, an opt-in extension for long operations: the server returns a durable `taskId`; the client polls with `tasks/get`, answers requests for input with `tasks/update` and cancels with `tasks/cancel`; statuses are working, input_required, completed, failed and cancelled ([tasks extension](https://modelcontextprotocol.io/extensions/tasks/overview)).
- Security: the best-practices page covers confused deputies, token passthrough, SSRF and local server compromise; tool descriptions "should be considered untrusted, unless obtained from a trusted server" ([security best practices](https://modelcontextprotocol.io/specification/2026-07-28/basic/security_best_practices)). Tool definitions and results also consume context: Anthropic reduced one workflow from 150,000 to 2,000 tokens by exposing MCP servers as code APIs instead of tool definitions ([Anthropic engineering, 2025, background](https://www.anthropic.com/engineering/code-execution-with-mcp)).

### 5.3 Subagents and agent SDKs

- **Subagents** are child agent loops started by a parent, each with its own context, returning a summary. They are how harnesses fan out (§4).
- The **Claude Agent SDK** packages "the same tools, agent loop, and context management that power Claude Code" for Python and TypeScript, with hooks, subagents, MCP, permissions, sessions and skills ([docs](https://code.claude.com/docs/en/agent-sdk/overview)).
- The **OpenAI Agents SDK** offers agents (instructions plus tools), handoffs, guardrails, tracing and sessions ([docs](https://openai.github.io/openai-agents-python/)).

None of these makes a call faster. They organise calls.

---

## 6. How lecture2audio should use skills and MCP: both directions

| Use | Recommendation | Why |
|---|---|---|
| **Expose lecture2audio as a skill** | Yes. Turn `.claude/skills/lecture2audio/` into a full skill: `SKILL.md` (when to use it, the one-folder input, how to read `report.md`), `scripts/` that call the command line and summarise `events.jsonl` and `report.md`, `references/` (the options, the meaning of each report section), `assets/` (an example folder). Mark it `disable-model-invocation` for runs that spend money. | Curated, focused skills help; agent-written ones hurt (SkillsBench). The procedure lives in the harness; the skill only teaches an agent to drive it. |
| **Expose lecture2audio as an MCP server** | Yes, as a thin layer over the command line. Tools: `build` (returns an MCP task, since runs take tens of minutes), `status` (reads `events.jsonl`), `redo`. Resources: `script.md`, `report.md`, `ideas.csv`, `termbase.json`, vault notes. Transport: stdio for local hosts. No sampling (deprecated). | Lets any MCP host (Claude Desktop, Claude Code, Codex, Gemini CLI, Goose) run lectures and query the ledger. The tasks extension matches a long run exactly. |
| **Consume MCP for research tools** | Optional. `--search mcp:<server>` could take `web_search` and `fetch_page` from an MCP server instead of `web.py`'s engines. | Opens more search engines without code per engine. Fetched content stays untrusted; quotes are still checked word for word by code (`check_facts`). Adds no speed. |
| **Subagents inside the harness** | Only as now: one research loop per gap, launched by code, in parallel. Possibly a coordinator per gap that splits a hard gap into two or three queries, bounded by code. | The breadth-first case where fan-out helps (Anthropic 2025; Fu et al. 2026). |
| **Agents that converse to write or verify** | No. | Sequential reasoning with shared context; the 2026 studies and STAGE above. Verdicts stay computed by code. |
| **Rebuild on an agent SDK** | No, for now. | The harness is provider-neutral and DeepSeek is the default; the Claude Agent SDK is tied to Claude's loop and the OpenAI SDK to its own. Production harnesses avoid frameworks (Barbaste et al.). |
| **Skills consumed by the harness's own prompts** | Not as a runtime feature. The prompts in `prompts/*.md` are the harness's equivalent of skill bodies, already loaded one at a time per stage. | No measured benefit from model-chosen skills inside a fixed graph; selection errors cost points (§5.1). |

---

## 7. Prioritised changes

| # | Change | Expected effect | Risk | Decision touched |
|---|---|---|---|---|
| 1 | Default concurrency from 3 to a shared per-model limiter (64 flash, 32 pro on DeepSeek; 16 elsewhere; adaptive on 429) | Per-part stages from 7 waves to 1: about 5 to 7 times less model-bound time | 429s under other load on the account; more parts flagged at once if a prompt is bad (cost, not quality) | `--workers` default (CLAUDE.md "Design decisions": parts run in parallel) |
| 2 | Targeted repair of the gap search for missing slides; a failed gap search no longer disables research | Gap stage from 100 to 320 s to about 100 s; research no longer lost | Merge must be checked like a full answer | ADR-0001 bounded repair, extended to the gap search |
| 3 | Audits beside the main path, not on it | 5 to 10 minutes off the critical path | None to quality; the audit result arrives at the same report | "Do not remove [audits] to save a call": kept |
| 4 | Feedback placed last in repair prompts; a common opening block per part and for whole-lecture calls | Input cost about 50 times lower on hits; somewhat faster first tokens | Section order can change model behaviour; check with audits and tests | Prompts and `dry_run_handler` phrases (CLAUDE.md conventions) |
| 5 | Per-part streaming scheduler derived from `stages.py`; audio starts with the first part | Critical path from about 130 to 190 calls to about 21: total 6 to 9 times less, audio overlapped | Tracker and app assume one stage at a time; the ledger is already updated per part | ADR-0003 (run order derived from the graph, listed there as "to revisit") |
| 6 | Gap search split by slide range, plus one whole-lecture pass for cross-part terms | Shorter outputs; parallel | Gaps that span ranges; the whole-lecture pass covers terms | ADR-0001 action item 8 |
| 7 | `reasoning_effort: low` for spoken form, terminology check and cold read | Shorter calls on three stages; size to be measured | Lower accuracy on those checks; measured by audits | CLAUDE.md "keep thinking on" (kept on; effort lowered) |
| 8 | Spend the time: factored verification per node, a second-family verifier, deeper research, parallel series | More checks at little added time | Cost | Research notes ideas 1 and 3 |
| 9 | Ship lecture2audio as a full skill and an MCP server (tasks extension) | Other agents can use it | Skill scripts and MCP servers run code: document permissions | ADR-0004 (interfaces) |

---

## 8. What could not be verified

- **DeepSeek's decode speed** and **whether reasoning tokens count inside `max_tokens`** are not stated on its pages; the code comment in `llm.HostedLLM` assumes they do. The 50 to 100 tokens per second estimate is an inference from one measured stream rate.
- **Audio duration** (Ming and Qwen3-TTS on Apple Silicon, plus the Whisper round trip) was not measured; it may dominate a run once the model-bound time shrinks.
- **The size of the effort-level gain** (change 7) is unknown until measured with the audits.
- **The DeepSeek concurrency figures** are those of the account tier the documentation shows; an account may have a different limit.
- **No API calls were made** for this note. All time estimates come from the code and from the one measured run.
- Most guidance on how many parallel agents are worth running at once is practitioner writing, not measurement (for example [Fowler, "The Orchestrator's Tax"](https://martinfowler.com/articles/orchestrator-tax.html)); the concurrency recommendation here rests on the provider's published limits and on the harness's own dependency graph instead.
