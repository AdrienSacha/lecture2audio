# Research notes for lecture2audio

These notes record the research behind the design of lecture2audio: for each source, what it found and what it changes in the build. They were compiled in November 2026 and updated on 3 November 2026 with sources published in 2026.

Three terms recur below.

- A **meta-analysis** pools the results of many experiments on the same question, so that one number summarises them.
- An **effect size**, written *g* or *d*, says how much a technique raises test scores compared with not using it, in units of the spread of scores. With *g* = 0.5, the average student who used the technique scored higher than about 69% of the students who did not. With *g* = 0.3, the figure is about 62%.
- An **arXiv preprint** is a paper posted publicly before, or without, review by other researchers. All the 2026 papers below are preprints unless a venue is given, so their results are less settled than those of the published meta-analyses.

Each source added in the November 2026 update carries a label in square brackets that states its standing:

- **[Q1 journal]** or **[A\* conference]**: a peer-reviewed paper in a journal ranked Q1 by Scimago, or in the main proceedings of a conference ranked A\* by CORE (ACL, EMNLP, ICLR, NeurIPS, Interspeech and similar).
- **[Findings]**: a peer-reviewed paper in *Findings of ACL* or *Findings of EMNLP*. These volumes are reviewed by the same committee as the main conference, but they hold papers that were not accepted to the main programme.
- **[Preprint]**: an arXiv paper with no venue, included only because it bears directly on a decision in the build.
- **[Practitioner]**: an engineering blog post from a model provider. It reports experience, not a controlled study.
- **[Tier not checked]**: a peer-reviewed journal whose Scimago rank I could not confirm.

---

## 1. How the audio is organised: learning science

### 1.1 Showing the structure (signaling)

**Sources.**
- Schneider, Beege, Nebel and Rey (2018) pooled 103 studies with 12,201 participants. Cues that show how the content is organised raised retention by *g* = 0.53 and transfer (applying the knowledge to new problems) by *g* = 0.33. Signaling worked best when it clarified the structure of the content and pointed attention to essential information.
- Alpizar, Adesope and Wong (2020) pooled 29 studies with 2,726 participants and found *d* = 0.38. Effects were larger for shorter lessons.
- Xie and colleagues (2017) pooled 32 studies. Cues reduced total mental effort (*d* = −0.11) and improved retention (*d* = 0.27) and transfer (*d* = 0.34).
- The summaries above report that learners with little prior knowledge benefit most. That is the situation of a student hearing a course for the first time.

**Limit.** Most of these studies use visual cues (headings, colour, arrows). None of the summaries I read isolates spoken cues in narration-only material. Section 1.2 is the closest evidence for audio. A search in November 2026 found no 2026 meta-analysis or experiment in a Q1 journal that tests spoken signaling in audio-only material; section 1.9 describes the only 2026 study found on spoken emphasis, and it did not measure learning.

**What it changes in the build.**
- The outline stage (`outline.py`, `prompts/outline.md`) gives every lecture an explicit tree of parts, points and sub-points.
- Each part opens with a spoken preview built by code: "This part has three points. First, … Second, … Third, …" (`list_sentence` and `part_blocks` in `pipeline.py`). A point with two or more sub-points gets the same kind of preview.
- Each point opens with a sentence that names it ("The second point is …"), required by rule 2 of `prompts/rewrite.md`.
- Each part closes with a recap of two or three sentences.

### 1.2 Structure statements in spoken lectures

**Source.** Chaudron and Richards (1986) compared four versions of the same lecture: no markers, small markers only, structure statements only, and both. Here, small markers are words like "well", "so" and "OK"; structure statements are sentences like "What I'm going to talk about today is …". Structure statements led to better recall of the lecture than small markers.

**What it changes in the build.**
- The rewrite prompt removes the small markers as noise (rule 9 of `prompts/rewrite.md`), because the study suggests they add nothing for the listener.
- The spoken previews and the transition sentences of section 1.1 are structure statements.

### 1.3 Previews that link the known to the new (advance organizers)

**Sources.** Ausubel (1960) introduced the "advance organizer": a short introduction, given before new material, that connects it to what the learner already knows. Mahar (1992) updated the meta-analysis of this research. In that summary, students who received an organizer outperformed 67% of control students, and organizers that link familiar material to unfamiliar material had the strongest effect. Most organizers tested were incomplete versions of Ausubel's idea, so the evidence is real but modest.

**What it changes in the build.** The first sentence of each preview ("intro" in `prompts/rewrite.md`, structure rule 3) says what the part is about and what it is for, before the list of points. The rewrite prompt's teaching rule 16 puts "what the idea is for" before its definition.

### 1.4 Removing noise and naming concepts first (coherence and pre-training)

**Source.** Mayer (2020), *Multimedia Learning*, 3rd edition, sets out evidence-based principles. Two apply directly. The coherence principle says that learners do better when extraneous material is excluded. The pre-training principle says that learners do better when they already know the names and characteristics of the main concepts before the explanation.

**What it changes in the build.**
- Coherence: noise units (filler, sips of water, room talk) are marked by the outline and left out of the script. Every noise unit is listed at the end of `report.md`, so that a wrong exclusion can be found.
- Pre-training: the outline lists the terms each part introduces (`terms` in `outline.json`). Each part receives the terms already defined earlier and the terms it must define (`_context` in `pipeline.py`). Rule 12 of the rewrite prompt requires a definition, a purpose and an example at a term's first use.

### 1.5 Retention: being tested, and spacing the reviews

**Sources.**
- Yang, Luo, Vadillo, Yu and Shanks (2021) pooled 222 classroom studies with 48,478 students. Quizzing raised achievement by *g* = 0.50. Corrective feedback and repeated tests are among the conditions that change the size of the effect.
- Dunlosky, Rawson, Marsh, Nathan and Willingham (2013) reviewed ten study techniques. Only two were rated "high utility": practice testing and distributed practice, meaning reviews spread over time. Summarising, highlighting and rereading were rated "low utility".

**What it changes in the build.**
- Each part ends with two or three questions. The audio reads the question, leaves a silence (`--quiz-pause`, 8 seconds by default) for you to answer in your head, then gives the answer, which serves as corrective feedback.
- `questions.md` collects all questions with the answers at the end, so that you can test yourself again on a later day (distributed practice).
- Listening to the recording a second time is the audio equivalent of rereading. So the recording alone is not expected to make you retain the lecture. The questions and the later reviews are what the evidence supports.

### 1.6 At most three levels

**Source.** None. This is a design judgment: a listener cannot glance back at a heading, so a deep tree cannot be followed by ear.

