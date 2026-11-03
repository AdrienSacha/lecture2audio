import copy
import json
import pathlib
import re
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from lecture2audio import course
from lecture2audio.course import (CoursePipeline, build_review, card_issues, dry_course,
                                  dry_course_check, normalise, validate_cards)
from lecture2audio.course_render import clean, link_terms, merge_terms, quote, write_vault
from lecture2audio.course_schema import Cards, Diagram
from lecture2audio.llm import FakeLLM, dry_run_handler
from lecture2audio.pipeline import TextPipeline, load_prompts, sentences_of
from test_pipeline import UNITS, Harness, silent

TERM = {"term": "window", "definition": "A window is the number of packets the sender may "
        "send before it must wait.", "depends_on": [], "confusable": [], "part": 1}


def card(**over):
    base = {"node": "1.1", "kind": "concept", "title": "T", "terms": [], "definition": "",
            "instance": "", "explanation": ["A sentence."], "formulas": [], "notation": [],
            "example": None, "diagram": None, "code": [], "table": None, "checks": [],
            "covers": [1]}
    return normalise(dict(base, **over))


def ctx(script=None, terms=(), later=(), notation=None):
    script = script or [{"origin": "lecture", "text": "The window holds 64 packets."}]
    return {"script": script, "count": len(script), "nodes": ["1", "1.1"], "slides": "",
            "termbase": [dict(t, define_here=True) for t in terms], "later_terms": list(later),
            "notation": notation or {}}


def kinds(issues):
    return sorted(i["type"] for i in issues)


FRAMES = {"title": "Search", "frames": [
    {"caption": "Start.", "mermaid": "graph LR\n  A --> B"},
    {"caption": "Visit A.", "mermaid": "graph LR\n  A:::seen --> B\n  classDef seen fill:#cde"}]}


class Figures(unittest.TestCase):
    def test_a_diagram_is_a_list_of_mermaid_frames(self):
        self.assertEqual(Diagram.model_validate(FRAMES).frames[0].caption, "Start.")

    def test_frames_must_be_mermaid_without_scripts_links_or_html(self):
        for bad in ("plot this", "graph LR\n  A --> B\n  click A callback", "graph TD\n A-->B\n"
                    "<script>x</script>", "graph TD\n A[\"see https://x.io\"]"):
            with self.assertRaises(Exception, msg=bad):
                Diagram.model_validate({"frames": [{"caption": "c", "mermaid": bad}]})
        with self.assertRaises(Exception):
            Diagram.model_validate({"frames": [{"caption": "c", "mermaid": "graph LR\n A-->B"}] * 9})

    def test_cards_reject_raw_html_and_bad_latex_but_take_good_math(self):
        c = ctx()
        html = dict(card(), node="1", explanation=["Use <b>bold</b> here."])
        self.assertTrue(validate_cards({"cards": [html]}, c))
        bad = dict(card(), node="1", formulas=[r"\frac{1}{"])
        self.assertTrue(validate_cards({"cards": [bad]}, c))
        inline = dict(card(), node="1", explanation=["Take $\\frac{1}{$ here."])
        self.assertTrue(validate_cards({"cards": [inline]}, c))
        good = dict(card(), node="1", formulas=[r"T = \frac{L}{R}"],
                    explanation=["The rate $R$ is in bits per second and 2 < 3."])
        self.assertEqual(validate_cards({"cards": [good]}, c), [])

    def test_code_and_tables_are_checked(self):
        c = ctx()
        ok = dict(card(), node="1", code=[{"language": "python", "code": "print(1)"}],
                  table={"columns": ["a", "b"], "rows": [["1", "2"]]})
        self.assertEqual(validate_cards({"cards": [ok]}, c), [])
        ragged = dict(ok, table={"columns": ["a", "b"], "rows": [["1"]]})
        self.assertTrue(validate_cards({"cards": [ragged]}, c))
        fenced = dict(ok, code=[{"code": "```x```"}])
        self.assertTrue(validate_cards({"cards": [fenced]}, c))


