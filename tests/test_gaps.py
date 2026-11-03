"""Gaps, research and terminology: gates, termbase logic, research clients, and the loop."""
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from lecture2audio import enrich, llm, web
from lecture2audio.llm import AnthropicLLM, FakeLLM, OpenAILLM, _between, dry_run_handler
from lecture2audio.outline import parse_outline
from lecture2audio.pipeline import TextPipeline, load_prompts, validate_part

from test_llm import FakeServer, anthropic_stream, chat_stream, responses_stream
from test_pipeline import OUTLINE, UNITS, counting_verifier, good_rewrite, silent

IDS = ["1", "1.1", "1.2", "2"]
PAGE = ("A retransmission timeout is the time a sender waits for an acknowledgement before "
        "it sends the segment again, and it is computed from the measured round-trip time.")


def outline():
    result, errors = parse_outline(OUTLINE, UNITS, 1000)
    assert not errors, errors
    return result


class Coverage(unittest.TestCase):
    def idea(self, **kw):
        return dict({"id": "S1", "slide": 1, "idea": "x", "status": "covered", "nodes": ["1.1"]},
                    **kw)

    def gap(self, **kw):
        return dict({"id": "G1", "kind": "skipped_step", "node": "1.2", "detail": "d",
                     "query": "q"}, **kw)

    def test_valid(self):
        data = {"slide_ideas": [self.idea(), self.idea(id="S2", status="missing", nodes=[])],
                "gaps": [self.gap(kind="slide_missing", idea="S2")]}
        self.assertEqual(enrich.validate_coverage(data, IDS, 1), [])

    def test_partial_idea_needs_a_gap_and_every_slide_needs_an_idea(self):
        data = {"slide_ideas": [self.idea(status="partial")], "gaps": []}
        errors = " ".join(enrich.validate_coverage(data, IDS, 2))
        self.assertIn("slide_missing", errors)
        self.assertIn("slides [2]", errors)

    def test_term_gap_names_its_term_and_node_exists(self):
        data = {"slide_ideas": [], "gaps": [self.gap(kind="undefined_term", node="9")]}
        errors = " ".join(enrich.validate_coverage(data, IDS, 0))
        self.assertIn('"term"', errors)
        self.assertIn('"node"', errors)


class Termbase(unittest.TestCase):
    def term(self, term, **kw):
        return dict({"term": term, "definition": f"def of {term}", "depends_on": [],
                     "confusable": [], "first_node": "1.1"}, **kw)

    def test_dependency_without_entry_cycle_and_required_term(self):
        data = {"terms": [self.term("timeout", depends_on=["round-trip time"])]}
        self.assertIn("no entry", " ".join(enrich.validate_termbase(data, IDS, [])))
        data = {"terms": [self.term("a", depends_on=["b"]), self.term("b", depends_on=["a"])]}
        self.assertIn("circle", " ".join(enrich.validate_termbase(data, IDS, [])))
        data = {"terms": [self.term("a")]}
        self.assertIn('"window"', " ".join(enrich.validate_termbase(data, IDS, ["window"])))

    def test_define_at_first_use_dependencies_earlier_glossary_wins(self):
        data = {"terms": [
            self.term("timeout", first_node="2", depends_on=["acknowledgement"],
                      confusable=["delay"]),
            self.term("acknowledgement", first_node="2"),
            self.term("delay", first_node="2")]}
        terms = {t["term"]: t for t in enrich.build_termbase(
            data, outline(), "timeout: the waiting time before a resend")}
        # "timeout" is first said in unit 3 (node 1.2), earlier than the model's "2".
        self.assertEqual(terms["timeout"]["define_at"], "1.2")
        # "acknowledgement" is first said in part 2 but "timeout" relies on it.
        self.assertEqual(terms["acknowledgement"]["define_at"], "1.2")
        self.assertEqual(terms["timeout"]["definition"], "the waiting time before a resend")
        self.assertIn("timeout", terms["delay"]["confusable"])
        self.assertFalse(terms["delay"]["in_lecture"])