**What it changes in the build.** `MAX_DEPTH = 3` in `outline.py`: part, point, sub-point. A node with a single child is folded into its parent, because one child says nothing about structure.

### 1.7 Is model-written text easy to listen to? (2026)

**Source.** Hjuler, Tran and Sitbon (Interspeech 2026) **[A\* conference]** compared how easy synthetic speech is to listen to when the text read aloud comes from four registers: conversational speech (radio transcripts), formal written text (Wikipedia), simplified written text, and text generated by a language model. In a within-subjects study with 47 listeners, they measured self-reported mental effort, cognitive load and cued recall. Synthetic speech made from written sources was easier to listen to than speech made from radio transcripts. Text generated by a language model was as easy to listen to as the Wikipedia text. The authors call the study preliminary.

**What it changes in the build.** It supports the central choice of the tool: a transcript of spoken teaching is a poor text to feed to a voice as it stands, and rewriting it into connected written sentences is expected to make it easier to follow. The teaching rules 14 to 17 of `prompts/rewrite.md` (full connected sentences, one idea per sentence, formal register, no filler) are the kind of written register that did well in the study. No code changes. The study did not test lectures or long recordings, so it does not settle whether the gain holds over a whole lecture; idea 7 of section 4 remains the way to check this on yourself.

### 1.8 Audio revision without built-in questions (2026)

**Source.** Joshi and colleagues (2026, *BMC Medical Education*) **[Tier not checked]** gave 203 medical students at two colleges six revision episodes of 8 to 10 minutes each, written by staff, formatted with the help of a language model, and voiced by text-to-speech. Scores on a 60-question test rose right after listening at both colleges. Thirty days later, the gain held at one college but fell significantly at the other. Students who completed more than half the episodes gained more. In focus groups, students said that the episodes had no pauses and no self-check questions, and the authors recommend adding both. The study had no control group, so concurrent exam preparation could explain part of the gain.

**What it changes in the build.** Nothing new is needed: each part already ends with questions, a silence for answering (`--quiz-pause`) and the answer (`part_blocks` in `pipeline.py`, `WORDING` for the fixed phrases). The study is a field report that students asked for exactly this, and that gains from listening alone can fade within a month. That strengthens idea 4 of section 4 (spaced review beyond one file).

### 1.9 Emphasis in a synthetic voice (2026)

**Source.** Chen, Wei, Cao and Huang (2026, *European Journal of Education*) **[Tier not checked]** showed 340 students in grades 8 and 9 mathematics videos narrated by an AI voice whose gender, pitch and emphasis on key information were varied. Emphasis on key information raised the students' stated intention to engage (β = 0.066). The study measured intention to engage, not learning, and used video, not audio alone.

**What it changes in the build.** Nothing. The evidence is too weak to justify a change: it does not measure learning, and only one of the voice backends (`OpenAITTS` in `tts.py`, through its `instructions` field) offers any control over emphasis. It is recorded as idea 11 of section 4, at low priority.

---

## 2. How the pipeline is organised: language-model systems

### 2.1 Fixed chains of calls, or autonomous agents

**Source.** Anthropic (2024), "Building effective agents", distinguishes two designs. In a **workflow**, code decides the sequence of model calls. In an **agent**, the model decides its own steps. Its "prompt chaining" pattern places code checks, called gates, between the calls. Its "evaluator-optimizer" pattern has one call write and another evaluate, in a loop. The article advises using the simplest design that works and adding complexity only when measurement shows a gain.

**What it changes in the build.** lecture2audio is a workflow. Code calls, in order: the outline, then for each part the rewrite, the code gates, the verifier and the repairs, then the spoken form. The rewrite–verify–repair loop is the evaluator-optimizer pattern.

### 2.2 One model or several (2026 studies)

A **multi-agent system** is a set of model instances, each with a role, that pass work to each other.

**Sources.**
- Xu and colleagues (January 2026) found that a single model, holding a multi-turn conversation, matched multi-agent workflows across seven benchmarks (coding, mathematics, question answering, domain reasoning, planning and tool use) at lower cost.
- Tran and Kiela (April 2026) showed, in theory and in experiments with Qwen3, DeepSeek-R1-Distill-Llama and Gemini 2.5, that a single model matches or beats a multi-agent system on multi-step reasoning when both get the same budget of reasoning tokens. Multi-agent systems became competitive only when the context was corrupted. The authors conclude that many reported multi-agent gains come from extra, uncounted computation.
- Fu and colleagues (June 2026, revised October 2026) compared six multi-agent workflows with a single agent using GPT-4.1, with the same tools, scoring and step limits. Only one workflow beat the single agent overall (79.70% against 76.65%). The other five trailed it by 1.71 to 8.76 points, and some used up to 4.34 times as many tokens.
- Earlier, Kim and colleagues (2025, Google Research) tested 180 configurations. Multi-agent coordination helped a task that splits into parallel pieces by 80.9%, and hurt a sequential planning task by 39% to 70%. Agents working without a coordinator amplified errors up to 17.2 times; a central coordinator limited this to 4.4 times.
- Anthropic (2025) reports the opposite case: a multi-agent research system beat a single agent by 90.2% on broad research questions, at about 15 times the tokens of a chat. The tasks were broad searches split into independent parallel pieces.

**What it changes in the build.**
- The work of a single part is sequential (write, then check, then repair), so one model does each step. There are no agents talking to each other.
- The parts, however, are independent once the outline exists, like the parallel tasks above. They run three at a time (`--workers`, `_map` in `pipeline.py`).
- The outline is the central plan, written once for the whole lecture. Every part receives it, so no part works blind (`render_outline`).

### 2.3 Why multi-step model systems fail

**Source.** Cemri and colleagues (2025) studied over 200 runs of seven multi-agent frameworks and classified 14 failure modes in three groups: problems with the task specification (41.8% of failures), misalignment between agents (36.9%), and weak verification (21.3%). Verifiers often checked only the surface, for example that code compiled, and not whether it was correct. Clearer role specifications raised one system's success by 9.4%. Adding a check on the high-level goal, on top of the low-level checks, raised it by 15.6%.

**What it changes in the build.**
- Specification: each rewrite receives a machine-readable required structure (`structure()` in `outline.py`). It lists the exact sections, preview items and sub-point previews expected.
- Verification at several levels:
  - **Sentence level:** the number check flags any number in a `lecture` sentence that appears in neither the transcript nor the slides (`unsupported_numbers`).
  - **Part level:** the verifier gives a status for every node of the outline.
  - **Lecture level:** the outline checks, the planned-term check, the noise-share warning and the verifier test of section 2.7 (`global_notes`, `audit_verifier`).