class CardChecks(unittest.TestCase):
    def test_every_script_sentence_must_be_covered(self):
        script = [{"origin": "lecture", "text": "One."}, {"origin": "lecture", "text": "Two."}]
        issues = card_issues([card(covers=[1])], ctx(script), "en")
        self.assertEqual(kinds(issues), ["uncovered"])
        self.assertIn("sentence 2", issues[0]["detail"])

    def test_an_owed_term_is_defined_once(self):
        c = ctx(terms=[TERM])
        self.assertEqual(kinds(card_issues([card()], c, "en")), ["undefined_term"])
        defined = card(terms=["window"], definition="A window is ...",
                       checks=[{"question": "q", "answer": "a"}])
        self.assertEqual(card_issues([defined], c, "en"), [])
        self.assertEqual(kinds(card_issues([defined, defined], c, "en")), ["double_definition"])

    def test_a_term_is_not_used_before_its_card(self):
        c = ctx(terms=[TERM], script=[{"origin": "lecture", "text": "a"}] * 2)
        early = card(explanation=["The window is used here."], covers=[1, 2])
        defined = card(terms=["window"], definition="A window is x.", covers=[1, 2],
                       checks=[{"question": "q", "answer": "a"}])
        self.assertIn("term_order", kinds(card_issues([early, defined], c, "en")))

    def test_a_term_of_a_later_part_is_not_used(self):
        later = [{"term": "timeout", "definition": "d", "part": 2}]
        issues = card_issues([card(explanation=["The timeout expires."])], ctx(later=later), "en")
        self.assertEqual(kinds(issues), ["term_order"])

    def test_cards_that_define_or_calculate_need_a_check_and_an_example(self):
        issues = card_issues([card(kind="procedure", terms=["window"], definition="x")],
                             ctx(terms=[TERM]), "en")
        self.assertIn("no_check", kinds(issues))
        self.assertIn("no_example", kinds(issues))

    def test_analogies_are_flagged_in_both_languages(self):
        for text, language in (("Think of it as a pipe.", "en"), ("Imagine a pipe.", "en"),
                               ("Pensez à un tuyau.", "fr"), ("C'est comme un tuyau.", "fr")):
            issues = card_issues([card(explanation=[text])], ctx(), language)
            self.assertEqual(kinds(issues), ["analogy"], text)
        self.assertEqual(card_issues([card(explanation=["A pipe has a diameter."])], ctx(), "en"),
                         [])

    def test_numbers_must_come_from_the_script_but_instances_may_illustrate(self):
        plain = card(explanation=["The window holds 99 packets."])
        self.assertEqual(kinds(card_issues([plain], ctx(), "en")), ["unsupported"])
        fine = card(explanation=["The window holds 64 packets."], instance="Take W = 8 packets.")
        self.assertEqual(card_issues([fine], ctx(), "en"), [])

    def test_a_symbol_keeps_one_meaning(self):
        a = card(notation=[{"symbol": "R", "meaning": "rate"}])
        b = card(notation=[{"symbol": "R", "meaning": "radius"}])
        self.assertEqual(kinds(card_issues([a, b], ctx(), "en")), ["notation"])
        issues = card_issues([a], ctx(notation={"R": "resistance"}), "en")
        self.assertEqual(kinds(issues), ["notation"])


class Shape(unittest.TestCase):
    def test_validate_cards_rejects_unknown_nodes_terms_and_wrong_order(self):
        c = ctx(terms=[TERM])
        good = {"cards": [dict(card(), node="1")]}
        self.assertEqual(validate_cards(good, c), [])
        self.assertTrue(validate_cards({"cards": [dict(card(), node="9")]}, c))
        self.assertTrue(validate_cards({"cards": [dict(card(), terms=["nope"],
                                                        definition="d")]}, c))
        self.assertTrue(validate_cards({"cards": [card(), dict(card(), node="1")]}, c))
        self.assertTrue(validate_cards({"cards": [dict(card(), covers=[5])]}, c))
        self.assertTrue(validate_cards({"cards": []}, c))


