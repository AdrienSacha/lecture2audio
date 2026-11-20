import os
os.environ["L2A_KEYS_FILE"] = "off"  # never read or write the real key file
import contextlib
import csv
import io
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
os.environ["L2A_PDF"] = "0"  # the LaTeX build has its own tests (test_recitation)

from lecture2audio.cli import main
from lecture2audio.llm import FakeLLM, _between, dry_run_handler
from lecture2audio.outline import parse_outline
from lecture2audio.pipeline import (NumberIndex, TextPipeline, list_sentence, load_prompts,
                                    numbers_lost, part_blocks, repair_plan, splice_repair,
                                    unsupported_numbers, validate_verify)

UNITS = ["The window holds 64 packets.", "um ok let me drink some water",
         "A timeout is 5 seconds.", "The sender waits for the timeout.",
         "Then it sends the packet again.", "The receiver acknowledges each packet.",
         "An acknowledgement carries a number."]
OUTLINE = {"title": "Flow control", "parts": [
    {"title": "Sending", "units": [], "children": [
        {"title": "the window", "units": [[1, 1]], "children": []},
        {"title": "the timeout", "units": [[3, 5]], "children": []}]},
    {"title": "Acknowledgements", "units": [[6, 7]], "children": []}],
    "noise": [[2, 2]], "terms": [{"term": "window", "part": 1}]}


def silent(_message):
    pass


def good_rewrite(user, extra=None):
    """A correct rewrite of whatever part is asked (one lecture sentence per unit)."""
    data = json.loads(dry_run_handler("rewrite", user))
    if "Sending" in _between(user, "TRANSCRIPT OF THIS PART").splitlines()[0]:
        data["terms_defined"] = ["window"]
    if extra:
        data["sections"][-1]["sentences"].append({"origin": "lecture", "text": extra})
    return json.dumps(data)


def counting_verifier(system, user):
    """Marks a node incomplete when its section has fewer lecture sentences than it has units."""
    units, current = {}, None
    for line in _between(user, "TRANSCRIPT OF THIS PART").splitlines():
        heading = re.match(r"^#{3,5} (\d+(?:\.\d+)*) ", line)
        if heading:
            current = heading.group(1)
            units[current] = 0
        elif line.startswith("#"):
            current = None
        elif current and line.startswith("["):
            units[current] += 1
    said, current = {}, None
    for line in _between(user, "SCRIPT").splitlines():
        heading = re.match(r"^#{3,5} (\d+(?:\.\d+)*) ", line)
        if heading:
            current = heading.group(1)
            said[current] = 0
        elif current and "[lecture]" in line:
            said[current] += 1
    nodes = json.loads(_between(user, "CHECKLIST (JSON)"))["nodes"]
    points = [{"node": n["node"], "detail": "",
               "status": "complete" if said.get(n["node"], 0) >= units.get(n["node"], 0)
               else "incomplete"} for n in nodes]
    ideas = json.loads(_between(user, "IDEAS (JSON)")) if "## IDEAS (JSON)\n" in user else []
    return json.dumps({"points": points, "issues": [],
                       "ideas": [{"idea": i["id"], "status": "taught", "detail": ""}
                                 for i in ideas]})


LEDGER = {"slide_ideas": [], "gaps": [], "transcript_ideas": [
    {"id": "T1", "kind": "claim", "idea": "the window holds 64 packets", "nodes": ["1.1"]},
    {"id": "T2", "kind": "condition", "idea": "a timeout is 5 seconds", "nodes": ["1.2"]},
    {"id": "T3", "kind": "claim", "idea": "each packet is acknowledged", "nodes": ["2"]}]}


class Harness:
    """Routes the four prompts to four small handlers."""

    def __init__(self, rewrite=None, verify=counting_verifier, spoken=None, outline=None,
                 ideas=None):
        self.handlers = {
            "outline": outline or (lambda s, u: json.dumps(OUTLINE)),
            "ideas": ideas or dry_run_handler,
            "verify": verify,
            "spoken": spoken or (lambda s, u: json.dumps({"blocks": json.loads(u)["blocks"]})),
            "rewrite": rewrite or (lambda s, u: good_rewrite(u))}
        self.llm = FakeLLM(self.handle)

    @staticmethod
    def kind(system):
        if "You build the outline" in system:
            return "outline"
        if "strict reviewer" in system:
            return "verify"
        if "exact words a voice should say" in system:
            return "spoken"
        if "You list the atomic ideas" in system:
            return "ideas"
        return "rewrite"

    def handle(self, system, user):
        return self.handlers[self.kind(system)](system, user)

    def pipeline(self, work, max_retries=2, workers=1, language="en", coverage=None):
        pipeline = TextPipeline(pathlib.Path(work), language, self.llm, self.llm,
                                load_prompts(language), max_retries=max_retries, log=silent,
                                max_part_words=1000, workers=workers)
        if coverage:
            pipeline.coverage = coverage
        return pipeline

    def prompts(self, kind):
        return [u for s, u in self.llm.calls if self.kind(s) == kind]


def run_text(harness, **kwargs):
    with tempfile.TemporaryDirectory() as tmp:
        pipeline = harness.pipeline(tmp, **kwargs)
        outline = pipeline.run_outline(UNITS)
        return outline, pipeline.run_parts(outline), pipeline