class Research(unittest.TestCase):
    FACT = {"text": "The timeout is computed from the round-trip time.",
            "url": "https://example.edu/tcp", "quote": "it is computed from the measured "
                                                       "round-trip time"}

    def test_quote_found_in_returned_page(self):
        kept, rejected = enrich.check_facts([self.FACT], [{"url": "https://www.example.edu/tcp/",
                                                           "title": "TCP", "text": PAGE}])
        self.assertEqual((len(kept), rejected), (1, []))
        self.assertEqual(kept[0]["title"], "TCP")

    def test_page_fetched_when_only_the_address_is_known(self):
        kept, _ = enrich.check_facts([self.FACT], [{"url": self.FACT["url"], "text": None}],
                                     fetch=lambda url: PAGE)
        self.assertEqual(len(kept), 1)

    def test_invented_quote_or_unknown_address_is_rejected(self):
        fact = dict(self.FACT, quote="the timeout is always exactly three seconds long")
        kept, rejected = enrich.check_facts([fact], [{"url": fact["url"], "text": PAGE}],
                                            fetch=lambda url: PAGE)
        self.assertEqual(kept, [])
        self.assertIn("word for word", rejected[0]["reason"])
        kept, rejected = enrich.check_facts([self.FACT], [])
        self.assertIn("not among", rejected[0]["reason"])

    def test_short_quotes_do_not_count(self):
        self.assertFalse(web.quote_found("round-trip time", [PAGE]))
        self.assertTrue(web.quote_found("It is computed  from the MEASURED round–trip time.",
                                        [PAGE]))

    def test_only_public_addresses_are_fetched(self):
        for url in ("http://localhost/x", "http://127.0.0.1/", "http://10.0.0.2/",
                    "file:///etc/passwd", "http://[::1]/"):
            self.assertFalse(web.public_url(url), url)
        with self.assertRaises(web.FetchError):
            web.fetch_page("http://192.168.1.1/")


class ResearchClients(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "ak", "OPENAI_API_KEY": "ok"})
        env.start()
        self.addCleanup(env.stop)

    def test_anthropic_tools_pause_turn_and_documents(self):
        first = [{"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_fetch",
                  "input": {"url": "https://example.edu/tcp"}},
                 {"type": "web_fetch_tool_result", "tool_use_id": "srvtoolu_1", "content": {
                     "type": "web_fetch_result", "url": "https://example.edu/tcp",
                     "retrieved_at": "2026-11-03T00:00:00Z",
                     "content": {"type": "document", "title": "TCP", "citations": None,
                                 "source": {"type": "text", "media_type": "text/plain",
                                            "data": PAGE}}}}]
        second = [{"type": "text", "text": "Found.", "citations": [
                      {"type": "web_search_result_location", "url": "https://b.org/x",
                       "title": "B", "cited_text": "a cited passage", "encrypted_index": "e"}]},
                  {"type": "text", "text": '{"found": true, "facts": []}'}]
        server = FakeServer(anthropic_stream(first, "pause_turn"), anthropic_stream(second))
        model = AnthropicLLM("claude-sonnet-5-5", http_client=server.client())
        text, docs = model.research("sys", "gap")
        sent = [r["body"] for r in server.requests]
        self.assertEqual([t["type"] for t in sent[0]["tools"]],
                         ["web_search_20260209", "web_fetch_20260209"])
        self.assertEqual(sent[0]["tools"][0]["max_uses"], 30)
        # pause_turn: the paused turn goes back unchanged, after the user message
        self.assertEqual(sent[1]["messages"][0], {"role": "user", "content": "gap"})
        self.assertEqual(sent[1]["messages"][1]["role"], "assistant")
        self.assertEqual(sent[1]["messages"][1]["content"], first)
        self.assertEqual(sent[1]["tools"], sent[0]["tools"])
        self.assertTrue(text.endswith('{"found": true, "facts": []}'))
        self.assertEqual([(d["url"], d["text"]) for d in docs],
                         [("https://example.edu/tcp", PAGE), ("https://b.org/x", "a cited passage")])
        self.assertEqual(model.usage["calls"], 2)

    def test_anthropic_research_gives_up_after_continuations(self):
        paused = [anthropic_stream([], "pause_turn") for _ in range(6)]
        server = FakeServer(*paused)
        with self.assertRaisesRegex(llm.LLMError, "did not finish"):
            AnthropicLLM("claude-opus-5-5", http_client=server.client()).research("s", "u", depth=4)
        self.assertEqual(len(server.requests), 6)

    def test_openai_responses_web_search(self):
        answer = {"status": "completed", "usage": {
                      "input_tokens": 5, "output_tokens": 7, "total_tokens": 12,
                      "input_tokens_details": {"cached_tokens": 0},
                      "output_tokens_details": {"reasoning_tokens": 0}},
                  "output": [{"type": "web_search_call", "id": "ws_1", "status": "completed",
                              "action": {"type": "search", "query": "q"}},
                             {"type": "message", "id": "m1", "role": "assistant",
                              "status": "completed", "content": [{
                                  "type": "output_text", "text": '{"found": false, "facts": []}',
                                  "annotations": [{"type": "url_citation", "url": "https://c.org",
                                                   "title": "C", "start_index": 0,
                                                   "end_index": 3}]}]}]}
        server = FakeServer(responses_stream(answer))
        model = OpenAILLM("gpt-x", http_client=server.client())
        text, docs = model.research("sys", "gap", depth=9)
        sent = server.requests[0]
        self.assertEqual(sent["url"], "https://api.openai.com/v1/responses")
        payload = sent["body"]
        self.assertTrue(payload["stream"])
        self.assertEqual(payload["tools"][0]["type"], "web_search")
        self.assertEqual(payload["instructions"], "sys")
        self.assertEqual(payload["max_tool_calls"], 9)
        self.assertEqual(payload["include"], ["web_search_call.action.sources"])
        self.assertIn("max_output_tokens", payload)
        self.assertEqual(text, '{"found": false, "facts": []}')
        self.assertEqual(docs, [{"url": "https://c.org", "title": "C", "text": None}])
        self.assertEqual(model.usage["output_tokens"], 7)

    def test_openai_responses_incomplete_and_refusal(self):
        cut = {"status": "incomplete", "output": [],
               "incomplete_details": {"reason": "max_output_tokens"}}
        with self.assertRaisesRegex(llm.LLMError, "cut off"):
            OpenAILLM("gpt-x", http_client=FakeServer(responses_stream(cut)).client()
                      ).research("s", "u")
        refused = {"status": "completed", "output": [
            {"type": "message", "id": "m1", "role": "assistant", "status": "completed",
             "content": [{"type": "refusal", "refusal": "no"}]}]}
        with self.assertRaisesRegex(llm.LLMError, "declined"):
            OpenAILLM("gpt-x", http_client=FakeServer(responses_stream(refused)).client()
                      ).research("s", "u")

    def test_local_model_cannot_research(self):
        with self.assertRaises(llm.LLMError):
            llm.OpenAICompatLLM("m").research("s", "u")