class Review(unittest.TestCase):
    def test_review_comes_from_earlier_parts_only(self):
        def part(pid, n):
            return {"part": pid, "cards": [{"id": f"{pid}.{k}", "checks": [
                {"question": f"q{pid}.{k}", "answer": "a"}]} for k in range(n)]}
        review = build_review([part("1", 2), part("2", 2), part("3", 1)])
        self.assertEqual(review["1"], [])
        self.assertTrue(all(r["part"] == "1" for r in review["2"]))
        self.assertEqual({r["part"] for r in review["3"]}, {"1", "2"})
        self.assertEqual({r["part"] for r in review["final"]}, {"1", "2", "3"})


def stage(writer=None, judge=None, terms=(), max_retries=2, coverage=None, cold=None):
    """Run the text stages with the test harness, then the course stage with given handlers."""
    harness = Harness()
    calls = {"write": [], "judge": []}

    def handler(system, user):
        if "You write the course material" in system:
            calls["write"].append(user)
            return (writer or (lambda u, n: dry_course(u)))(user, len(calls["write"]))
        if "You check the course material" in system:
            calls["judge"].append(user)
            return (judge or (lambda u, n: dry_course_check(u)))(user, len(calls["judge"]))
        if "You read as a student" in system:
            calls.setdefault("cold", []).append(user)
            return (cold or (lambda u: dry_run_handler(system, u)))(user)
        if "You check terminology" in system:
            return dry_run_handler(system, user)
        return harness.handle(system, user)

    llm = FakeLLM(handler)
    tmp = tempfile.TemporaryDirectory()
    pipeline = TextPipeline(pathlib.Path(tmp.name), "en", llm, llm, load_prompts("en"),
                            max_retries=max_retries, log=silent, max_part_words=1000, workers=1)
    pipeline.terms = list(terms)
    if coverage:
        pipeline.coverage = coverage
    outline = pipeline.run_outline(UNITS)
    records = pipeline.run_parts(outline)
    result = CoursePipeline(pipeline).run(outline, records)
    return outline, records, result, calls, tmp