class OutlineStage(unittest.TestCase):
    def test_rejected_outline_is_asked_again_with_the_reasons(self):
        bad = dict(OUTLINE, noise=[])
        answers = iter([json.dumps(bad), json.dumps(OUTLINE)])
        harness = Harness(outline=lambda s, u: next(answers))
        outline, _, _ = run_text(harness)
        self.assertEqual(len(outline.parts), 2)
        second = harness.prompts("outline")[1]
        self.assertIn("PREVIOUS ANSWER REJECTED", second)
        self.assertIn("units 2 are in no node", second)

    def test_outline_falls_back_to_parts_by_size(self):
        harness = Harness(outline=lambda s, u: "no outline today")
        outline, records, _ = run_text(harness)
        self.assertIsNotNone(outline.fallback)
        self.assertEqual([u for p in outline.parts for u in p.all_units()], list(range(1, 8)))
        self.assertTrue(records)


class Gates(unittest.TestCase):
    def test_wrong_structure_is_refused_before_the_verifier(self):
        def rewrite(system, user):
            data = json.loads(good_rewrite(user))
            if len(harness.prompts("rewrite")) == 1 and len(data["sections"]) > 1:
                data["sections"].reverse()
            return json.dumps(data)

        harness = Harness(rewrite)
        _, records, _ = run_text(harness)
        self.assertIn("must have exactly these nodes", harness.prompts("rewrite")[1])
        self.assertEqual([s["node"] for s in records[0]["sections"]], ["1", "1.1", "1.2"])

    def test_verifier_must_give_one_status_per_node(self):
        answers = iter([json.dumps({"points": [], "issues": []})])

        def verify(system, user):
            return next(answers, None) or counting_verifier(system, user)

        harness = Harness(verify=verify)
        run_text(harness)
        self.assertIn('"points" must have exactly one entry', harness.prompts("verify")[1])

    def test_verifier_must_check_off_every_idea_of_the_part(self):
        points = [{"node": "1", "status": "complete"}]
        data = {"points": points, "issues": []}
        self.assertEqual(validate_verify(data, ["1"]), [])
        self.assertTrue(validate_verify(data, ["1"], ["T1"]))
        data["ideas"] = [{"idea": "T1", "status": "maybe"}]
        self.assertTrue(validate_verify(data, ["1"], ["T1"]))
        data["ideas"] = [{"idea": "T1", "status": "taught"}]
        self.assertEqual(validate_verify(data, ["1"], ["T1"]), [])

    def test_the_verifier_gets_the_ideas_of_its_part_and_records_them(self):
        harness = Harness()
        _, records, _ = run_text(harness, coverage=LEDGER)
        first, second = harness.prompts("verify")[:2]
        self.assertIn("## IDEAS (JSON)", first)
        self.assertIn("T1", first)
        self.assertNotIn("T3", first)
        self.assertIn("T3", second)
        self.assertEqual([i["idea"] for i in records[0]["ideas"]], ["T1", "T2"])
        self.assertEqual(records[1]["ideas"][0]["status"], "taught")

    def test_an_idea_the_verifier_calls_missing_triggers_a_rewrite(self):
        def verify(system, user):
            data = json.loads(counting_verifier(system, user))
            if len(harness.prompts("verify")) == 1:
                data["ideas"][0].update(status="missing", detail="the 64 is gone")
            return json.dumps(data)

        harness = Harness(verify=verify)
        _, records, _ = run_text(harness, coverage=LEDGER)
        self.assertEqual(records[0]["attempts"], 2)
        self.assertIn("idea_missing", harness.prompts("rewrite")[1])
        self.assertIn("T1", harness.prompts("rewrite")[1])

    def test_number_check(self):
        script = [{"origin": "lecture", "text": "The window is 64 KB and the timeout is 5 seconds."},
                  {"origin": "lecture", "text": "The retry count is 9999."},
                  {"origin": "added", "text": "For example 12 plus 30 is 42."}]
        issues = unsupported_numbers(script, "window 64 kilobytes timeout five seconds", "", "en")
        self.assertEqual([(i["sentence"], i["type"]) for i in issues], [(2, "unsupported")])