class Terminology(unittest.TestCase):
    TERMS = [{"term": "rate", "definition": "bits per second sent", "confusable": ["bandwidth"]},
             {"term": "bit rate", "definition": "x", "confusable": []}]

    def test_longest_term_wins(self):
        pairs = enrich.occurrences([{"text": "The bit rate is fixed."},
                                    {"text": "Rates vary."}], self.TERMS)
        self.assertEqual(pairs, [{"sentence": 1, "term": "bit rate"},
                                 {"sentence": 2, "term": "rate"}])

    def test_swapped_synonym_is_flagged_unless_the_sources_use_it(self):
        script = [{"text": "The bandwidth is 5 Mb/s."}]
        issues = enrich.stray_terms(script, self.TERMS, "the rate is 5", {"rate"})
        self.assertEqual(issues[0]["type"], "term_swap")
        self.assertEqual(enrich.stray_terms(script, self.TERMS, "bandwidth and rate", {"rate"}),
                         [])

    def test_judge_must_cover_every_occurrence(self):
        pairs = [{"sentence": 1, "term": "rate"}, {"sentence": 2, "term": "rate"}]
        data = {"uses": [{"sentence": 1, "term": "rate", "status": "correct"}]}
        self.assertIn("sentence 2", enrich.validate_uses(data, pairs)[0])