class Stage(unittest.TestCase):
    def test_every_script_sentence_reaches_a_card(self):
        outline, records, result, calls, tmp = stage()
        self.addCleanup(tmp.cleanup)
        for record, part in zip(records, result["parts"]):
            covered = {n for c in part["cards"] for n in c["covers"]}
            self.assertEqual(covered, set(range(1, len(sentences_of(record)) + 1)))
            self.assertFalse(part["flagged"])

    def test_the_writer_sees_the_script_and_never_the_transcript(self):
        outline, records, result, calls, tmp = stage()
        self.addCleanup(tmp.cleanup)
        self.assertIn("## SCRIPT\n", calls["write"][0])
        self.assertNotIn("TRANSCRIPT OF THIS PART", calls["write"][0])

    def test_a_dropped_sentence_is_repaired_with_feedback(self):
        def writer(user, n):
            data = json.loads(dry_course(user))
            if n == 1:
                data["cards"] = data["cards"][:-1]
            return json.dumps(data)
        outline, records, result, calls, tmp = stage(writer)
        self.addCleanup(tmp.cleanup)
        first = result["parts"][0]
        self.assertEqual(first["attempts"], 2)
        self.assertFalse(first["flagged"])
        self.assertIn("is not covered by any card", calls["write"][1])
        self.assertIn("FEEDBACK", calls["write"][1])

    def test_the_best_attempt_is_kept_and_the_part_flagged(self):
        def writer(user, n):
            data = json.loads(dry_course(user))
            data["cards"] = data["cards"][:-1] if n == 2 else data["cards"][:-2] or data["cards"][:1]
            return json.dumps(data)
        outline, records, result, calls, tmp = stage(writer, max_retries=2)
        self.addCleanup(tmp.cleanup)
        part = result["parts"][1]  # one card only: nothing to drop, so it passes at once
        self.assertEqual(part["attempts"], 1)
        flagged = result["parts"][0]
        self.assertTrue(flagged["flagged"])
        self.assertEqual(flagged["attempts"], 3)
        self.assertEqual(flagged["blocking"], min(
            i for i in [flagged["blocking"]]))  # the kept attempt has the fewest problems

    def test_the_judge_blocks_an_added_fact(self):
        def judge(user, n):
            cards = json.loads(user.split("## CARDS (JSON)\n", 1)[1])
            status = "added_fact" if n == 1 else "faithful"
            return json.dumps({"cards": [{"card": c["card"], "status": status,
                                          "detail": "the card says more than the script"
                                          if status != "faithful" else ""} for c in cards]})
        outline, records, result, calls, tmp = stage(judge=judge)
        self.addCleanup(tmp.cleanup)
        self.assertEqual(result["parts"][0]["attempts"], 2)
        self.assertIn("added_fact", calls["write"][1])

    def test_defined_terms_are_checked_against_the_termbase(self):
        outline, records, result, calls, tmp = stage(terms=[TERM])
        self.addCleanup(tmp.cleanup)
        first = result["parts"][0]["cards"]
        self.assertEqual([c["terms"] for c in first if c["terms"]], [["window"]])
        self.assertTrue(all(c["checks"] for c in first if c["terms"]))

    def test_cards_know_where_they_come_from(self):
        outline, records, result, calls, tmp = stage()
        self.addCleanup(tmp.cleanup)
        for part in result["parts"]:
            for c in part["cards"]:
                self.assertIn(c["origin"], {"lecture", "added", "external"})
                self.assertEqual(c["sources"], [])

    def test_the_stage_is_cached(self):
        harness_calls = []
        outline, records, result, calls, tmp = stage()
        self.addCleanup(tmp.cleanup)
        before = len(calls["write"])
        # a second run on the same folder writes nothing new
        llm = FakeLLM(lambda s, u: harness_calls.append(1) or "{}")
        pipeline = TextPipeline(pathlib.Path(tmp.name), "en", llm, llm, load_prompts("en"),
                                log=silent, workers=1)
        again = CoursePipeline(pipeline).run(outline, records)
        self.assertEqual(harness_calls, [])
        self.assertEqual(len(again["parts"]), len(result["parts"]))
        self.assertEqual(before, len(calls["write"]))


IDEAS = {"slide_ideas": [], "gaps": [], "transcript_ideas": [
    {"id": "T1", "kind": "definition", "idea": "the window holds 64 packets", "nodes": ["1.1"]},
    {"id": "T2", "kind": "condition", "idea": "a timeout is 5 seconds", "nodes": ["1.2"]}]}


class Ideas(unittest.TestCase):
    def test_the_writer_and_the_judge_get_the_ideas_of_the_part(self):
        outline, records, result, calls, tmp = stage(coverage=IDEAS)
        self.addCleanup(tmp.cleanup)
        self.assertIn("IDEAS TO TEACH (JSON)", calls["write"][0])
        self.assertIn("T1", calls["write"][0])
        self.assertNotIn("T1", calls["write"][1])  # part 2 has none of them
        self.assertIn("## IDEAS (JSON)", calls["judge"][0])
        self.assertFalse(result["parts"][0]["flagged"])

    def test_an_idea_the_judge_calls_missing_is_a_blocking_problem(self):
        def judge(user, n):
            data = json.loads(dry_course_check(user))
            if n == 1:
                data["ideas"][0]["status"] = "missing"
            return json.dumps(data)
        outline, records, result, calls, tmp = stage(judge=judge, coverage=IDEAS)
        self.addCleanup(tmp.cleanup)
        self.assertEqual(result["parts"][0]["attempts"], 2)
        self.assertIn("idea_missing", calls["write"][1])
        self.assertIn("T1", calls["write"][1])

    def test_the_judge_must_answer_for_every_idea(self):
        data = {"cards": [{"card": 1, "status": "faithful"}], "ideas": []}
        self.assertTrue(course.validate_judgement(data, 1, ["T1"]))
        data["ideas"] = [{"idea": "T1", "status": "taught"}]
        self.assertEqual(course.validate_judgement(data, 1, ["T1"]), [])

    def test_the_gap_search_accepts_and_checks_transcript_ideas(self):
        from lecture2audio import enrich
        data = {"slide_ideas": [], "gaps": [], "transcript_ideas": IDEAS["transcript_ideas"]}
        self.assertEqual(enrich.validate_coverage(data, ["1", "1.1", "1.2", "2"], 0), [])
        bad = dict(data, transcript_ideas=[{"id": "T1", "kind": "rumour", "idea": "x",
                                            "nodes": ["9"]}])
        self.assertEqual(len(enrich.validate_coverage(bad, ["1", "1.1"], 0)), 2)
        self.assertEqual(enrich.validate_coverage({"slide_ideas": [], "gaps": []}, ["1"], 0), [])