class Loop(unittest.TestCase):
    def test_invented_number_triggers_a_rewrite_with_feedback(self):
        def rewrite(system, user):
            return good_rewrite(user, None if "## FEEDBACK" in user else "It retries 9999 times.")

        harness = Harness(rewrite)
        _, records, _ = run_text(harness)
        self.assertEqual(records[0]["attempts"], 2)
        self.assertFalse(records[0]["flagged"])
        repairs = [u for u in harness.prompts("rewrite") if "## FEEDBACK" in u]
        self.assertTrue(repairs and all("9999" in u for u in repairs))

    def test_dropped_content_is_reported_by_node(self):
        def rewrite(system, user):
            data = json.loads(good_rewrite(user))
            if data["sections"][0]["node"] == "1":
                data["sections"][2]["sentences"] = data["sections"][2]["sentences"][:1]
            return json.dumps(data)

        harness = Harness(rewrite)
        _, records, _ = run_text(harness, max_retries=1)
        first = records[0]
        self.assertTrue(first["flagged"])
        self.assertEqual([(i["type"], i["node"]) for i in first["issues"]], [("missing", "1.2")])

    def test_the_best_attempt_is_kept_when_a_repair_makes_things_worse(self):
        count = {"n": 0}

        def rewrite(system, user):
            if "Acknowledgements" in _between(user, "TRANSCRIPT OF THIS PART").splitlines()[0]:
                return good_rewrite(user)
            count["n"] += 1
            return good_rewrite(user, f"Attempt {['one', 'two', 'three'][count['n'] - 1]} here.")

        def verify(system, user):
            points = json.loads(counting_verifier(system, user))
            found = re.search(r"Attempt (\w+) here", user)
            bad = {"one": 1, "two": 3, "three": 2}.get(found.group(1), 0) if found else 0
            # no sentence or unit: the problems cannot be placed, so each repair is a full
            # rewrite, which can make the script worse
            points["issues"] = [{"type": "wrong", "sentence": None, "detail": f"problem {k}"}
                                for k in range(bad)]
            return json.dumps(points)

        harness = Harness(rewrite, verify)
        _, records, _ = run_text(harness)
        first = records[0]
        self.assertEqual((first["attempts"], first["blocking"]), (3, 1))
        last_text = " ".join(s["text"] for s in first["sections"][-1]["sentences"])
        self.assertIn("Attempt one here", last_text)
        third_prompt = [u for u in harness.prompts("rewrite") if "Sending" in u][2]
        self.assertIn("Attempt one here", third_prompt)
        self.assertNotIn("Attempt two here", third_prompt)

    def test_soft_issues_do_not_block(self):
        def verify(system, user):
            data = json.loads(counting_verifier(system, user))
            data["issues"] = [{"type": "long", "sentence": 1, "detail": "Too long."}]
            return json.dumps(data)

        harness = Harness(lambda s, u: json.dumps(dict(json.loads(good_rewrite(u)),
                                                       terms_defined=[])), verify)
        _, records, _ = run_text(harness)
        self.assertEqual((records[0]["attempts"], records[0]["flagged"]), (1, False))
        self.assertIn("plan", [i["type"] for i in records[0]["issues"]])

    def test_parallel_parts_give_the_same_records_in_order(self):
        _, one, _ = run_text(Harness(), workers=1)
        _, three, _ = run_text(Harness(), workers=3)
        self.assertEqual(one, three)

    def test_finished_parts_are_not_redone(self):
        harness = Harness()
        with tempfile.TemporaryDirectory() as tmp:
            pipeline = harness.pipeline(tmp)
            pipeline.run_parts(pipeline.run_outline(UNITS))
            calls = len(harness.llm.calls)
            again = harness.pipeline(tmp)
            again.run_parts(again.run_outline(UNITS))
        self.assertEqual(len(harness.llm.calls), calls)


class BoundedRepair(unittest.TestCase):
    """A repair rewrites only the sections at fault; code keeps the rest word for word."""

    @staticmethod
    def part_one():
        outline, errors = parse_outline(OUTLINE, UNITS, 1000)
        assert not errors, errors
        return outline.parts[0]

    @staticmethod
    def record():
        return {"preview": {"intro": "p", "items": []},
                "sections": [{"node": "1", "sentences": [{"origin": "added", "text": "a"}]},
                             {"node": "1.1", "sentences": [{"origin": "lecture", "text": "b"}]},
                             {"node": "1.2", "sentences": [{"origin": "lecture", "text": "c"},
                                                           {"origin": "lecture", "text": "d"}]}],
                "recap": ["r"], "questions": [{"question": "q", "answer": "a"}],
                "terms_defined": ["window"], "reference": [{"label": "Code 1", "content": "x"}]}

    def test_each_issue_is_placed_by_node_sentence_or_unit(self):
        part, record = self.part_one(), self.record()
        issues = [{"type": "unsupported", "node": None, "sentence": 4, "unit": None, "detail": ""},
                  {"type": "wrong", "node": None, "sentence": None, "unit": 1, "detail": ""},
                  {"type": "long", "node": None, "sentence": 1, "unit": None, "detail": ""}]
        scope, shown = repair_plan(part, record, issues)
        self.assertEqual(scope, ["1.1", "1.2"])  # in section order; the soft issue in 1 is not
        self.assertEqual([i["type"] for i in shown], ["unsupported", "wrong"])
        recap = [{"type": "recap", "node": None, "sentence": None, "unit": None, "detail": ""}]
        self.assertEqual(repair_plan(part, record, recap)[0], ["recap"])

    def test_an_issue_that_cannot_be_placed_or_faults_everywhere_rewrites_the_whole_part(self):
        part, record = self.part_one(), self.record()
        general = [{"type": "undefined_term", "node": None, "sentence": None, "unit": None,
                    "detail": ""}]
        self.assertEqual(repair_plan(part, record, general), (None, general))
        everywhere = [{"type": "missing", "node": n, "sentence": None, "unit": None, "detail": ""}
                      for n in ("1", "1.1", "1.2")]
        self.assertIsNone(repair_plan(part, record, everywhere)[0])
        noise = [{"type": "wrong", "node": None, "sentence": None, "unit": 2, "detail": ""}]
        self.assertIsNone(repair_plan(part, record, noise)[0])  # unit 2 is noise

    def test_the_splice_takes_only_the_scope_from_the_repair(self):
        best, new = self.record(), self.record()
        for section in new["sections"]:
            section["sentences"] = [{"origin": "lecture", "text": "NEW " + section["node"]}]
        new.update(recap=["new recap"], terms_defined=["timeout"],
                   reference=[{"label": "Code 1", "content": "y"}, {"label": "Table 1", "content": "t"}])
        out = splice_repair(best, new, ["1.2", "recap"])
        self.assertEqual([s["sentences"][0]["text"] for s in out["sections"]], ["a", "b", "NEW 1.2"])
        self.assertEqual(out["recap"], ["new recap"])
        self.assertEqual(out["questions"], best["questions"])
        self.assertEqual(out["terms_defined"], ["window", "timeout"])
        self.assertEqual([r["content"] for r in out["reference"]], ["x", "t"])
        self.assertEqual(best["sections"][2]["sentences"][0]["text"], "c")  # best is not changed

    def test_a_repair_cannot_change_a_section_that_passed(self):
        def rewrite(system, user):
            if "## FEEDBACK" not in user:
                return good_rewrite(user, "It retries 9999 times.")
            data = json.loads(good_rewrite(user))
            for section in data["sections"][:-1]:  # the repair also "improves" passing sections
                section["sentences"][0]["text"] = "Damaged by the repair."
            return json.dumps(data)

        harness = Harness(rewrite)
        _, records, _ = run_text(harness)
        first, second = records
        self.assertEqual((first["attempts"], first["flagged"]), (2, False))
        texts = [s["text"] for section in first["sections"] for s in section["sentences"]]
        self.assertNotIn("Damaged by the repair.", texts)
        self.assertNotIn("It retries 9999 times.", texts)
        self.assertEqual(first["repairs"], [{"attempt": 2, "scope": ["1.2"], "before": 1,
                                             "after": 0, "kept": True}])
        feedback = [u for u in harness.prompts("rewrite") if "## FEEDBACK" in u][0]
        self.assertIn("only in section 1.2. Rewrite only that", feedback)
        # part 2 has one section only: its repair is a full rewrite
        self.assertEqual(second["repairs"][0]["scope"], "all")

    def test_the_report_lists_each_repair(self):
        from lecture2audio.report import repair_lines
        text = "\n".join(repair_lines([
            {"part": "1", "repairs": [{"attempt": 2, "scope": ["1.2", "recap"], "before": 2,
                                       "after": 0, "kept": True}]},
            {"part": "2", "repairs": [{"attempt": 2, "scope": "all", "before": 1, "after": 1,
                                       "kept": False}]},
            {"part": "3"}]))
        self.assertIn("## Repairs", text)
        self.assertIn("- Part 1, attempt 2: section 1.2 and the recap were rewritten; the blocking "
                      "problems went from 2 to 0, so the repair was kept.", text)
        self.assertIn("- Part 2, attempt 2: the whole part was rewritten; the blocking problems "
                      "went from 1 to 1, so the earlier attempt was kept.", text)
        self.assertEqual(repair_lines([{"part": "1", "repairs": []}]), [])


