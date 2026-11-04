"""Command line: lecture2audio [build|text|series] INPUT [options].

INPUT is a transcript, a recording, or a folder that holds them (with slides and a glossary,
found by name and extension). Without a command, `build` runs: the script, the Obsidian vault
of course material, the audio, and the idea ledger `ideas.csv`."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.parse
from pathlib import Path

from . import ledger, stages
from .assemble import encode_final, encode_part_files, join_parts, repair_chunks, synth_blocks
from .factcheck import run_factcheck
from .course import CoursePipeline, course_report
from .course_render import write_vault
from .series import load_lecture, merge_termbases, series_report
from .llm import PRESETS, FakeLLM, LLMError, dry_run_handler, make_llm, research_loop
from .web import SEARCH_ENGINES, FetchError, make_search, pick_engine
from .pipeline import PipelineError, TextPipeline, chapter_titles, load_prompts
from .report import (write_glossary, write_index, write_questions, write_reference,
                     write_report, write_script)
from .segment import split_units
from .sources import (AUDIO_EXT, detect_language, discover_inputs, load_glossary, load_slides,
                      load_transcript, slugify)
from .transcribe import STTError, available as stt_available, transcribe_audio
from .tts import TTSError, make_tts

# What --redo X discards: the results of stage X and of every stage that depends on it,
# computed from the declared stage graph (stages.py). The idea ledger (ideas.csv) and anki.csv
# are rebuilt from the stage results on every run.
LATER = stages.redo_table()
COMMANDS = ("build", "text", "series")
# (rewrite, verifier) per provider. A local server has no default: its models are
# whatever the user has pulled.
DEFAULT_MODELS = {"anthropic": ("claude-sonnet-5-5", "claude-opus-5-5"),
                  "openai": ("gpt-6.1-sol", "gpt-6-astra"),
                  "deepseek": ("deepseek-flash", "deepseek-v4-pro")}
PROVIDERS = ["deepseek", "anthropic", "openai", "kimi", "openrouter", "compat", "local", "dry"]
NATIVE_SEARCH = ("anthropic", "openai")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lecture2audio",
        description="Turn a lecture transcript (or recording) into a clear spoken explanation "
                    "and an Obsidian vault of course material.",
        epilog="Without a command, 'build' runs: 'lecture2audio ./lecture_folder/' finds the "
               "recording or transcript, the slides and the glossary in the folder.")
    sub = parser.add_subparsers(dest="command", required=True)
    for command, text in (("build", "transcript or recording -> audio"),
                          ("text", "same, but stop before the voice: script and quality report only"),
                          ("series", "several lectures -> one course: shared terms and notation, "
                                     "course material in one vault (no audio)")):
        p = sub.add_parser(command, help=text, description=text)
        if command == "series":
            p.add_argument("inputs", nargs="+",
                           help="the lectures in course order (transcripts, recordings, or one "
                                "folder per lecture)")
            p.add_argument("--name", help="name of the course (default: first lecture's folder)")
        else:
            p.add_argument("input", help="transcript (.txt .md .srt .vtt), recording (.mp3 .m4a "
                                         ".wav .mp4 ...), or a folder holding them: the slides "
                                         "(.pdf .pptx, or slides.md) and a glossary file "
                                         "(glossary.txt) in it are used without flags")
            p.add_argument("--name", help="name of the lecture (default: the input file or folder "
                                          "name)")
        p.add_argument("--out", default="out", help="output folder (default: ./out)")
        p.add_argument("--slides", help="slides as PDF (needs pypdf), PowerPoint (needs "
                                        "python-pptx) or text, optional")
        p.add_argument("--glossary", help="text file, one course term per line, optional")
        p.add_argument("--language", choices=["en", "fr", "auto"], default="en")
        p.add_argument("--course", action=argparse.BooleanOptionalAction, default=True,
                       help="write the course material (default; --no-course skips it): a "
                            "Markdown vault for Obsidian with exact definitions, full "
                            "explanations, worked examples, check questions, figures, linked "
                            "terms and the audio players, made from the verified script")
        p.add_argument("--vault-dir", default=os.environ.get("L2A_VAULT_DIR"),
                       help="folder where the course vault is written (default: env "
                            "L2A_VAULT_DIR, else vault/ in the lecture's output folder). Point "
                            "it at your Obsidian vault: the notes go in a sub-folder named "
                            "after the lecture or the course")
        p.add_argument("--open-obsidian", action=argparse.BooleanOptionalAction, default=None,
                       help="open the lecture's index note in Obsidian when the run ends (macOS; "
                            "on by default when --vault-dir or L2A_VAULT_DIR is set)")
        p.add_argument("--export-anki", action="store_true",
                       help="also write anki.csv (Front, Back, Tags) for spaced repetition: "
                            "the check questions of the cards, tagged with the ideas of "
                            "ideas.csv they test")
        p.add_argument("--redo", choices=sorted(LATER),
                       help="discard saved results from this stage onwards and run it again")
        llm = p.add_argument_group("language models")
        llm.add_argument("--llm", choices=PROVIDERS, default="deepseek",
                         help="provider for the outline, the rewrite and the spoken form; "
                              "'openai' needs OPENAI_API_KEY; 'deepseek' (DEEPSEEK_API_KEY), "
                              "'kimi' (MOONSHOT_API_KEY) and 'openrouter' (OPENROUTER_API_KEY) "
                              "are hosted OpenAI-compatible APIs; 'compat' = any other one "
                              "(--base-url, L2A_COMPAT_API_KEY); 'local' = OpenAI-compatible "
                              "server (Ollama, LM Studio, llama.cpp, vLLM), no key needed; "
                              "'dry' = no model, pass-through, for smoke tests")
        llm.add_argument("--rewrite-model",
                         help="default: env L2A_REWRITE_MODEL, else claude-sonnet-5-5 "
                              "(anthropic), gpt-6.1-sol (openai) or deepseek-flash (deepseek); "
                              "required for the other providers")
        llm.add_argument("--base-url",
                         help="API base URL (default for local: http://localhost:11434/v1, "
                              "Ollama). With Ollama, raise num_ctx in a Modelfile: the OpenAI "
                              "route cannot set the context size")
        llm.add_argument("--verify-llm", choices=PROVIDERS,
                         help="provider for the verifier (default: same as --llm). A verifier from "
                              "another model family is less inclined to approve the rewrite")
        llm.add_argument("--verify-model",
                         help="default: env L2A_VERIFY_MODEL, else claude-opus-5-5 (anthropic), "
                              "gpt-6-astra (openai) or deepseek-v4-pro (deepseek); otherwise "
                              "the rewrite model")
        llm.add_argument("--verify-base-url", help="API base URL for --verify-llm openai or local")
        llm.add_argument("--max-part-words", type=int, default=1500,
                         help="longest part of the outline, in words (default 1500: long outputs "
                              "lose faithfulness towards their end, so parts are kept short)")
        llm.add_argument("--max-retries", type=int, default=2,
                         help="rewrites allowed after a failed verification (default 2)")
        llm.add_argument("--workers", type=int, default=3,
                         help="parts processed at the same time (default 3)")
        llm.add_argument("--no-audit", action="store_true",
                         help="skip the tests that plant a fault (an omission for the verifier, a "
                              "hole for the gap search, a fact for the idea listing, an emptied "
                              "card for the course judge) to check that they notice it")
        research = p.add_argument_group("web research (fills the gaps the lecture leaves)")
        research.add_argument("--research", choices=["auto", "native", "loop", "off"],
                              default="auto",
                              help="'native' = the provider's own web search (anthropic, "
                                   "openai); 'loop' = the program runs the searches and reads "
                                   "the pages for any model that can call tools (deepseek, kimi, "
                                   "openrouter, compat, local), using --search; 'auto' = native "
                                   "when the provider has it, else loop; 'off' = only list the "
                                   "gaps in report.md")
        research.add_argument("--search", choices=["auto", *SEARCH_ENGINES], default="auto",
                              help="search engine for --research loop: deepseek "
                                   "(DEEPSEEK_API_KEY), tavily (TAVILY_API_KEY), brave "
                                   "(BRAVE_API_KEY) or searxng (free, self-hosted, "
                                   "--searxng-url). 'auto' takes the first one available")
        research.add_argument("--searxng-url", help="address of a SearXNG instance with JSON output")
        research.add_argument("--factcheck", choices=["auto", "minicheck", "off"], default="auto",
                              help="check every lecture sentence of the script against the "
                                   "transcript units it cites with MiniCheck, a small local "
                                   "fact-checker (auto: when it is installed, "
                                   "uv pip install -e '.[factcheck]')")
        research.add_argument("--research-depth", type=int, default=30,
                              help="searches and page reads allowed per gap (default 30). Every "
                                   "gap is researched; raise this for deeper research")
        llm.add_argument("--prompts-dir",
                         help="folder with your own prompts (outline.md, rewrite.md, verify.md, "
                              "spoken.md, coverage.md, ideas.md, termbase.md, research.md, "
                              "terms.md, course.md, course_check.md, cold_read.md)")
        stt = p.add_argument_group("speech to text (recording input, round trip)")
        stt.add_argument("--stt", choices=["auto", "mlx", "faster", "parakeet"], default="auto",
                         help="mlx = Whisper large-v3 on Apple chips; faster = faster-whisper; "
                              "parakeet = NVIDIA Parakeet TDT (no hallucination over silence, "
                              "but no glossary prompt); auto = the first one installed, in "
                              "that order")
        stt.add_argument("--stt-model", help="model name for the chosen backend")
        stt.add_argument("--transcribe-only", action="store_true",
                         help="stop after writing transcript.txt, so you can correct it first")
        if command == "build":
            voice = p.add_argument_group("voice")
            voice.add_argument("--tts", choices=["auto", "ming", "qwen3", "openai", "say",
                                                 "command", "fake"],
                               default="auto",
                               help="auto = Ming-3.0-tts in English, Qwen3-TTS in French "
                                    "(default); ming = Ming-3.0-tts, local; qwen3 = Qwen3-TTS, "
                                    "local, ten languages; openai = OpenAI speech API; say = "
                                    "macOS voice; command = your own script; fake = tones")
            voice.add_argument("--tts-model", help="model of the local voice (a Hugging Face id)")
            voice.add_argument("--voice", help="ming: US_James (default), US_Emma, UK_George, "
                                               "UK_Charlotte, US_Stella, or a WAV file of a voice; "
                                               "openai or say: a voice name")
            voice.add_argument("--tts-command",
                               help="for --tts command: shell command using {text_file} {out} {language}")
            voice.add_argument("--format", choices=["m4a", "mp3", "wav"], default="m4a")
            voice.add_argument("--chunk-chars", type=int, default=600,
                               help="longest piece of text sent to the voice at once")
            voice.add_argument("--no-quiz", action="store_true",
                               help="leave the questions out of the audio (they stay in questions.md)")
            voice.add_argument("--quiz-pause", type=float, default=8.0,
                               help="seconds of silence after each question (default 8)")
            voice.add_argument("--no-loudnorm", action="store_true", help="skip loudness normalisation")
            voice.add_argument("--roundtrip", action=argparse.BooleanOptionalAction, default=None,
                               help="transcribe every chunk of the voice again and voice it anew "
                                    "when the words heard differ from the script (default: on "
                                    "when speech recognition is installed and the voice is a "
                                    "model)")
            voice.add_argument("--roundtrip-threshold", type=float, default=0.88)
            voice.add_argument("--voice-retries", type=int, default=2,
                               help="new takes of a chunk that fails the round trip (default 2)")
    return parser


def _model(explicit: str | None, env: str, provider: str, slot: int, fallback: str | None):
    if provider == "dry":
        return None
    defaults = DEFAULT_MODELS.get(provider)
    model = explicit or os.environ.get(env) or (defaults[slot] if defaults else fallback)
    if not model:
        flag = f"--{'rewrite' if slot == 0 else 'verify'}-model"
        hint = (" (the name the local server serves, for example from `ollama list`)"
                if provider == "local" else "")
        raise SystemExit(f"Give {flag}{hint}: there is no default model for '{provider}'.")
    return model


def _llms(args):
    verify_provider = args.verify_llm or args.llm
    rewrite_model = _model(args.rewrite_model, "L2A_REWRITE_MODEL", args.llm, 0, None)
    verify_model = _model(args.verify_model, "L2A_VERIFY_MODEL", verify_provider, 1,
                          rewrite_model if verify_provider == args.llm else None)
    if args.llm == "dry":
        rewrite_llm = FakeLLM(dry_run_handler)
    else:
        rewrite_llm = make_llm(args.llm, rewrite_model, args.base_url)
    if verify_provider == "dry":
        verify_llm = FakeLLM(dry_run_handler)
    else:
        verify_llm = make_llm(verify_provider, verify_model,
                              args.verify_base_url or (args.base_url if verify_provider == args.llm
                                                       else None))
    return rewrite_llm, verify_llm


def _researcher(args, llm):
    """(researcher function, None) or (None, why the gaps are not searched)."""
    mode = args.research
    if mode == "off":
        return None, "web research was turned off (--research off)"
    if mode == "auto":
        mode = "native" if args.llm in NATIVE_SEARCH or args.llm == "dry" else "loop"
    if mode == "native":
        if args.llm not in NATIVE_SEARCH and args.llm != "dry":
            raise SystemExit(f"--research native needs --llm anthropic or openai; use "
                             f"--research loop with --llm {args.llm}.")
        return (lambda system, user: llm.research(system, user, depth=args.research_depth)), None
    if args.llm not in ("openai", *PRESETS, "local"):
        raise SystemExit(f"--research loop needs a model that calls tools through an "
                         f"OpenAI-compatible API; with --llm {args.llm} use --research native.")
    engine = pick_engine(args.search, args.llm, args.searxng_url)
    if engine is None:
        return None, ("no search engine is set up for the research loop: set TAVILY_API_KEY, "
                      "BRAVE_API_KEY or DEEPSEEK_API_KEY, or give --searxng-url")
    try:
        search = make_search(engine, base_url=args.searxng_url)
    except FetchError as exc:
        raise SystemExit(str(exc)) from None
    return (lambda system, user: research_loop(llm, system, user, search,
                                               args.research_depth)), None


def _transcript(args, work: Path, glossary: str, log) -> str:
    source = Path(args.input)
    target = work / "transcript.txt"
    if source.suffix.lower() in AUDIO_EXT:
        if args.redo == "transcribe" and target.exists():
            target.unlink()
        if not target.exists():
            minutes = _minutes(source)
            log(f"[transcribe] {source.name}" + (f", {minutes:.0f} min of audio. Each recognised "
                                                "sentence is printed as it comes; a long "
                                                "recording takes several minutes" if minutes else ""))
            terms = ", ".join(line.split(":")[0].strip() for line in glossary.splitlines())[:800]
            language = None if args.language == "auto" else args.language
            removed: list[dict] = []
            target.write_text(transcribe_audio(source, args.stt, language, terms or None,
                                               args.stt_model, progress=True, removed=removed),
                              encoding="utf-8")
            (work / "transcript_notes.json").write_text(
                json.dumps({"removed": removed}, ensure_ascii=False, indent=2), encoding="utf-8")
            if removed:
                log(f"[transcribe] {len(removed)} segment(s) dropped as hallucinations or loops "
                    "(listed in report.md)")
            log(f"[transcribe] saved {target}. Correct misheard technical terms there; "
                "running the same command again reuses your corrected file.")
        return target.read_text(encoding="utf-8")
    text = load_transcript(source)
    target.write_text(text, encoding="utf-8")
    return text


def _minutes(path: Path) -> float | None:
    """Length of a recording in minutes (ffprobe), or None."""
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                              "-of", "csv=p=0", str(path)], capture_output=True, text=True,
                             timeout=30)
        return float(out.stdout.strip()) / 60
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _discard(work: Path, names: list[str]) -> None:
    for name in names:
        path = work / name
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists():
            path.unlink()


def _resolve_folder(args, log) -> None:
    """A folder as INPUT: take its recording or transcript, and its slides and glossary when
    no flag names them. The lecture is named after the folder."""
    folder = Path(args.input)
    if not folder.is_dir():
        return
    found = discover_inputs(folder)
    if not found["input"]:
        raise SystemExit(f"No transcript (.txt .srt .vtt .md) or recording (.m4a .mp3 .wav "
                         f".mp4 ...) found in {folder}")
    args.name = args.name or slugify(folder.resolve().name)
    args.input = str(found["input"])
    if not args.slides and found["slides"]:
        args.slides = str(found["slides"])
    if not args.glossary and found["glossary"]:
        args.glossary = str(found["glossary"])
    log(f"[inputs] {folder}: lecture {Path(args.input).name}"
        + (f", slides {Path(args.slides).name}" if args.slides else "")
        + (f", glossary {Path(args.glossary).name}" if args.glossary else ""))
    if found["skipped"]:
        log("[inputs] not used: " + ", ".join(f.name for f in found["skipped"])
            + " (give --slides or --glossary, or the file itself as INPUT, to choose another)")


def _asr_notes(work: Path) -> list[dict]:
    notes = work / "transcript_notes.json"
    if not notes.exists():
        return []
    return json.loads(notes.read_text(encoding="utf-8")).get("removed", [])


def _open_obsidian(args, entry: str | None, log) -> None:
    """Open a note in Obsidian through its URI scheme. The note must lie inside a vault that
    Obsidian knows, which is why this is on by default only with --vault-dir."""
    want = args.open_obsidian if args.open_obsidian is not None else bool(args.vault_dir)
    if not want or not entry:
        return
    uri = "obsidian://open?path=" + urllib.parse.quote(str(Path(entry).resolve()), safe="")
    if sys.platform == "darwin" and shutil.which("open"):
        if subprocess.run(["open", uri], check=False).returncode == 0:
            log(f"[obsidian] opened {entry}")
            return
    log(f"[obsidian] open this link to read the notes: {uri}")


def run(args, series: dict | None = None) -> int:
    """One lecture. `series` (see run_series) stops the run after the termbase, supplies the
    notation of the earlier lectures, or keeps the run from writing the vault."""
    series = series or {}

    def log(message: str) -> None:
        print(message, flush=True)

    source = Path(args.input)
    if not source.exists():
        raise SystemExit(f"Input not found: {source}")
    _resolve_folder(args, log)
    source = Path(args.input)
    name = args.name or slugify(source.stem)
    work = Path(args.out) / name
    work.mkdir(parents=True, exist_ok=True)
    glossary = load_glossary(args.glossary)
    slides = load_slides(args.slides)
    if args.redo:
        _discard(work, LATER[args.redo])
    start = stages.present(work)

    text = _transcript(args, work, glossary, log)
    if args.transcribe_only:
        return 0
    language = detect_language(text) if args.language == "auto" else args.language
    units = split_units(text)
    if not units:
        raise SystemExit("The transcript is empty.")
    fingerprint = hashlib.sha1(json.dumps([args.max_part_words, units, slides, glossary])
                               .encode()).hexdigest()
    marker = work / "fingerprint.txt"
    if marker.exists() and marker.read_text() != fingerprint:
        log("[cache] the transcript, slides, glossary or --max-part-words changed: earlier "
            "results discarded")
        _discard(work, LATER["outline"])
    marker.write_text(fingerprint)
    # stages whose result was on disk when the run began and survived every discard
    before = start & stages.present(work)
    log(f"{len(text.split())} words, {len(units)} units, language: {language}")

    rewrite_llm, verify_llm = _llms(args)
    researcher, research_note = _researcher(args, rewrite_llm)
    pipeline = TextPipeline(work, language, rewrite_llm, verify_llm,
                            load_prompts(language, args.prompts_dir), glossary, slides,
                            args.max_retries, log, args.max_part_words, args.workers,
                            researcher)
    outline = pipeline.run_outline(units)
    log(f"[outline] {len(outline.parts)} part(s), "
        f"{sum(len(p.children) for p in outline.parts)} point(s)")
    coverage = pipeline.run_coverage(outline)
    pipeline.run_ideas(outline)  # fills coverage["transcript_ideas"], part by part
    # the idea ledger: every atomic idea of the transcript, checked off by the later stages as
    # each part finishes; ideas.csv is rewritten after every part
    book = ledger.Ledger(work / "ideas.csv", outline, coverage)
    ideas = book.rows
    log(f"[ideas] {len(ideas)} atomic idea(s) in {book.path}")
    ideas_audit = None if args.no_audit else pipeline.audit_ideas(outline)
    terms = pipeline.run_termbase(outline)
    if series.get("stop_after") == "termbase":
        return 0
    if research_note and coverage["gaps"]:
        log(f"[research] skipped: {research_note}")
    supplements = pipeline.run_research(outline)
    log(f"[gaps] {len(coverage['gaps'])} gap(s), {len(terms)} term(s), "
        f"{len(supplements)} checked fact(s) from outside the lecture")
    gaps = {"coverage": coverage, "terms": terms, "research": pipeline.research,
            "supplements": supplements, "note": research_note,
            "audit": None if args.no_audit else pipeline.audit_coverage(outline)}
    records = pipeline.run_parts(outline, on_done=book.script_done)
    factcheck = None
    if args.factcheck != "off":
        factcheck = run_factcheck(work, outline, records, coverage.get("transcript_ideas", []),
                                  log)
        if factcheck is None and args.factcheck == "minicheck":
            raise SystemExit("--factcheck minicheck needs MiniCheck: uv pip install -e "
                             "'.[factcheck]'")
    audit = None if args.no_audit else pipeline.audit_verifier(outline, records)
    notes = pipeline.global_notes(outline, records)
    spoken = pipeline.run_spoken(outline, records)
    course = course_stats = None
    vault = Path(args.vault_dir) / name if args.vault_dir else work / "vault"

    def write_course(audio: dict | None = None) -> dict:
        stats = write_vault(vault, [{"name": name, "outline": outline, "course": course,
                                     "records": records, "terms": terms, "audio": audio}],
                            language, outline.title)
        log(f"[course] {stats['cards']} card(s), {stats['notes']} note(s): {vault}")
        return stats

    if args.course:
        course = CoursePipeline(pipeline, series.get("notation")).run(
            outline, records, on_done=book.cards_done, audit=not args.no_audit)
        if series.get("skip_vault"):
            course_stats = {"cards": sum(len(p["cards"]) for p in course["parts"]),
                            "notes": 0, "diagrams": sum(bool(c["diagram"]) for p in course["parts"]
                                                        for c in p["cards"])}
            log(f"[course] {course_stats['cards']} card(s)")
        else:
            course_stats = write_course()
    unverified = ledger.open_rows(ideas, bool(course))
    if unverified:
        log(f"[ideas] {len(unverified)} of {len(ideas)} idea(s) not "
            f"{'verified in the cards' if course else 'found in the script'}: "
            + ", ".join(r["idea_id"] for r in unverified[:20])
            + (" ..." if len(unverified) > 20 else "") + " (see report.md and ideas.csv)")
    if args.export_anki:
        count = ledger.write_anki(work / "anki.csv", name, ideas, course, records, language)
        log(f"[anki] {count} note(s) in {work / 'anki.csv'} (Anki: File > Import)")
    usage = {rewrite_llm.name: rewrite_llm.usage}
    if verify_llm is not rewrite_llm:
        usage[verify_llm.name + " (verifier)"] = verify_llm.usage

    def reports(chapters=None, roundtrip=None):
        write_script(work / "script.md", name, outline, records, language)
        write_index(work / "index.md", name, outline, chapters)
        write_questions(work / "questions.md", name, outline, records)
        write_reference(work / "reference.md", name, outline, records)
        write_glossary(work / "glossary.md", name, outline, terms)
        write_report(work / "report.md", name, outline, records, spoken, notes, audit, usage,
                     roundtrip, gaps, {"rows": ideas, "course": bool(course),
                                       "empty_parts": ledger.parts_without_ideas(outline, ideas),
                                       "audit": ideas_audit,
                                       "no_content": pipeline.idea_notes["no_content"],
                                       "unaccounted": pipeline.idea_notes["unaccounted"],
                                       "unledgered": [(r["part"], i) for r in records
                                                      for i in r["issues"]
                                                      if i["type"] == "unledgered"]},
                     factcheck, _asr_notes(work))
        override = {}
        if source.suffix.lower() not in AUDIO_EXT:
            override["transcribe"] = "read from the input file"
        if course_stats and args.vault_dir:
            override["vault"] = f"written to {vault}"
        # the course section and the stage section go before the list of noise, which stays last
        section = (course_report(course, course_stats, language) if course else "") + "\n".join(
            [""] + stages.report_lines(stages.statuses(work, before, override)))
        report = (work / "report.md").read_text(encoding="utf-8")
        cut = report.find("\n## Removed as noise")
        report = report + section if cut < 0 else report[:cut] + section + report[cut:]
        (work / "report.md").write_text(report, encoding="utf-8")

    reports()
    flagged = [r["part"] for r in records if r["flagged"]]
    if flagged:
        log(f"[check] parts with open problems after the retries: {', '.join(flagged)} "
            "(see report.md)")
    if audit and audit.get("caught") is False:
        log("[check] the verifier missed a planted omission (see report.md)")
    if ideas_audit and ideas_audit.get("caught") is False:
        log("[check] the idea listing missed a planted fact (see report.md)")
    if course and (course.get("audit") or {}).get("caught") is False:
        log("[check] the course judge passed an emptied card (see report.md)")
    if args.command == "text":
        log(f"Done. Read {work / 'script.md'} and {work / 'report.md'}.")
        if course_stats and not series:
            _open_obsidian(args, course_stats.get("entry"), log)
        return 0

    backend = make_tts(args.tts, language, args.voice, args.tts_command, args.tts_model)
    voiced = synth_blocks(backend, [s["blocks"] for s in spoken], work / "audio", args.chunk_chars,
                          not args.no_quiz, args.quiz_pause, log)
    roundtrip = None
    check = args.roundtrip if args.roundtrip is not None else (
        backend.name not in ("fake", "command") and stt_available())
    if check:
        log("[roundtrip] transcribing every chunk again; a chunk heard wrong is voiced anew")
        results = repair_chunks(
            backend, voiced,
            lambda p: transcribe_audio(p, args.stt, language, None, args.stt_model),
            args.roundtrip_threshold, args.voice_retries, work / "audio" / "roundtrip.json", log)
        roundtrip = [r for r in results if r["score"] < args.roundtrip_threshold]
        repaired = sum(1 for r in results if r["tries"] and r["score"] > r["first"])
        log(f"[roundtrip] {len(results)} chunk(s) checked, {repaired} improved by a new take, "
            f"{len(roundtrip)} still below {args.roundtrip_threshold}")
    joined, chapters = join_parts(voiced, work / "audio")
    titles = chapter_titles(outline, language, not args.no_quiz)
    output = work / f"{name}.{args.format}"
    encode_final(joined, output, [titles[c] for c, _ in chapters], [s for _, s in chapters],
                 loudness=not args.no_loudnorm)
    part_files = {}
    if course_stats and not series.get("skip_vault"):
        # one recording per part, for the player at the top of each part note
        part_files = encode_part_files(voiced, [r["part"] for r in records], work / "audio",
                                       work / "parts", name, args.format, titles,
                                       loudness=not args.no_loudnorm)
        course_stats = write_course({"full": output, "parts": part_files})
    reports(chapters, roundtrip)
    log(f"Done. Audio: {output}")
    if course_stats:
        _open_obsidian(args, course_stats.get("entry"), log)
    return 0


def run_series(args) -> int:
    """Several lectures as one course. Pass 1 prepares every lecture up to its termbase; the
    termbases are merged (one definition per term, the first lecture that defines a term owns
    it); pass 2 finishes the lectures in order, each starting from the notation of the earlier
    ones; the vault is written once at the end, with shared term notes."""
    sources = [Path(p) for p in args.inputs]
    for path in sources:
        if not path.exists():
            raise SystemExit(f"Input not found: {path}")
    course_name = args.name or slugify(sources[0].resolve().parent.name or "course")
    if args.open_obsidian is None:
        args.open_obsidian = bool(args.vault_dir)
    names, seen = [], set()
    for path in sources:
        base = slugify(path.stem)
        name, k = base, 2
        while name in seen:
            name, k = f"{base}-{k}", k + 1
        seen.add(name)
        names.append(name)
    root = Path(args.out) / course_name
    root.mkdir(parents=True, exist_ok=True)

    def lecture_args(number: int, redo):
        lecture = copy.copy(args)
        lecture.input, lecture.name, lecture.out = str(sources[number]), names[number], str(root)
        lecture.command, lecture.course, lecture.redo = "text", True, redo
        return lecture

    for number in range(len(sources)):
        print(f"[series] lecture {number + 1}/{len(sources)}: preparing {names[number]}", flush=True)
        run(lecture_args(number, args.redo), {"stop_after": "termbase"})
    conflicts = merge_termbases([root / n for n in names], names)
    print(f"[series] terms merged: {len(conflicts)} conflict(s)", flush=True)
    registry: list[dict] = []
    for number in range(len(sources)):
        print(f"[series] lecture {number + 1}/{len(sources)}: writing {names[number]}", flush=True)
        run(lecture_args(number, None), {"notation": registry, "skip_vault": True})
        notation_file = root / names[number] / "course" / "notation.json"
        if notation_file.exists():
            known = {n["symbol"] for n in registry}
            registry += [n for n in json.loads(notation_file.read_text(encoding="utf-8"))
                         if n["symbol"] not in known]
    lectures = [load_lecture(root / n, n) for n in names]
    first = (root / names[0] / "transcript.txt").read_text(encoding="utf-8")
    language = detect_language(first) if args.language == "auto" else args.language
    vault = Path(args.vault_dir) / course_name if args.vault_dir else root / "vault"
    stats = write_vault(vault, lectures, language, course_name)
    series_report(root / "series.md", course_name, lectures, conflicts, stats)
    (root / "series.json").write_text(json.dumps(
        {"lectures": names, "notation": registry, "conflicts": conflicts}, ensure_ascii=False,
        indent=2), encoding="utf-8")
    print(f"Done. Vault: {vault}. Read {root / 'series.md'}.", flush=True)
    _open_obsidian(args, stats.get("entry"), lambda m: print(m, flush=True))
    return 0


def main(argv=None) -> None:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv and argv[0] not in (*COMMANDS, "-h", "--help"):
        argv = ["build", *argv]  # zero friction: `lecture2audio ./lecture_folder/`
    args = build_parser().parse_args(argv)
    try:
        sys.exit(run_series(args) if args.command == "series" else run(args))
    except KeyboardInterrupt:
        print("Interrupted. Run the same command again to continue where it stopped.",
              file=sys.stderr)
        sys.exit(130)
    except (LLMError, PipelineError, TTSError, STTError, RuntimeError,
            subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