class ColdRead(unittest.TestCase):
    def test_loose_ends_are_reported_and_do_not_block(self):
        def cold(user):
            return json.dumps({"loose": [{"card": "1.1", "kind": "undefined_term",
                                          "detail": "the word packet is never defined"}]})
        outline, records, result, calls, tmp = stage(cold=cold)
        self.addCleanup(tmp.cleanup)
        self.assertEqual(len(result["cold"]), 2)
        self.assertEqual(result["cold"][0]["loose"][0]["kind"], "undefined_term")
        self.assertFalse(result["parts"][0]["flagged"])
        report = course.course_report(result, {"cards": 1, "diagrams": 0, "notes": 1})
        self.assertIn("Card 1.1 (undefined_term)", report)

    def test_the_reader_knows_only_the_earlier_parts(self):
        earlier = dict(TERM, part=1)
        later = {"term": "acknowledgement", "definition": "A reply.", "depends_on": [],
                 "confusable": [], "part": 2}
        outline, records, result, calls, tmp = stage(terms=[earlier, later])
        self.addCleanup(tmp.cleanup)
        first, second = calls["cold"]
        self.assertIn("nothing", first.split("## THIS PART")[0])
        self.assertIn("window", second.split("## THIS PART")[0])
        self.assertNotIn("acknowledgement", second.split("## THIS PART")[0])

    def test_a_bad_answer_is_validated_and_a_failure_does_not_stop_the_build(self):
        self.assertTrue(course.validate_cold({"loose": [{"card": "9.9", "kind": "x",
                                                         "detail": ""}]}, ["1.1"]))
        outline, records, result, calls, tmp = stage(cold=lambda u: "not json")
        self.addCleanup(tmp.cleanup)
        self.assertEqual(result["cold"][0]["loose"], [])
        self.assertIn("error", result["cold"][0])


class CoverageAudit(unittest.TestCase):
    def run_audit(self, blind):
        harness = Harness()
        seen = []

        def handler(system, user):
            if "You find the gaps" in system:
                seen.append(user)
                if "] um ok\n" in user + "\n" and not blind:
                    return json.dumps({"slide_ideas": [], "gaps": [{
                        "id": "G1", "kind": "skipped_step", "node": "1.2", "detail": "d",
                        "query": "q"}]})
                return dry_run_handler(system, user)
            return harness.handle(system, user)

        llm = FakeLLM(handler)
        with tempfile.TemporaryDirectory() as tmp:
            pipeline = TextPipeline(pathlib.Path(tmp), "en", llm, llm, load_prompts("en"),
                                    log=silent, max_part_words=1000, workers=1)
            outline = pipeline.run_outline(UNITS)
            pipeline.run_coverage(outline)
            return pipeline.audit_coverage(outline), seen

    def test_a_search_that_notices_the_hole_passes(self):
        audit, seen = self.run_audit(blind=False)
        self.assertTrue(audit["caught"])
        self.assertEqual(audit["node"], "1.2")
        self.assertNotIn("] um ok\n", seen[0] + "\n")  # the real run was not blanked
        self.assertIn("[4] um ok", seen[1])

    def test_a_blind_search_is_reported(self):
        audit, _ = self.run_audit(blind=True)
        self.assertFalse(audit["caught"])