class Ideas(unittest.TestCase):
    def test_ideas_are_listed_part_by_part_with_part_scoped_ids_and_cached(self):
        harness = Harness()
        with tempfile.TemporaryDirectory() as tmp:
            pipeline = harness.pipeline(tmp)
            outline = pipeline.run_outline(UNITS)
            ideas = pipeline.run_ideas(outline)
            self.assertEqual(len(harness.prompts("ideas")), 2)  # one call per part
            self.assertEqual([i["id"] for i in ideas],
                             ["T1.1", "T1.2", "T1.3", "T1.4", "T2.1", "T2.2"])
            self.assertEqual(ideas[1]["nodes"], ["1.2"])
            self.assertEqual(ideas[1]["units"], [3])
            self.assertIs(pipeline.coverage["transcript_ideas"], ideas)
            self.assertNotIn("Acknowledgements", harness.prompts("ideas")[0].split(
                "## TRANSCRIPT OF THIS PART\n", 1)[1])
            again = harness.pipeline(tmp)
            self.assertEqual(again.run_ideas(outline), ideas)
            self.assertEqual(len(harness.prompts("ideas")), 2)  # from the cache

    def test_a_node_with_units_but_no_idea_is_asked_again(self):
        answers = []

        def ideas(system, user):
            data = json.loads(dry_run_handler(system, user))
            answers.append(user)
            if len(answers) == 1:
                data["ideas"] = data["ideas"][:1]  # drops node 1.2
            return json.dumps(data)

        harness = Harness(ideas=ideas)
        with tempfile.TemporaryDirectory() as tmp:
            pipeline = harness.pipeline(tmp)
            found = pipeline.run_ideas(pipeline.run_outline(UNITS))
        self.assertIn("## UNITS NOT ACCOUNTED FOR\nUnits 3-5 are cited by no idea", answers[1])
        self.assertIn("fewer ideas are fine", answers[1])  # no pressure to pad the list
        self.assertEqual(len([i for i in found if i["id"].startswith("T1.")]), 4)

    def test_units_that_teach_nothing_may_be_declared_instead_of_padded(self):
        def ideas(system, user):
            data = json.loads(dry_run_handler(system, user))
            data["ideas"] = [i for i in data["ideas"] if i["units"] != [4]]
            if any(u == 4 for i in json.loads(dry_run_handler(system, user))["ideas"]
                   for u in i["units"]):
                data["no_content"] = [{"units": [4], "kind": "unclear", "reason": "garbled"}]
            return json.dumps(data)

        harness = Harness(ideas=ideas)
        with tempfile.TemporaryDirectory() as tmp:
            pipeline = harness.pipeline(tmp)
            pipeline.run_ideas(pipeline.run_outline(UNITS))
        self.assertEqual(len(harness.prompts("ideas")), 2)  # accounted for at the first try
        self.assertEqual(pipeline.idea_notes["no_content"],
                         [{"part": "1", "units": [4], "kind": "unclear", "reason": "garbled"}])
        self.assertEqual(pipeline.idea_notes["unaccounted"], {})

    def test_units_still_unaccounted_after_the_retries_are_kept_for_the_report(self):
        def ideas(system, user):
            data = json.loads(dry_run_handler(system, user))
            data["ideas"] = [i for i in data["ideas"] if i["units"] != [5]]
            return json.dumps(data)

        harness = Harness(ideas=ideas)
        with tempfile.TemporaryDirectory() as tmp:
            pipeline = harness.pipeline(tmp, max_retries=1)
            pipeline.run_ideas(pipeline.run_outline(UNITS))
        self.assertEqual(pipeline.idea_notes["unaccounted"], {"1": [5]})

    def test_a_part_that_keeps_failing_is_left_empty_and_tried_again_next_run(self):
        def ideas(system, user):
            if "Acknowledgements" in user.split("## TRANSCRIPT OF THIS PART\n", 1)[1]:
                return json.dumps({"ideas": [{"kind": "rumour", "idea": "x", "nodes": ["9"],
                                              "units": [99]}]})
            return dry_run_handler(system, user)

        harness = Harness(ideas=ideas)
        with tempfile.TemporaryDirectory() as tmp:
            pipeline = harness.pipeline(tmp)
            found = pipeline.run_ideas(pipeline.run_outline(UNITS))
            self.assertEqual({i["id"][:3] for i in found}, {"T1."})
            self.assertFalse((pathlib.Path(tmp) / "ideas" / "part_002.json").exists())
            self.assertTrue((pathlib.Path(tmp) / "ideas" / "part_001.json").exists())