- Against surface checks: the verifier must return one status per node, and an answer that skips a node is refused (`validate_verify`).

### 2.4 Code checks before model checks

**Source.** Afoud and Wu (September 2026, revised October 2026) proposed verification as a separate layer of an agent, modelled on the "V-model" of systems engineering: one verifier per level of the specification, each made of a code check that costs nothing and an optional model judge. In their pilot (47 runs, small scale), the code checks produced 8 of the 9 corrections observed. The verified agents could also stop and decline to answer instead of running until their budget ran out.

**What it changes in the build.**
- The structure gate (`validate_part`) and the number check run before the verifier, so a malformed script never costs a verifier call.
- The outline gate (`parse_outline`) runs before anything else.
- The verdict on a part is computed by code from the verifier's statuses and issues, never taken from the model's own verdict.
- A part that still fails after the repairs is not hidden. It is flagged in `report.md`, which is the program's way of declining.

### 2.5 A verifier needs the source

**Sources.**
- Kamoi and colleagues (2024) surveyed self-correction. A model prompted to correct its own answer, without outside information, does not reliably improve it. Correction works when reliable outside feedback is available, or when checking is much easier than producing the answer.
- Dhuliawala and colleagues (2024) introduced Chain-of-Verification: the model plans verification questions about its draft, answers them separately, and revises the draft. Answering each question without seeing the draft worked best, because answers that see the draft repeat its errors. On long biographies the best variant scored 71.4 against 55.9 for the baseline.

**What it changes in the build.**
- The verifier always receives the transcript of the part, so it checks against the source, not against its own opinion.
- The check is broken into small questions: one status per node, then separate checks for faithfulness, definitions, the recap, the questions, and listening.
- The verifier is a separate call, with another model by default (Opus verifies what Sonnet wrote).

### 2.6 Repairs can make things worse

**Sources.**
- Wu, Shen, Yang, Peng and Hu (July 2026) studied loops in which a verifier checks an answer and a repairer fixes it, when both make mistakes. Repairs can damage answers that were already correct. In one setting, five fixed rounds of repair cut the share of valid answers from 0.70 to 0.12, while the verifier's acceptance rate kept rising. Their robust fallback keeps the best candidate so far, replaces it only when a new one is clearly better, and commits that candidate rather than the last one.
- Zhang (September 2026) **[Preprint]** tracked, across 29 open-weight models, how often a second pass turns a right answer into a wrong one and the reverse. Average accuracy hid large differences. For example, one model gained 25.5 points on a mathematics benchmark while its revision turned 19.1% of its initially correct answers into wrong ones, and another, on a yes-or-no reading task, repaired 122 errors but broke 159 correct answers. More forceful instructions to change the answer raised harmful changes more than helpful ones. The author concludes that a revision should be treated as a policy and judged on both the errors it repairs and the errors it introduces. The study tests revision without outside feedback, which differs from lecture2audio, where the repair receives the verifier's findings.

**What it changes in the build.** In `_process` (`pipeline.py`):
- the best attempt is kept and replaced only by an attempt with strictly fewer blocking problems;
- the feedback for the next repair is built from the best attempt, not from the latest one;
- there are at most two repairs (`--max-retries`, default 2);
- the loop stops as soon as an attempt has no blocking problem.

Zhang's study supports these choices. It also shows that counting blocking problems hides what a repair broke: an attempt with fewer problems may still have lost a correct passage that the previous attempt had. Idea 9 of section 4 would record this.

### 2.7 Checking the verifier itself

**Source.** Zhang and colleagues (October 2026; NeurIPS 2026 workshop "Who Verifies the Agents?") showed that, without safeguards, a verifier can collapse into one that passes everything. Such a verifier can still appear to work, because the system it supervises keeps improving on other signals. Their safeguard is a small set of reference cases with known answers, against which the verifier is checked.

**What it changes in the build.** Once per run, `audit_verifier` takes an accepted part and removes the lecture sentences of one point, keeping only its opening sentence. It then asks the verifier again. If the verifier does not report the omission, `report.md` warns that its passes are less trustworthy. The result is cached in `audit.json`; `--no-audit` skips it.

### 2.8 Judges favour their own family

**Sources.**
- Panickssery, Bowman and Feng (2024) showed that GPT-4 and Llama 2 recognise their own outputs and rate them higher. The two abilities were linked.
- Awuni and colleagues (September 2026) measured, across 9,312 judgments with four open model families, that a judge favours outputs from its own family by 3.4 to 8.4 percentage points. Changing the mix of families on a judging panel changed 18.5% of the outcomes.
- Pombal, Rei and Martins (2026) **[Preprint]** studied rubric-based judging, in which a judge gives a yes-or-no verdict on each criterion, as the lecture2audio verifier does for each node. The bias persisted even on objective criteria: judges were much more likely to mark a failed criterion as satisfied when grading their own outputs. Using several judges together reduced the bias but did not remove it. On a medical benchmark, the bias moved model scores by up to 10 points.
- Chae and colleagues (Findings of EMNLP 2026) **[Findings]** had ten models judge items that carry no stylistic trace of their author. When the judges did not know who wrote an item, the preference for their own work disappeared. When an item was simply labelled "self" or "other", without naming any model, judges raised the scores of items labelled as their own and lowered the others, whoever the true author was.

**What it changes in the build.**
- `--verify-llm`, `--verify-model` and `--verify-base-url` let the verifier come from another provider or another family.
- The OpenAI and local (OpenAI-compatible) clients now exist in `llm.py` and are tested offline, so `--llm anthropic --verify-llm openai` runs the rewrite on Claude and the verifier on OpenAI. With `--llm openai` or `--verify-llm openai`, the default models are `gpt-6.1-sol` for the rewrite and `gpt-6-astra` for the verifier.
- The default is still Claude for both roles, because no real API call has yet been tested for any provider. This is a known weakness, not a recommendation. Pombal and colleagues' result applies directly to the node statuses of `prompts/verify.md`, which makes idea 3 of section 4 more pressing.
- Chae and colleagues show that a label of authorship alone biases a judge. The verifier prompt already says only that "a script ... was made from one part of a lecture transcript", and `_verify` in `pipeline.py` never says which model wrote it. This must stay so; any later change to the prompt should keep authorship out of it.

### 2.9 Plan, execute, verify, re-plan

**Source.** Zhang and colleagues (March 2026, ICLR 2026 workshop on multi-agent systems) built a system in which a planner breaks a question into sub-questions with verification criteria, executes them in parallel (three at a time), verifies each result for completeness, and re-plans for the gaps. It stopped after at most three iterations. It kept earlier results instead of discarding them. On 25 market-research questions it raised completeness from 3.1 to 4.2 on a 1-to-5 scale over a single agent, at about 8.5 times the tokens. The authors note that the verifier checked completeness and sources, not factual accuracy, and that the judge came from the same model family.