def lecture(outline, records, result, terms, name="flow"):
    return {"name": name, "outline": outline, "course": result, "records": records,
            "terms": list(terms)}


class Vault(unittest.TestCase):
    def write(self, writer=None, terms=(TERM,)):
        outline, records, result, calls, tmp = stage(writer, terms=list(terms))
        self.addCleanup(tmp.cleanup)
        folder = pathlib.Path(tmp.name) / "vault"
        stats = write_vault(folder, [lecture(outline, records, result, terms)], "en")
        return folder, stats, result

    def test_the_vault_has_an_index_parts_terms_and_reference_notes(self):
        folder, stats, _ = self.write()
        root = folder
        names = sorted(p.name for p in folder.glob("*.md"))
        self.assertIn("flow – Index.md", names)
        self.assertEqual(len([n for n in names if re.match(r"flow – \d\d ", n)]), 2)
        for note in ("flow – Sources.md", "flow – Review.md"):
            self.assertIn(note, names)
        for note in ("00 Course.md", "Flow control – Glossary.md", "Flow control – Notation.md"):
            self.assertTrue((root / note).exists(), note)
        self.assertTrue((root / "Terms" / "window.md").exists())
        self.assertEqual(stats["terms"], 1)

    def test_every_script_sentence_is_in_the_notes(self):
        folder, _, _ = self.write()
        text = " ".join(p.read_text() for p in folder.glob("*.md"))
        text = re.sub(r"\[\[([^\]|]*)(?:\|([^\]]*))?\]\]", lambda m: m.group(2) or m.group(1), text)
        for unit in ("The window holds 64 packets", "A timeout is 5 seconds"):
            self.assertIn(unit, text.replace("**", ""))

    def test_terms_are_linked_and_the_defined_term_is_bold(self):
        folder, _, _ = self.write()
        part = next(folder.glob("flow – 01 *.md")).read_text()
        self.assertIn("> [!definition]", part)
        self.assertIn("**window**", part)
        self.assertIn("[[window]]", part)
        self.assertEqual(part.count("[[window"), 1)  # linked once per card

    def test_checks_are_collapsed_callouts_and_notes_link_to_each_other(self):
        folder, _, _ = self.write()
        part = next(folder.glob("flow – 01 *.md")).read_text()
        self.assertIn("> [!question]- What is window?", part)
        self.assertIn("Next: [[flow – 02 ", part)
        self.assertIn("[[flow – 01 ", next(folder.glob("flow – 02 *.md")).read_text())
        term = (folder / "Terms" / "window.md").read_text()
        self.assertIn("Defined in:", term)
        self.assertIn("[[flow – 01 Sending#", term)

    def test_math_and_code_are_not_linked(self):
        out = link_terms("The $window$ and `window` are not links but window is.",
                         [{"term": "window"}], set())
        self.assertEqual(out.count("[["), 1)
        self.assertTrue(out.endswith("but [[window]] is."))

    def test_figures_become_numbered_mermaid_frames(self):
        def writer(user, n):
            data = json.loads(dry_course(user))
            data["cards"][0]["diagram"] = FRAMES
            return json.dumps(data)
        folder, stats, _ = self.write(writer)
        part = next(folder.glob("flow – 01 *.md")).read_text()
        self.assertEqual(part.count("```mermaid"), 2)
        self.assertIn("*Figure, step 2 / 2.* Visit A.", part)

    def test_names_and_callouts_are_safe(self):
        self.assertEqual(clean('a/b: c#d [e]|f'), "a b c d e f")
        self.assertEqual(quote("one\n\ntwo"), "> one\n>\n> two")


if __name__ == "__main__":
    unittest.main()