class Citations(unittest.TestCase):
    def test_every_idea_must_be_cited_by_a_lecture_sentence(self):
        def rewrite(system, user):
            data = json.loads(good_rewrite(user))
            if len(harness.prompts("rewrite")) == 1:
                for section in data["sections"]:
                    for sentence in section["sentences"]:
                        sentence.pop("ideas", None)
            return json.dumps(data)

        harness = Harness(rewrite)
        _, records, _ = run_text(harness, coverage=LEDGER)
        self.assertEqual(records[0]["attempts"], 2)
        second = harness.prompts("rewrite")[1]
        self.assertIn("idea_uncited", second)
        self.assertIn("No \"lecture\" sentence cites the idea T1", second)
        self.assertIn("## IDEAS OF THIS PART (JSON)", second)

    def test_a_sentence_citing_no_idea_is_a_warning_and_an_unknown_id_is_refused(self):
        _, records, _ = run_text(Harness(), coverage=LEDGER)
        unledgered = [i for i in records[0]["issues"] if i["type"] == "unledgered"]
        self.assertTrue(unledgered)  # 1.2 has three units but one idea, cited once
        self.assertFalse(records[0]["flagged"])
        shape = {"preview_items": [], "sections": ["1"], "section_previews": {}, "ideas": ["T1"]}
        data = {"preview": {"intro": "i", "items": []}, "recap": ["r"],
                "questions": [{"question": "q", "answer": "a"}],
                "sections": [{"node": "1", "sentences": [
                    {"origin": "lecture", "text": "t", "ideas": ["T9"]}]}]}
        from lecture2audio.pipeline import validate_part
        self.assertTrue(any("T9" not in e and "IDEAS OF THIS PART" in e
                            for e in validate_part(data, shape)))
        data["sections"][0]["sentences"][0]["ideas"] = ["T1"]
        self.assertEqual(validate_part(data, shape), [])


class SpeechRecognition(unittest.TestCase):
    def test_known_hallucinations_and_loops_are_dropped_and_reported(self):
        from lecture2audio.transcribe import clean_segments
        kept, removed = clean_segments([
            "A router forwards packets.", "Thank you for watching!", "The table is built.",
            "The table is built.", "The table is built.", "Sous-titres réalisés par la "
            "communauté d'Amara.org", "Thank you for watching the router forward packets."])
        self.assertEqual(kept, ["A router forwards packets.", "The table is built.",
                                "The table is built.",
                                "Thank you for watching the router forward packets."])
        self.assertEqual(len(removed), 3)
        self.assertIn("silence", removed[0]["reason"])
        self.assertIn("third time", removed[1]["reason"])


class VoiceRepair(unittest.TestCase):
    def test_a_chunk_heard_wrong_is_voiced_again_and_the_better_take_kept(self):
        from lecture2audio.assemble import repair_chunks
        from lecture2audio.tts import FakeTTS
        if not shutil.which("ffmpeg"):
            self.skipTest("ffmpeg missing")
        with tempfile.TemporaryDirectory() as tmp:
            audio = pathlib.Path(tmp)
            first = audio / "a.wav"
            FakeTTS().synth("x", first)
            voiced = [[{"kind": "chunk", "path": first, "text": "the window holds packets"},
                       {"kind": "pause", "path": first, "text": None}]]
            heard = iter(["the window holes", "the window holds packets"])
            results = repair_chunks(FakeTTS(), voiced, lambda p: next(heard), 0.88, 2,
                                    audio / "roundtrip.json", silent)
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["tries"], 1)
            self.assertEqual(results[0]["score"], 1.0)
            self.assertLess(results[0]["first"], 0.88)
            self.assertEqual(sorted(p.name for p in audio.glob("*.wav")), ["a.wav"])
            again = repair_chunks(FakeTTS(), voiced, lambda p: 1 / 0, 0.88, 2,
                                  audio / "roundtrip.json", silent)  # cached: nothing heard
            self.assertEqual(again[0]["score"], 1.0)

    def test_the_voice_is_chosen_by_language(self):
        from lecture2audio import tts
        made = []
        with mock.patch.object(tts, "MingTTS", lambda **k: made.append("ming")), \
                mock.patch.object(tts, "Qwen3TTS", lambda *a, **k: made.append("qwen3")):
            tts.make_tts("auto", "en")
            tts.make_tts("auto", "fr")
        self.assertEqual(made, ["ming", "qwen3"])