**What it changes in the build.** It confirms the shape already chosen. There is one plan (the outline), with a checklist per part (`checklist()`), parallel parts, a completeness check per node, a small iteration cap, and earlier results kept. Its stated weaknesses are the reasons for the faithfulness checks in `prompts/verify.md` and for the `--verify-llm` option.

### 2.10 Loop engineering

**Source.** "Loop engineering" is a term that spread among developers in 2026, after posts by Boris Cherny (Claude Code) and Peter Steinberger, and an article by Addy Osmani (ADTmag, 1 July 2026). It means designing the cycle in which a model produces work, a check judges it, and the model repairs it, so that the cycle can run without a person prompting each step. It is a practitioners' term, not a research field. The principles named are:
- verification built into the loop;
- defined stop conditions and limits on failures;
- points where a human must step in;
- logs;
- a cost budget.

**What it changes in the build.**
- Verification is built in (sections 2.3 to 2.7).
- Stop conditions: no blocking problem, or two repairs.
- Failure limit: three tries for any malformed answer, then either an error or a fallback (the outline falls back to parts cut by size).
- Human step-in: the flagged parts in `report.md`.
- Logs: every stage's result is saved under `out/<name>/`.
- Cost: the "Model use in this run" table in `report.md`, with calls and tokens per model.

### 2.11 A strong writer can fool a strong verifier (2026)

**Source.** Zhou and colleagues (ICLR 2026) **[A\* conference]** studied verifiers that reason in writing before giving a yes-or-no verdict, across 12 benchmarks, 14 open models and GPT-4o. Verifiers confirmed correct answers more reliably on easy problems. Errors made by weaker writers were easier to detect than errors made by stronger ones, because strong writers produce well-structured answers that are wrong. A stronger verifier sometimes offered little advantage over a weaker one, so a larger verifier model does not by itself remove the limits of verification.

**What it changes in the build.** It weakens one of the reasons given in section 2.5 for the default setting, "Opus verifies what Sonnet wrote": a stronger verifier model is no guarantee, and the scripts that Sonnet writes are fluent and well structured, which is the case in which verifiers miss errors most. It strengthens three existing choices: the code checks that do not depend on the verifier (`validate_part`, `unsupported_numbers`), the planted-omission test (`audit_verifier`), and idea 2 of section 4 (a small reference set to measure verifiers). No code changes.

### 2.12 Omissions that mislead (2026)

**Sources.**
- Li and colleagues (ACL 2026) **[A\* conference]** studied news previews (a picture and a headline) that are accurate in every statement but mislead because they leave out context given in the full article. Open vision-language models often failed to detect such omissions. Their analysis found that misleading omissions usually come from missing local background, not from a change in the overall framing.
- Tang, Zhang, Feng and Tung (EMNLP 2026) **[A\* conference]** addressed "half-truths", claims that are technically accurate but mislead because they omit context. Their method has two model roles argue over the same evidence while a third judges, and it improved omission detection over single-model and multi-model baselines at lower reasoning cost.

**What it changes in the build.** Both papers confirm that omission is a separate failure from error: a script can contain only true sentences and still mislead. Rule 7 of `prompts/rewrite.md` already forbids dropping a qualification such as "only if" or "at most", and check 3 of `prompts/verify.md` classes a sentence that "strengthens or weakens" what the teacher said as "wrong". Li and colleagues' finding that the harm comes from missing local background matches the node-by-node coverage check, which looks for every condition and warning of each node's own units. Tang and colleagues use several model roles that argue; the design decision recorded in section 2.2 is kept, because their gain was measured on fact verification with retrieval, not on a sequential rewriting task, and no comparison at equal token budget was reported. No code changes.

### 2.13 Faithfulness measures fail on long documents (2026)

**Source.** Mujahid, Wright and Augenstein (ACL 2026) **[A\* conference]** tested six automatic faithfulness measures, designed for short summaries, on long documents from fiction, law and science. They changed summaries in ways that keep the meaning (paraphrase, simplification, synonyms, equivalent negation and others). The measures gave different scores to summaries that mean the same thing, and became less reliable for dense claims that resemble many parts of the source. Giving the measures more context helped in some domains, but no measure stayed reliable on long inputs.

**What it changes in the build.** It supports two existing choices. First, the verifier checks one part at a time against that part's transcript (`render_part_transcript` in `outline.py`), not the whole lecture at once, which keeps the source short. Second, the build does not use an off-the-shelf faithfulness score as a gate; the only automatic faithfulness check is the narrow number check (`unsupported_numbers`). No code changes.

### 2.14 Checking several criteria in one call (2026)

**Sources.**
- Yu and colleagues (August 2026) **[Preprint]** found that when a judge grades a response against several criteria in one call, its verdict on one criterion changes with the other criteria present. In their preliminary study, only about one third of samples received fully consistent verdicts when the set of criteria was enlarged, reduced, reordered or given irrelevant extra criteria. Their remedy, a training method, does not apply to a model used through an API.
- Bagaria, Sundaram, Krishnan and Ravindran (EMNLP 2026) **[A\* conference]** showed that a classifier that sees only the rubric, and never the response, can partly predict a judge's scores, and that judges often fail to change their decision when the response or the criterion is reversed.
- Anthropic (January 2026), "Demystifying evals for AI agents" **[Practitioner]**, recommends layering code-based, model-based and human graders; calibrating model graders against human experts; reading the transcripts of many trials; giving the model grader a way to answer "Unknown" when it lacks information; and using a separate, isolated judge for each dimension of a rubric rather than one judge for all.

**What it changes in the build.** The verifier checks every node of a part, and then six kinds of issue, in a single call (`_verify` in `pipeline.py`, `prompts/verify.md`). Yu and colleagues and the Anthropic guidance both suggest that this saves calls at some cost in consistency. This strengthens idea 1 of section 4 (factored verification per node). Bagaria and colleagues show that a judge may follow the rubric rather than the response, which is the failure that the planted-omission test (`audit_verifier`) is designed to detect; this supports keeping it. The Anthropic advice to let a grader say "Unknown" is recorded as idea 8. No code changes now.

### 2.15 Filling the gaps of a lecture, and one meaning per term (decision of November 2026)

**Decision.** The user asked that the program find what a lecture leaves out (slide ideas never taught, terms never defined, fundamentals taken for granted, steps skipped), look it up online, and fill it; and that every technical term have one precise meaning, be defined at its first use, and never be swapped for a near word. This extends the rule "everything the teacher taught stays" with a second rule: "nothing the student needs is missing, and what was added is marked".