class Loop(unittest.TestCase):
    """The pipeline with a gap, a researched fact and a termbase."""

    def handler(self, rewrite=None, uses="correct"):
        def handle(system, user):
            if "You find the gaps" in system:
                return json.dumps({"slide_ideas": [], "gaps": [
                    {"id": "G1", "kind": "skipped_step", "node": "1.2",
                     "detail": "how the timeout is set", "query": "TCP retransmission timeout"},
                    {"id": "G2", "kind": "undefined_term", "node": "1.2", "term": "timeout",
                     "detail": "never defined", "query": "timeout definition"}]})
            if "You build the termbase" in system:
                return json.dumps({"terms": [
                    {"term": "timeout", "definition": "the waiting time before a resend",
                     "depends_on": [], "confusable": ["delay"], "first_node": "1.2"}]})
            if "You research one gap" in system:
                found = "retransmission" in _between(user, "GAP")
                facts = [{"text": "The timeout is computed from the round-trip time.",
                          "url": "https://example.edu/tcp",
                          "quote": "it is computed from the measured round-trip time"}]
                return "Done.\n" + json.dumps({"found": found, "facts": facts if found else []})
            if "You check terminology" in system:
                pairs = json.loads(_between(user, "OCCURRENCES (JSON)"))
                return json.dumps({"uses": [dict(p, status=uses, detail="means delay here")
                                            for p in pairs]})
            if "strict reviewer" in system:
                return counting_verifier(system, user)
            if "You build the outline" in system:
                return json.dumps(OUTLINE)
            return (rewrite or self.rewrite)(user)
        return handle

    @staticmethod
    def rewrite(user, external=True):
        data = json.loads(good_rewrite(user))
        if "Sending" in _between(user, "TRANSCRIPT OF THIS PART").splitlines()[0]:
            data["terms_defined"] = ["window", "timeout"]
            if external:
                data["sections"][-1]["sentences"].append(
                    {"origin": "external", "source": "E1",
                     "text": "The lecture does not spell this out, but the timeout is computed "
                             "from the round-trip time."})
        return json.dumps(data)

    def go(self, handler):
        fake = FakeLLM(handler, documents=lambda s, u: [{"url": "https://example.edu/tcp",
                                                         "title": "TCP", "text": PAGE}])
        with tempfile.TemporaryDirectory() as tmp:
            p = TextPipeline(pathlib.Path(tmp), "en", fake, fake, load_prompts("en"),
                             log=silent, max_part_words=1000, workers=1, researcher=fake.research)
            o = p.run_outline(UNITS)
            p.run_coverage(o)
            p.run_termbase(o)
            p.run_research(o)
            return p, p.run_parts(o), fake

    def test_fact_is_researched_checked_and_taught(self):
        p, records, fake = self.go(self.handler())
        self.assertEqual([s["id"] for s in p.supplements], ["E1"])
        self.assertEqual(p.supplements[0]["node"], "1.2")
        self.assertFalse(p.research["G2"]["found"])
        self.assertFalse(records[0]["flagged"], records[0]["issues"])
        rewrite_prompt = next(u for s, u in fake.calls if "SUPPLEMENTS (JSON)" in u)
        self.assertIn('"define_here": true', rewrite_prompt)

    def test_untaught_supplement_blocks_and_feedback_asks_for_it(self):
        p, records, fake = self.go(self.handler(rewrite=lambda u: self.rewrite(u, False)))
        self.assertTrue(records[0]["flagged"])
        self.assertIn("gap", [i["type"] for i in records[0]["issues"]])

    def test_misused_term_blocks(self):
        p, records, _ = self.go(self.handler(uses="wrong"))
        self.assertIn("term_misuse", [i["type"] for i in records[0]["issues"]])
        self.assertTrue(records[0]["flagged"])

    def test_external_sentence_needs_a_known_source(self):
        shape = {"sections": ["2"], "preview_items": [], "section_previews": {}, "sources": []}
        data = {"preview": {"intro": "x", "items": []}, "recap": ["r"],
                "questions": [{"question": "q", "answer": "a"}],
                "sections": [{"node": "2", "sentences": [
                    {"origin": "external", "source": "E9", "text": "t"}]}]}
        self.assertIn("supplement", " ".join(validate_part(data, shape)))

    def test_dry_handler_answers_the_new_prompts(self):
        prompts = load_prompts("en")
        self.assertEqual(json.loads(dry_run_handler(prompts.termbase, "")), {"terms": []})
        self.assertFalse(json.loads(dry_run_handler(prompts.research, ""))["found"])


if __name__ == "__main__":
    unittest.main()