class IdeaAudit(unittest.TestCase):
    def audit(self, ideas=None):
        harness = Harness(ideas=ideas)
        with tempfile.TemporaryDirectory() as tmp:
            pipeline = harness.pipeline(tmp)
            outline = pipeline.run_outline(UNITS)
            pipeline.run_ideas(outline)
            result = pipeline.audit_ideas(outline)
            cached = pipeline.audit_ideas(outline)
        return result, cached, harness

    def test_an_extractor_that_lists_everything_lists_the_planted_fact(self):
        result, cached, harness = self.audit()
        self.assertTrue(result["caught"])
        self.assertEqual(result["node"], "1.2")  # the node with the most units
        self.assertIn("7919", harness.prompts("ideas")[-1])
        self.assertEqual(cached, result)
        self.assertEqual(len(harness.prompts("ideas")), 3)  # two parts, one audit, then cache

    def test_an_extractor_that_skips_the_planted_fact_is_exposed(self):
        def ideas(system, user):
            data = json.loads(dry_run_handler(system, user))
            data["ideas"] = [i for i in data["ideas"] if "7919" not in i["idea"]]
            return json.dumps(data)
        result, _, _ = self.audit(ideas)
        self.assertFalse(result["caught"])


class IdeaNumbers(unittest.TestCase):
    def test_a_taught_idea_whose_number_is_not_in_the_script_is_disputed_not_overruled(self):
        def rewrite(system, user):
            return good_rewrite(user).replace("The window holds 64 packets.",
                                              "The window holds packets.")
        harness = Harness(rewrite)
        _, records, _ = run_text(harness, coverage=LEDGER, max_retries=1)
        first = records[0]["ideas"][0]
        self.assertEqual((first["idea"], first["status"], first["lost"]), ("T1", "taught", ["64"]))
        self.assertFalse(records[0]["flagged"])  # a code doubt reports, it does not block
        issue = next(i for i in records[0]["issues"] if i["type"] == "idea_numbers")
        self.assertIn("does not contain 64, which the transcript gives.", issue["detail"])

    def test_numbers_said_in_words_and_exponents_are_not_held_against_the_text(self):
        source = NumberIndex("The cost is O(n^2) for 10^6 items and 64 packets.", "en")
        said = NumberIndex("The cost is n squared for a million items and sixty-four packets.",
                           "en")
        self.assertEqual(numbers_lost("O(n^2) for 10^6 items, 64 packets", source, said), [])
        self.assertEqual(numbers_lost("64 packets", source, NumberIndex("many packets", "en")),
                         ["64"])

    def test_a_number_the_transcript_lacks_is_not_held_against_the_script(self):
        coverage = {"transcript_ideas": [dict(LEDGER["transcript_ideas"][0],
                                              idea="the window holds 99 packets")]}
        _, records, _ = run_text(Harness(), coverage=coverage)
        self.assertEqual(records[0]["ideas"][0]["status"], "taught")


class Audit(unittest.TestCase):
    def audit(self, verify):
        harness = Harness(verify=verify)
        with tempfile.TemporaryDirectory() as tmp:
            pipeline = harness.pipeline(tmp)
            outline = pipeline.run_outline(UNITS)
            records = pipeline.run_parts(outline)
            return pipeline.audit_verifier(outline, records)

    def test_a_verifier_that_reads_the_script_catches_the_planted_omission(self):
        result = self.audit(counting_verifier)
        self.assertEqual((result["node"], result["caught"]), ("1.2", True))

    def test_a_verifier_that_always_passes_is_exposed(self):
        def always_pass(system, user):
            nodes = json.loads(_between(user, "CHECKLIST (JSON)"))["nodes"]
            return json.dumps({"points": [{"node": n["node"], "status": "complete"} for n in nodes],
                               "issues": []})

        self.assertFalse(self.audit(always_pass)["caught"])


class AuditScoresTheModel(unittest.TestCase):
    def test_code_checks_inside_the_verifier_do_not_pass_its_audit(self):
        # The planted omission keeps only the first sentence of node 1.2, which here is not
        # "A timeout is 5 seconds."; code notices the lost 5, but the verifier model said
        # complete and taught, so the audit must still fail.
        def rewrite(system, user):
            data = json.loads(good_rewrite(user))
            for section in data["sections"]:
                if section["node"] == "1.2":
                    section["sentences"].reverse()
            return json.dumps(data)

        def always_pass(system, user):
            data = json.loads(counting_verifier(system, user))
            for point in data["points"]:
                point["status"] = "complete"
            return json.dumps(data)

        harness = Harness(rewrite, verify=always_pass)
        with tempfile.TemporaryDirectory() as tmp:
            pipeline = harness.pipeline(tmp, coverage=LEDGER)
            outline = pipeline.run_outline(UNITS)
            audit = pipeline.audit_verifier(outline, pipeline.run_parts(outline))
        self.assertEqual(audit["node"], "1.2")
        self.assertFalse(audit["caught"])

    def test_a_failed_audit_is_not_cached(self):
        harness = Harness(ideas=lambda s, u: (dry_run_handler(s, u) if "7919" not in u
                                              else "no json"))
        with tempfile.TemporaryDirectory() as tmp:
            pipeline = harness.pipeline(tmp)
            outline = pipeline.run_outline(UNITS)
            pipeline.run_ideas(outline)
            self.assertIn("runs again next time", pipeline.audit_ideas(outline)["skipped"])
            self.assertFalse((pathlib.Path(tmp) / "ideas_audit.json").exists())