**How it is built, and which earlier sources each choice rests on.**
- Still a workflow (section 2.1): four new stages run once per lecture before the rewrite. One call finds the gaps (`prompts/coverage.md`, `run_coverage`); one call builds the termbase (`prompts/termbase.md`, `run_termbase`); one research call per gap, in parallel, with web search (`prompts/research.md`, `run_research`). The research call is the only open-ended step, and it fans out over independent gaps, the case in which Anthropic (2025) and Kim and colleagues (2025) found parallel work pays.
- Code gates before any judge (section 2.4): every slide must have its ideas listed, and every partial or missing idea a gap (`validate_coverage`); every term a definition relies on must have its own entry and no definitions may be circular (`validate_termbase`).
- Outside feedback, not the model's memory (section 2.5): a fact found online is kept only if its quote is found word for word in a page the provider returned or that the program fetched itself (`check_facts`, `web.py`). The program fetches only public addresses. Facts that fail the check are listed in `report.md`.
- Specification (section 2.3): each fact becomes a supplement attached to a node. The rewrite must teach it, tagged `external` with its source id (`validate_part`); an untaught supplement is a blocking problem; numbers in an external sentence must come from its supplement. The verifier checks external sentences against the quoted passage.
- Marking without interrupting: one natural phrase opens each passage of external material; short external clarifications are not marked (rule 11b of `prompts/rewrite.md`). A missing or repeated marker is a minor issue, so it never triggers a repair on its own.
- One meaning per term: the termbase gives each term one definition, the terms it relies on, and the terms it must not be confused with. Code places each term's definition at its first use, or earlier when a term defined earlier relies on it (`build_termbase`), so fundamentals come first. Code flags a confusable word that the sources of the part never use (`stray_terms`) and a term used before the part that defines it. A separate judge checks every occurrence of every term against its definition (`prompts/terms.md`, `_check_terms`), and must give one status per occurrence (`validate_uses`), because a judge that checks several criteria in one call mixes them (section 2.14).

**Limits.** Every gap is researched, with no cap on their number; `--research-depth` (30 by default) sets the searches and page reads per gap. Providers with their own web search (Anthropic, OpenAI) use it; any other model that calls tools (DeepSeek, Kimi, OpenRouter, a local server) runs a search loop that the program drives, through DeepSeek's, Tavily's or Brave's search API or a self-hosted SearXNG (`research_loop` in `llm.py`, `make_search` in `web.py`). The same quote check applies to both. None of this has been tested with real API calls yet.

---

### 2.16 One idea ledger, Obsidian as the only interface (decision of November 2026)

**Decision.** The user removed the browser interface (`gui.py`, `start.py`): the program is a command line that takes one folder, and Obsidian is the only place where the results are read, browsed as a graph and reviewed. The course material is on by default, and each part note embeds its own recording, so reading and listening happen in the same note.

**The idea ledger.** The atomic ideas that the gap search lists (`transcript_ideas`) are written to `ideas.csv` and checked off twice: by the verifier against the script (`prompts/verify.md`, field `ideas`) and by the course judge against the cards (`prompts/course_check.md`, with the cards that teach each idea). The cards also claim their ideas (`Card.ideas`), and code refuses a set of cards in which an idea of the part is claimed by no card before any judge reads them, in line with section 2.4. This turns completeness, which section 2.12 shows a judge often misses when it is asked as one criterion among many (section 2.14), into a checklist that the model must answer item by item and that code counts. The ledger is rebuilt from the cached stage results on every run, so it cannot drift from them.

**Figures.** A frame is either Mermaid or an ASCII box drawing (`course_schema.Frame`). Rules 11 to 16 of `docs/learning-science.md` set when a figure is required (structure, state, protocol, layout, change over time), that each frame shows one change and the full state, that the change is signalled (`classDef active`), and that a figure has at most eight frames. ASCII is used where exact positions matter (bit fields, stack layouts), since Mermaid cannot place cells at fixed offsets. The course judge now reads figures and tables as well as text.

**Scaffolds.** Prerequisite reminders are a collapsed callout (rule 21, expertise reversal); definitions, instances, worked examples and figures stay open (rule 35); answers stay collapsed (rule 20).

**Ideas part by part.** The ideas are listed in one call per part (`run_ideas`, `prompts/ideas.md`), run in parallel like the rewrite, and checked by a code gate (`validate_idea_list`: every node with units has an idea, every idea cites units of its part). The gap search keeps the whole lecture, because a gap such as a term used in one part and defined in another needs it, but it no longer lists the ideas, so its own output is shorter. Ids are scoped to the part (`T3.12`), so a part that is redone or that failed once never renumbers the others, whose cached results refer to those ids. The ledger file is kept current during the run: it is rebuilt from the results received so far and replaced atomically each time a part finishes.