class ResearchLoop(unittest.TestCase):
    """The program runs searches and reads pages for a model that calls tools."""

    def setUp(self):
        env = mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "dk"}, clear=False)
        env.start()
        self.addCleanup(env.stop)

    def model(self, turns):
        """A DeepSeek client whose server answers with `turns` (assistant deltas), in order;
        `sent` collects the request bodies."""
        server = FakeServer(*[chat_stream(finish="tool_calls" if t.get("tool_calls") else "stop",
                                          **t) for t in turns])
        model = llm.HostedLLM("deepseek", "deepseek-flash", http_client=server.client())
        return model, server

    def call(self, i, name, **args):
        return {"id": f"c{i}", "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)}}

    def test_search_fetch_answer(self):
        final = '{"found": true, "facts": []}'
        model, server = self.model([
            {"content": None, "reasoning_content": "think",
             "tool_calls": [self.call(1, "web_search", query="tcp timeout")]},
            {"content": None, "reasoning_content": "think2",
             "tool_calls": [self.call(2, "fetch_page", url="https://e.edu/t")]},
            {"content": final, "reasoning_content": "done"}])
        search = lambda q: [{"url": "https://e.edu/t", "title": "T", "text": "snippet"}]
        text, docs = llm.research_loop(model, "sys", "gap", search, depth=10,
                                       fetch=lambda url: PAGE)
        sent = [r["body"] for r in server.requests]
        self.assertEqual(text, final)
        self.assertEqual([d["text"] for d in docs], ["snippet", PAGE])
        self.assertEqual(sent[0]["tools"][0]["function"]["name"], "web_search")
        # Thinking mode with tools: every earlier reasoning_content goes back, or DeepSeek
        # answers 400.
        for body in sent:
            self.assertEqual(body["thinking"], {"type": "enabled"})
            self.assertEqual(body["reasoning_effort"], "high")
        self.assertEqual(sent[1]["messages"][2]["reasoning_content"], "think")
        self.assertEqual(sent[1]["messages"][2]["tool_calls"][0]["function"]["name"], "web_search")
        self.assertEqual(sent[1]["messages"][3]["role"], "tool")
        self.assertEqual(sent[1]["messages"][3]["tool_call_id"], "c1")
        self.assertEqual([m.get("reasoning_content") for m in sent[2]["messages"]
                          if m["role"] == "assistant"], ["think", "think2"])
        self.assertEqual(model.base_url, "https://api.deepseek.com")

    def test_budget_used_up_forces_an_answer(self):
        looping = {"content": None, "reasoning_content": "r",
                   "tool_calls": [self.call(1, "web_search", query="q")]}
        model, server = self.model([looping, looping, {"content": "{}"}])
        text, _ = llm.research_loop(model, "s", "u", lambda q: [], depth=2)
        self.assertEqual(text, "{}")
        last = server.requests[-1]["body"]
        self.assertEqual(last["tool_choice"], "none")
        self.assertEqual([m.get("reasoning_content") for m in last["messages"]
                          if m["role"] == "assistant"], ["r", "r"])

    def test_search_engine_requests(self):
        seen = []

        def fake(url, payload, headers, timeout=30):
            seen.append((url, payload, headers))
            if "brave" in url:
                return {"web": {"results": [{"url": "u", "title": "t", "description": "d",
                                             "extra_snippets": ["e"]}]}}
            return {"results": [{"url": "u", "title": "t", "snippet": "s", "content": "c"}]}

        with mock.patch.object(web, "_json_request", fake):
            self.assertEqual(web.make_search("deepseek")("q")[0]["text"], "s")
            self.assertEqual(web.make_search("tavily", key="tk")("q")[0]["text"], "c")
            self.assertEqual(web.make_search("brave", key="bk")("q")[0]["text"], "d e")
            self.assertEqual(web.make_search("searxng", base_url="http://s.local")("q")[0]["text"],
                             "c")
        self.assertTrue(seen[0][0].endswith("/v1/tools/search"))
        self.assertEqual(seen[1][2]["authorization"], "Bearer tk")
        self.assertEqual(seen[2][2]["x-subscription-token"], "bk")
        self.assertIn("format=json", seen[3][0])
        with self.assertRaises(web.FetchError):
            web.make_search("tavily", key=None) if not os.environ.get("TAVILY_API_KEY") \
                else (_ for _ in ()).throw(web.FetchError("x"))

    def test_hosted_keys_stay_with_their_host(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "secret"}, clear=False):
            os.environ.pop("MOONSHOT_API_KEY", None)
            with self.assertRaises(llm.LLMError):
                llm.make_llm("kimi", "k2")
        with self.assertRaises(llm.LLMError):
            llm.make_llm("compat", "m")  # no --base-url