class Notes(unittest.TestCase):
    def test_planned_term_never_defined_and_heavy_noise_are_noted(self):
        units = list(UNITS)
        units[1] = "um " * 30
        outline, _ = parse_outline(dict(OUTLINE, terms=[{"term": "socket", "part": 2}]), units, 1000)
        records = [{"part": "1", "terms_defined": ["window"]}, {"part": "2", "terms_defined": []}]
        notes = " ".join(TextPipeline.global_notes(outline, records))
        self.assertIn('"socket"', notes)
        self.assertIn("marked as noise", notes)


class Spoken(unittest.TestCase):
    def spoken(self, handler, language="en"):
        harness = Harness(spoken=handler)
        with tempfile.TemporaryDirectory() as tmp:
            pipeline = harness.pipeline(tmp, language=language)
            outline = pipeline.run_outline(UNITS)
            return pipeline.run_spoken(outline, pipeline.run_parts(outline))[0], harness

    def test_digits_are_converted_and_counted(self):
        result, _ = self.spoken(None)
        text = " ".join(b["text"] for b in result["blocks"])
        self.assertIn("The window holds sixty-four packets.", text)
        self.assertGreater(result["llm_residue"]["digits"], 0)

    def test_model_that_shortens_the_text_is_ignored(self):
        result, _ = self.spoken(lambda s, u: json.dumps({"blocks": ["x"] * len(json.loads(u)["blocks"])}))
        self.assertIn("sixty-four", " ".join(b["text"] for b in result["blocks"]))
        self.assertIn("shortened", result["fallback"])

    def test_wrong_number_of_blocks_is_asked_again_then_rules_take_over(self):
        result, harness = self.spoken(lambda s, u: json.dumps({"blocks": ["only one"]}))
        self.assertEqual(len(harness.prompts("spoken")), 3 * 2)
        self.assertIn("failed", result["fallback"])

    def test_blocks_follow_the_structure(self):
        result, _ = self.spoken(None)
        roles = [b["role"] for b in result["blocks"]]
        self.assertEqual(roles[:2], ["title", "preview"])
        self.assertEqual(roles[-3:], ["quiz_intro", "question", "answer"])
        self.assertEqual(result["blocks"][0]["text"], "Part one: Sending.")
        self.assertIn("This part has two points. First, the window. Second, the timeout.",
                      result["blocks"][1]["text"])
        self.assertEqual([b["chapter"] for b in result["blocks"] if b["role"] == "section"],
                         ["1", "1.1", "1.2"])


class Wording(unittest.TestCase):
    def test_french_previews(self):
        items = [{"phrase": "la fenêtre"}, {"phrase": "le délai."}]
        self.assertEqual(list_sentence(items, "fr", "points"),
                         "Cette partie comporte deux points. Premièrement, la fenêtre. "
                         "Deuxièmement, le délai.")
        self.assertEqual(list_sentence(items[:1], "en", "points"), "This part has one point: la fenêtre.")
        self.assertEqual(list_sentence(items, "en", "subpoints"),
                         "This point has two sub-points. First, la fenêtre. Second, le délai.")