**Every judge of completeness is tested.** Section 2.7 argues that a verifier must itself be checked. The same planted-fault test now covers the idea listing (one unmistakable fact planted in a copy of a part must be listed) and the course judge (an emptied card must make its idea "missing", scored on the judge's own verdict), besides the verifier and the gap search. When a judge says "taught" but the script or the named cards lack a number of the idea that the transcript holds, code marks the idea `disputed` instead of overruling the judge, in the spirit of section 2.4: a code check that cannot tell "10^6" from "a million" must not force repairs, and each audit scores the judge's own verdict, so that the code check never hides a judge that passes everything. The architecture is recorded in `docs/adr/0001-pipeline-architecture.md`.

## 3. The CLAUDE.md file

**Source.** Anthropic's Claude Code documentation (pages "Best practices for Claude Code" and "How Claude remembers your project", 2026):
- keep the file under 200 lines;
- include only what cannot be learned by reading the code: the exact commands that check the work, misleading places in the code, decisions that are not written in the repository, and anything that had to be corrected twice;
- group lines under headings;
- avoid contradictions;
- do not add capitals or "IMPORTANT" for emphasis, because current models follow a plainly worded line and over-apply emphasised ones;
- a rule that must never be broken belongs in a hook (a script that Claude Code runs automatically), not in the file;
- personal preferences go in `CLAUDE.local.md`, which is not committed.

**What it changes in the build.** `CLAUDE.md` has 38 lines in four sections: commands, conventions, design decisions, gotchas. `CLAUDE.local.md` is listed in `.gitignore`. No hook was added, because no rule here is absolute yet.

---

## 4. Ideas not built yet

Each idea comes with the source that suggests it and the reason it is not built.

1. **Factored verification per node** (Dhuliawala et al., 2024; Yu et al., 2026; Anthropic, 2026). Verify each node in its own call, without showing the verifier the other sections. This would likely catch more omissions and give more consistent statuses, since a verdict on one node would no longer depend on the other nodes checked in the same call. It would multiply the verifier calls by the number of nodes. Worth testing on one lecture if the planted-omission test shows weak verifiers. It would touch `_verify` in `pipeline.py` and `prompts/verify.md`.
2. **A small reference set for the verifier** (Zhang et al., October 2026; Wu et al., July 2026; Zhou et al., 2026). Check by hand five to ten parts of real lectures, record the true problems, and measure how often each candidate verifier agrees. This would show which verifier model to trust; Zhou and colleagues show that the larger model is not always the better verifier. It would also make calibrated stopping possible: the repair loop could then stop when one more repair is more likely to harm than to help. It needs real lectures first.
3. **A verifier from another family by default** (Awuni et al., 2026; Pombal et al., 2026). The OpenAI client now exists (`--verify-llm openai`), so the only step left is to make another family the default verifier once real calls have been tested. Pombal and colleagues found that self-preference affects yes-or-no rubric verdicts like the node statuses, and that a panel of judges reduces but does not remove it.
4. **Spaced review beyond one file** (Dunlosky et al., 2013; Yang et al., 2021; Joshi et al., 2026). Two options: export `questions.md` as a CSV that a flashcard program such as Anki can import, or open each new lecture's audio with two or three questions from the previous lecture. Joshi and colleagues saw gains from audio revision fade within thirty days at one of two colleges.
5. **Outline in windows for very long transcripts.** The outline is one call on the whole transcript. That fits a 90-minute lecture in current Claude models, but may not fit a small local model.
6. **Topic boundaries from slides.** The outline receives the slide text, but slide titles are not yet used as explicit candidates for part boundaries.
7. **Measuring the result on yourself.** No study above tests audio rewritten by a model from a lecture. A simple personal check would be to answer `questions.md` a week after listening, for lectures heard in the original recording and for lectures heard through the tool, and compare.
8. **A status for "cannot judge"** (Anthropic, 2026). Allow the verifier to give a node the status "unclear" when the transcript is too garbled to decide, instead of forcing "complete" or "incomplete". Such nodes would be listed in `report.md` for a human to check, without triggering a repair. It would touch `prompts/verify.md`, `STATUSES` and `validate_verify` in `pipeline.py`, `dry_run_handler` in `llm.py`, `report.py`, and the tests, in one change, as `CLAUDE.md` requires. Not built because no real lecture has yet shown how often the verifier meets such passages.
9. **Recording what each repair broke** (Zhang, September 2026; Wu et al., July 2026). For each repair, compare the issues of the new attempt with those of the best attempt, and record in `report.md` how many were fixed and how many were new. Across several lectures, this would show whether repairs cause net harm and whether the default of two repairs is right. It would touch `_process` in `pipeline.py` and `write_report` in `report.py`. Not built because it needs real runs to be useful.
10. **An estimate of listening effort** (Hoffner et al., 2026; Ramonda et al., 2026). The round-trip check (`roundtrip.py`, `--roundtrip`) compares what the voice was given with what speech recognition hears. It shows that the words were pronounced, but Ramonda and colleagues found that recognition errors are almost unrelated to how hard a text is for people, so it says nothing about how easy the audio is to follow. Hoffner and colleagues predicted listeners' ratings of effort and intelligibility for synthetic speech with a correlation above 0.88. A model of this kind could flag chunks that are hard to listen to. It would touch `roundtrip.py` and `cli.py`, as an optional package imported inside the function. Not built because I have not confirmed that either model is available as an installable package.
11. **Spoken emphasis on key terms** (Chen et al., 2026). Ask the voice to stress the name of each point and each newly defined term. Low priority: the only study found measured intention to engage, not learning, and only `OpenAITTS` in `tts.py` accepts instructions about delivery.
12. **Ideas listed part by part.** Built in November 2026 (section 2.16).

---

## 5. References

### Learning science

- Alpizar, D., Adesope, O. O., & Wong, R. M. (2020). A meta-analysis of signaling principle in multimedia learning environments. *Educational Technology Research and Development*, 68(5), 2095–2119. https://doi.org/10.1007/s11423-020-09748-7
- Ausubel, D. P. (1960). The use of advance organizers in the learning and retention of meaningful verbal material. *Journal of Educational Psychology*, 51(5), 267–272. https://doi.org/10.1037/h0046669
- Chaudron, C., & Richards, J. C. (1986). The effect of discourse markers on the comprehension of lectures. *Applied Linguistics*, 7(2), 113–127. https://doi.org/10.1093/applin/7.2.113 Finding read in a later study's summary: https://core.ac.uk/download/213421386.pdf
- Dunlosky, J., Rawson, K. A., Marsh, E. J., Nathan, M. J., & Willingham, D. T. (2013). Improving students' learning with effective learning techniques: Promising directions from cognitive and educational psychology. *Psychological Science in the Public Interest*, 14(1), 4–58. https://doi.org/10.1177/1529100612453266 Press summary: https://www.psychologicalscience.org/news/releases/which-study-strategies-make-the-grade.html
- Mahar (1992). *Thirty years after Ausubel: An updated meta-analysis of advance organizer research*. Doctoral dissertation, University of Illinois. https://www.ideals.illinois.edu/items/20405
- Mayer, R. E. (2020). *Multimedia Learning* (3rd ed.). Cambridge University Press. https://doi.org/10.1017/9781316941355 Principles summarised at https://ugc.futurelearn.com/uploads/files/7d/d6/7dd6188d-c343-4311-b064-ac98d2c95abc/Multimedia_Principles._R._E._Mayer__2020.pdf
- Richter, J., Scheiter, K., & Eitel, A. (2016). Signaling text-picture relations in multimedia learning: A comprehensive meta-analysis. *Educational Research Review*, 17, 19–36. https://doi.org/10.1016/j.edurev.2015.12.003
- Schneider, S., Beege, M., Nebel, S., & Rey, G. D. (2018). A meta-analysis of how signaling affects learning with media. *Educational Research Review*, 23, 1–24. https://doi.org/10.1016/j.edurev.2017.11.001 Figures as summarised at https://inspire.acu.edu.au/articles/signalling
- Xie, H., Wang, F., Hao, Y., Chen, J., An, J., Wang, Y., & Liu, H. (2017). The more total cognitive load is reduced by cues, the better retention and transfer of multimedia learning: A meta-analysis and two meta-regression analyses. *PLoS ONE*, 12(8), e0183884. https://doi.org/10.1371/journal.pone.0183884
- Yang, C., Luo, L., Vadillo, M. A., Yu, R., & Shanks, D. R. (2021). Testing (quizzing) boosts classroom learning: A systematic and meta-analytic review. *Psychological Bulletin*, 147(4), 399–435. https://doi.org/10.1037/bul0000309

### Learning science and speech, 2026

- Chen, Z., Wei, W., Cao, X., & Huang, Y. (2026). AI voice features and emotional self-dominance: Shaping student engagement in instructional video learning. *European Journal of Education*, 61(1), e70413. https://doi.org/10.1111/ejed.70413 [Tier not checked]
- Hjuler, M. J., Tran, T. K. N., & Sitbon, L. (2026). Listenability of synthetic speech: On the effect of linguistic registers in text-to-speech input. *Proceedings of Interspeech 2026*, 2911–2916. DOI given by ISCA: 10.21437/Interspeech.2026-894 (not yet resolving on 3 November 2026). https://www.isca-archive.org/interspeech_2026/hjuler26_interspeech.html [A\* conference]
- Hoffner, D. E., Schoon, H., Huber, R., Rennies, J., & Meyer, B. T. (2026). Deep learning-based predictions of perceived listening effort and intelligibility across enhanced, synthetic, natural, and binaural speech. *Proceedings of Interspeech 2026*, 734–738. DOI given by ISCA: 10.21437/Interspeech.2026-1891 (not yet resolving). https://www.isca-archive.org/interspeech_2026/hoffner26_interspeech.html [A\* conference]
- Joshi, U., Sarkar, A., Shukla, A., Shrimali, S., Bezbaruah, N. K., B, S. K., Baisil, S., Sheth, Z., Bhargava, P., & Shah, S. (2026). Ears for examinations: A multicentre quasi-experimental evaluation of AI-generated revision podcasts on learning outcomes and retention among medical students. *BMC Medical Education*, 26, 1532. https://doi.org/10.1186/s12909-026-10080-6 [Tier not checked]
- Ramonda, B., Sitbon, L., & Pinquier, J. (2026). Readability does not predict speech recognition errors: Contrasting human and machine perception. *Proceedings of Interspeech 2026*, 5483–5487. DOI given by ISCA: 10.21437/Interspeech.2026-2439 (not yet resolving). https://www.isca-archive.org/interspeech_2026/ramonda26_interspeech.html [A\* conference]

### Language-model systems, 2026

- Afoud, A., & Wu, J. J. (2026). VReAct: Verification as an architectural layer for LLM agents: A V-model design, and a pilot study of its deterministic core. arXiv:2609.31937. https://arxiv.org/abs/2609.31937 [Preprint]
- Anthropic (Grace, M., Hadfield, J., Olivares, R., & De Jonghe, J.) (9 January 2026). Demystifying evals for AI agents. https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents [Practitioner]
- Awuni, D. A., Achenie, L. E. K., Partey, B. T., Owusu, E. G., & Sowah, N.-N. D. (2026). Who judges matters: Measuring family-conditioned preference in LLM-as-judge panels. arXiv:2609.17857. https://arxiv.org/abs/2609.17857 [Preprint]
- Bagaria, A., Sundaram, S. S., Krishnan, G. S., & Ravindran, B. (2026). Judging LLM-as-a-judge: Concerning rubric artifacts in LLM-based automated text generation evaluation. *Proceedings of EMNLP 2026*, 59341–59352. https://doi.org/10.18653/2026.emnlp-main.2612 [A\* conference]
- Chae, S., Kim, M., Jung, D., Choi, S., & Jung, S. (2026). Self- and other-labels induce bidirectional bias in LLM judges. *Findings of the Association for Computational Linguistics: EMNLP 2026*, 33466–33482. https://doi.org/10.18653/2026.findings-emnlp.1571 [Findings]
- Fu, Y., Fang, R., Shao, J., Zheng, H., Zhu, Z., Luo, B., Wen, Z., & Lin, T. (2026). Do more agents help? Evaluating accuracy and cost in LLM multi-agent workflows. arXiv:2606.05670. https://arxiv.org/abs/2606.05670 [Preprint]
- Li, F., Wu, J., Fu, T., Li, D., Wan, H., Zhou, W., & Kan, M.-Y. (2026). What's left unsaid? Detecting and correcting misleading omissions in multimodal news previews. *Proceedings of ACL 2026 (Volume 1: Long Papers)*, 6480–6502. https://doi.org/10.18653/v1/2026.acl-long.293 [A\* conference]
- Mujahid, Z. M., Wright, D., & Augenstein, I. (2026). Stress testing factual consistency metrics for long-document summarization. *Proceedings of ACL 2026 (Volume 1: Long Papers)*, 31914–31933. https://doi.org/10.18653/v1/2026.acl-long.1472 [A\* conference]
- Pombal, J., Rei, R., & Martins, A. F. T. (2026). Self-preference bias in rubric-based evaluation of large language models. arXiv:2604.06996. https://arxiv.org/abs/2604.06996 [Preprint]
- Tang, Y., Zhang, Y., Feng, H., & Tung, A. K. H. (2026). Debating the unspoken: Role-anchored multi-agent reasoning for half-truth detection. *Proceedings of EMNLP 2026*, 32858–32872. https://doi.org/10.18653/2026.emnlp-main.1438 [A\* conference]
- Tran, D., & Kiela, D. (2026). Single-agent LLMs outperform multi-agent systems on multi-hop reasoning under equal thinking token budgets. arXiv:2604.02460. https://arxiv.org/abs/2604.02460 [Preprint]
- Wu, Y., Shen, S., Yang, R., Peng, H., & Hu, B. (2026). Verify, repair, repeat, or stop? Robust stopping for noisy verify-repair loops in LLM agents. arXiv:2607.17641. https://arxiv.org/abs/2607.17641 [Preprint, marked "under review"]
- Xu, J., Koesdwiady, A., Bei, S., Han, Y., Huang, B., Wang, D., Chen, Y., Wang, Z., Wang, P., Li, P., & Ding, Y. (2026). Rethinking the value of multi-agent workflow: A strong single agent baseline. arXiv:2601.12307. https://arxiv.org/abs/2601.12307 [Preprint]
- Yu, D., Zhang, T., Mou, Y., Zhang, Y., Ye, W., & Zhang, S. (2026). Mitigating rubric interference in LLM judges via on-policy self-distillation. arXiv:2608.14684. https://arxiv.org/abs/2608.14684 [Preprint]
- Zhang, T. (2026). When should LLMs trust their own revisions? A risk-aware study of intrinsic self-correction. arXiv:2609.35832. https://arxiv.org/abs/2609.35832 [Preprint]
- Zhang, X., Cui, Y., Wang, G., Qiu, W., Li, Z., Han, F., Huang, Y., Qiu, H., Zhu, B., & He, P. (2026). Verified multi-agent orchestration: A plan-execute-verify-replan framework for complex query resolution. ICLR 2026 Workshop on MALGAI. arXiv:2603.11445. https://arxiv.org/abs/2603.11445
- Zhang, X., Wang, G., Cui, Y., Li, Z., Qiu, W., Zhu, B., & He, P. (2026). Who verifies the verifier? Co-evolving inspectable graders with self-improving agents. NeurIPS 2026 Workshop "Who Verifies the Agents?". arXiv:2610.11464. https://arxiv.org/abs/2610.11464
- Zhou, Y., Xu, A., Zhou, Y., Singh, J., Gui, J., & Joty, S. (2026). Variation in verification: Understanding verification dynamics in large language models. *International Conference on Learning Representations (ICLR 2026)*. arXiv:2509.17995. https://arxiv.org/abs/2509.17995 [A\* conference]
- Loop engineering: "Loop engineering emerges as developers put AI coding agents on repeat", ADTmag, 1 July 2026. https://adtmag.com/articles/2026/07/01/loop-engineering-emerges-as-developers-put-ai-coding-agents-on-repeat.aspx

### Language-model systems, earlier

- Anthropic (2024). Building effective agents. https://www.anthropic.com/research/building-effective-agents
- Anthropic (2025). How we built our multi-agent research system. https://www.anthropic.com/engineering/multi-agent-research-system
- Cemri, M., Pan, M. Z., Yang, S., Agrawal, L. A., Chopra, B., Tiwari, R., Keutzer, K., Parameswaran, A., Klein, D., Ramchandran, K., Zaharia, M., Gonzalez, J. E., & Stoica, I. (2025). Why do multi-agent LLM systems fail? NeurIPS 2025, Datasets and Benchmarks Track. arXiv:2503.13657. https://arxiv.org/abs/2503.13657
- Dhuliawala, S., Komeili, M., Xu, J., Raileanu, R., Li, X., Celikyilmaz, A., & Weston, J. (2024). Chain-of-Verification reduces hallucination in large language models. *Findings of the Association for Computational Linguistics: ACL 2024*, 3563–3578. https://doi.org/10.18653/v1/2024.findings-acl.212 (preprint 2023: arXiv:2309.11495)
- Kamoi, R., Zhang, Y., Zhang, N., Han, J., & Zhang, R. (2024). When can LLMs actually correct their own mistakes? A critical survey of self-correction of LLMs. *Transactions of the Association for Computational Linguistics*, 12, 1417–1440. https://doi.org/10.1162/tacl_a_00713
- Kim, Y., Gu, K., Park, C., Park, C., Schmidgall, S., Heydari, A. A., Yan, Y., Zhang, Z., Zhuang, Y., Liu, Y., Malhotra, M., Liang, P. P., Park, H. W., Yang, Y., Xu, X., Du, Y., Patel, S., Althoff, T., McDuff, D., & Liu, X. (2025). Towards a science of scaling agent systems. arXiv:2512.08296. Google Research blog: https://research.google/blog/towards-a-science-of-scaling-agent-systems-when-and-why-agent-systems-work/
- Panickssery, A., Bowman, S. R., & Feng, S. (2024). LLM evaluators recognize and favor their own generations. *Advances in Neural Information Processing Systems 37* (NeurIPS 2024), 68772–68802. https://doi.org/10.52202/079017-2197

### Claude Code documentation

- Best practices for Claude Code. https://code.claude.com/docs/en/best-practices
- How Claude remembers your project. https://code.claude.com/docs/en/memory

---

## 6. How reliable these notes are

- **How the references were checked (3 November 2026).**
  - Journal articles: author names, year, journal, volume, issue, pages and DOI were checked against the Crossref record of each DOI.
  - ACL and EMNLP papers: checked on the ACL Anthology page of each paper, and each DOI was confirmed to resolve.
  - Interspeech 2026 papers: checked on the ISCA archive page of each paper. Their DOIs are printed there but did not yet resolve on 3 November 2026.
  - arXiv papers: titles, authors and the comment field (where a venue is announced) were checked through the arXiv interface.
- **Corrections made in this update.**
  - The journal details of the learning-science references, previously given from memory, were all confirmed; DOIs and missing issue numbers were added, and the full title of Dunlosky et al. (2013) was completed.
  - Dhuliawala et al. is now cited as published in *Findings of ACL 2024*, pages 3563–3578, not only as a 2023 preprint. The text of section 2.5 now gives 2024.
  - Kamoi et al. (2024) now has its volume (12), pages (1417–1440) and DOI.
  - Panickssery et al. (2024) now has its NeurIPS volume, pages and DOI.
  - Cemri et al. (2025) is now placed in the NeurIPS 2025 Datasets and Benchmarks Track, with the full author list. The published version analyses more than 1,600 annotated traces, while section 2.3 reports figures (over 200 runs, and the three percentages) from an earlier version. Those figures have not been checked against the published version.
  - Afoud and Wu (2026): the title now includes its current prefix, "VReAct".
  - Kim et al. (2025): the full author list was added.
  - The cross-reference in section 1.3 now points to teaching rule 16 of `prompts/rewrite.md` ("what the idea is for" before the definition) instead of rule 10.
- **Not verified.**
  - Mahar (1992): the dissertation's existence and title are confirmed by its entry in Hattie's *Visible Learning MetaX* database, but the repository page could not be opened, so the author's first name, the year and the "67%" figure remain unchecked.
  - The tier of *BMC Medical Education* and of the *European Journal of Education* in the Scimago ranking could not be confirmed, because the Scimago site refused access. Both are labelled "[Tier not checked]".
- **2026 papers:** the figures come from each paper's abstract or main page, read through a summarising tool, not from a full reading of the paper.
- **Learning science:**
  - The figures for Schneider et al., Alpizar et al. and Xie et al. come from an evidence summary page.
  - Chaudron and Richards' finding comes from a later study's summary of it.
- **Gaps in the 2026 evidence.** A search limited to 2026 found no Q1 journal article on audio-only learning, spoken signaling, segmenting in narration, speech rate, or retrieval practice in audio. The 2026 learning evidence above is therefore thin: one preliminary conference study (section 1.7), and two journal studies whose tier is unconfirmed and which have weak designs (sections 1.8 and 1.9). The older meta-analyses in sections 1.1 to 1.5 remain the main basis for the design of the audio.
- **Not verified:** nothing above shows that a model-rewritten lecture, heard as audio, teaches better than the original recording. That is the open question idea 7 would test.