class EndToEnd(unittest.TestCase):
    TRANSCRIPT = ("A router forwards packets between networks. It looks at the destination address "
                  "and picks the next hop from its table.\n\n"
                  "The table is built by a routing protocol, for example OSPF. Each link has a cost "
                  "of 1 or more, and the protocol adds the costs along a path.\n\n"
                  "The cheapest path wins. If two paths cost the same, the router may use both.")

    def setUp(self):
        # never write into the user's real vault, never open Obsidian
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("L2A_VAULT_DIR", None)

    def run_cli(self, *args):
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                main(list(args))
        return raised.exception.code

    def build(self, tmp, *extra):
        source = pathlib.Path(tmp) / "lecture.txt"
        source.write_text(self.TRANSCRIPT, encoding="utf-8")
        out = pathlib.Path(tmp) / "out"
        code = self.run_cli("build", str(source), "--llm", "dry", "--tts", "fake", "--out", str(out),
                            "--language", "en", "--max-part-words", "25", "--quiz-pause", "1",
                            "--factcheck", "off", *extra)
        self.assertEqual(code, 0)
        return out / "lecture"

    @staticmethod
    def probe(path):
        result = subprocess.run(["ffprobe", "-v", "error", "-show_chapters", "-show_format", "-of",
                                 "json", str(path)], capture_output=True, text=True, check=True)
        return json.loads(result.stdout)

    def test_build_with_dry_model_and_fake_voice(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = self.build(tmp)
            for name in ("script.md", "index.md", "questions.md", "reference.md", "report.md",
                         "lecture.m4a", "transcript.txt", "outline.json", "ideas.csv"):
                self.assertTrue((work / name).exists(), name)
            outline = json.loads((work / "outline.json").read_text())
            parts = len(outline["parts"])
            points = sum(len(p["children"]) for p in outline["parts"])
            self.assertGreaterEqual(parts, 2)
            self.assertRegex((work / "index.md").read_text(), r"- 1 .*\(0:00\)")
            if shutil.which("ffprobe"):
                info = self.probe(work / "lecture.m4a")
                self.assertEqual(len(info["chapters"]), 2 * parts + points)
                self.assertGreater(float(info["format"]["duration"]), 5)

    def test_the_idea_ledger_follows_every_idea_to_a_card(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = self.build(tmp, "--export-anki")
            with open(work / "ideas.csv", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(list(rows[0]), ["idea_id", "node_id", "kind", "statement",
                                             "source_units", "covered_in_card", "status"])
            self.assertTrue(rows)
            self.assertEqual({r["status"] for r in rows}, {"verified"})
            self.assertTrue(all(r["covered_in_card"] and r["source_units"] for r in rows))
            report = (work / "report.md").read_text()
            self.assertIn("## Idea ledger (ideas.csv)", report)
            self.assertIn("The test of the idea listing planted one fact", report)
            self.assertIn("the fact was listed.", report)
            self.assertIn("The test of the course judge", report)
            self.assertIn("## Stages of this run", report)
            self.assertIn("| transcribe | the input | read from the input file |", report)
            self.assertIn("| course | rewrite, ideas | computed in this run |", report)
            self.assertLess(report.index("## Stages of this run"),
                            report.index("## Removed as noise"))
            self.assertFalse((work / "ideas.csv.tmp").exists())
            anki = (work / "anki.csv").read_text(encoding="utf-8")
            self.assertTrue(anki.startswith("#separator:Comma\n"))
            self.assertIn("#columns:Front,Back,Tags", anki)
            self.assertIn("lecture::idea-T1.1", anki)

    def test_the_vault_embeds_the_audio_and_reads_as_one_chapter(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = self.build(tmp)
            vault = work / "vault"
            self.assertTrue((vault / "audio" / "lecture.m4a").exists())
            self.assertTrue((vault / "audio" / "lecture – 01.m4a").exists())
            index = (vault / "lecture – Index.md").read_text(encoding="utf-8")
            self.assertIn("![[audio/lecture.m4a]]", index)
            self.assertIn("[[lecture – Full Lecture]]", index)
            part = next(vault.glob("lecture – 01 *.md")).read_text(encoding="utf-8")
            self.assertIn("![[audio/lecture – 01.m4a]]", part)
            book = (vault / "lecture – Full Lecture.md").read_text(encoding="utf-8")
            self.assertIn("![[lecture – 01 ", book)
            self.assertIn("![[lecture – 02 ", book)

    def test_a_folder_without_a_command_is_built(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = pathlib.Path(tmp) / "Week 3"
            folder.mkdir()
            (folder / "transcript.txt").write_text(self.TRANSCRIPT, encoding="utf-8")
            (folder / "glossary.txt").write_text("router\nrouting protocol\n", encoding="utf-8")
            out = pathlib.Path(tmp) / "out"
            code = self.run_cli(str(folder), "--llm", "dry", "--tts", "fake", "--out", str(out),
                                "--language", "en", "--max-part-words", "25", "--quiz-pause", "1",
                                "--no-open-obsidian")
            self.assertEqual(code, 0)
            self.assertTrue((out / "week-3" / "week-3.m4a").exists())
            self.assertTrue((out / "week-3" / "vault" / "week-3 – Index.md").exists())

    def test_no_course_writes_no_vault(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = pathlib.Path(tmp) / "lecture.txt"
            source.write_text(self.TRANSCRIPT, encoding="utf-8")
            out = pathlib.Path(tmp) / "out"
            code = self.run_cli("text", str(source), "--llm", "dry", "--out", str(out),
                                "--language", "en", "--no-course")
            self.assertEqual(code, 0)
            self.assertFalse((out / "lecture" / "vault").exists())
            self.assertTrue((out / "lecture" / "ideas.csv").exists())

    def test_no_quiz_gives_a_shorter_audio(self):
        if not shutil.which("ffprobe"):
            self.skipTest("ffprobe missing")
        with tempfile.TemporaryDirectory() as tmp:
            with_quiz = float(self.probe(self.build(tmp) / "lecture.m4a")["format"]["duration"])
            work = self.build(tmp, "--no-quiz", "--redo", "audio")
            without = float(self.probe(work / "lecture.m4a")["format"]["duration"])
        self.assertLess(without, with_quiz)

    def test_text_command_stops_before_the_voice(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = pathlib.Path(tmp) / "lecture.txt"
            source.write_text(self.TRANSCRIPT, encoding="utf-8")
            out = pathlib.Path(tmp) / "out"
            code = self.run_cli("text", str(source), "--llm", "dry", "--out", str(out), "--language", "en")
            self.assertEqual(code, 0)
            self.assertTrue((out / "lecture" / "script.md").exists())
            self.assertFalse((out / "lecture" / "audio").exists())

    def test_changed_transcript_discards_old_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = pathlib.Path(tmp) / "lecture.txt"
            out = pathlib.Path(tmp) / "out"
            source.write_text(self.TRANSCRIPT, encoding="utf-8")
            self.run_cli("text", str(source), "--llm", "dry", "--out", str(out), "--language", "en")
            source.write_text("A completely different lecture about switches and VLANs.", encoding="utf-8")
            self.run_cli("text", str(source), "--llm", "dry", "--out", str(out), "--language", "en")
            script = (out / "lecture" / "script.md").read_text(encoding="utf-8")
            self.assertIn("VLANs", script)
            self.assertNotIn("router", script)


if __name__ == "__main__":
    unittest.main()
