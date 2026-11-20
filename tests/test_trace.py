"""calls.jsonl carries the context of every call (stage, part, role, attempt, conversation,
parent), in start and end lines, across worker threads, and never a key."""
import os
os.environ["L2A_KEYS_FILE"] = "off"
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from lecture2audio import llm, stages, trace
from lecture2audio.llm import FakeLLM, research_loop
from lecture2audio.outline import Node
from lecture2audio.pipeline import TextPipeline, load_prompts


def records(folder) -> list[dict]:
    return [json.loads(line) for line in (Path(folder) / "calls.jsonl").read_text().splitlines()]


class TraceCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        trace.open_run(self.tmp.name)

    def tearDown(self):
        trace.close()
        self.tmp.cleanup()

    def lines(self, kind=None):
        return [r for r in records(self.tmp.name) if kind is None or r["kind"] == kind]


class TestContext(TraceCase):
    def test_start_and_end_lines_share_id_and_context(self):
        trace.set_stage("rewrite")
        model = FakeLLM(lambda s, u: "{}")
        with trace.context(part="3", part_title="Masks", attempt=2):
            model.complete("You are a strict reviewer of a script.", "the script")
        start, end = self.lines("model")
        self.assertEqual((start["phase"], end["phase"]), ("start", "end"))
        self.assertEqual(start["id"], end["id"])
        for line in (start, end):
            self.assertEqual((line["stage"], line["part"], line["attempt"]), ("rewrite", "3", 2))
            self.assertEqual((line["prompt"], line["role"]), ("verify", "checker"))
        self.assertEqual(start["request"]["messages"][0]["content"], "the script")
        self.assertEqual(end["answer"], "{}")
        self.assertNotIn("answer", start)
        self.assertEqual(self.lines("run")[0]["phase"], "open")

    def test_tracker_sets_the_stage_and_stamps_events(self):
        tracker = stages.Tracker(self.tmp.name)
        tracker("outline")
        self.assertEqual(trace.current()["stage"], "outline")
        tracker.finish()
        self.assertNotIn("stage", trace.current())
        events = [json.loads(l) for l in (Path(self.tmp.name) / "events.jsonl").read_text().splitlines()]
        self.assertTrue(all(isinstance(e["time"], float) for e in events))

    def test_error_is_written_and_context_is_restored(self):
        with self.assertRaises(ValueError), trace.context(role="writer"):
            with trace.call("model", model="m"):
                raise ValueError("boom")
        self.assertEqual(trace.current().get("role"), None)
        end = self.lines("model")[-1]
        self.assertEqual(end["phase"], "end")
        self.assertIn("ValueError: boom", end["error"])

    def test_parallel_parts_keep_their_own_context(self):
        pipeline = TextPipeline.__new__(TextPipeline)
        pipeline.workers = 4
        parts = [Node(str(k), f"Part {k}", 1, []) for k in range(1, 9)]
        barrier = threading.Barrier(4)
        model = FakeLLM(lambda s, u: u)

        def one(part):
            try:
                barrier.wait(timeout=2)  # four threads at once
            except threading.BrokenBarrierError:
                pass
            with trace.context(attempt=1):
                return model.complete("You list the atomic ideas of a part.", part.id)

        with trace.context(audit=None, seat="writer"):
            pipeline._map(one, parts)
        ends = [r for r in self.lines("model") if r["phase"] == "end"]
        self.assertEqual(len(ends), 8)
        for line in ends:
            self.assertEqual(line["part"], line["answer"])  # the call made for that part
            self.assertEqual(line["seat"], "writer")  # the caller's context reached the thread
            self.assertEqual(line["role"], "idea lister")
        self.assertNotIn("part", trace.current())

    def test_ask_json_names_the_prompt_and_writes_the_gates(self):
        answers = iter(["not json", '{"ok": 1}'])
        writer, checker = FakeLLM(lambda s, u: next(answers)), FakeLLM(lambda s, u: "{}")
        pipeline = TextPipeline(Path(self.tmp.name), "en", writer, checker, load_prompts("en"))
        data = pipeline._ask_json(writer, pipeline.prompts.outline, "units", lambda d: [], "outline")
        self.assertEqual(data, {"ok": 1})
        models = [r for r in self.lines("model") if r["phase"] == "end"]
        self.assertEqual([m.get("try") for m in models], [None, 2])
        self.assertTrue(all(m["prompt"] == "outline" and m["role"] == "planner"
                            and m["seat"] == "writer" for m in models))
        gates = self.lines("gate")
        self.assertEqual([g["ok"] for g in gates], [False, True])
        self.assertEqual([g["parent"] for g in gates], [m["id"] for m in models])
        self.assertIn("not valid JSON", gates[0]["errors"][0])
        pipeline._ask_json(checker, pipeline.prompts.verify, "x", lambda d: [], "verify")
        self.assertEqual(self.lines("model")[-1]["seat"], "checker")


class TestResearchLoop(TraceCase):
    def test_turns_and_tools_are_linked(self):
        class Chat:
            def __init__(self):
                self.turn = 0

            def chat(self, messages, tools=None, tool_choice="auto", max_tokens=0):
                self.turn += 1
                with trace.call("model", model="m", messages=messages) as record:
                    if self.turn == 1:
                        record["answer"] = "search"
                        return {"role": "assistant", "content": "", "tool_calls": [
                            {"id": "a", "type": "function", "function": {
                                "name": "web_search", "arguments": '{"query": "bit masks"}'}},
                            {"id": "b", "type": "function", "function": {
                                "name": "fetch_page", "arguments": '{"url": "https://x.org/p"}'}}]}
                    record["answer"] = "done"
                    return {"role": "assistant", "content": '{"found": false}'}

        def search(query):
            with trace.call("search", payload={"query": query}) as record:
                record["answer"] = {"results": []}
            return []

        def fetch(url):
            with trace.call("page", url=url):
                return "text"

        text, _ = research_loop(Chat(), "You research one gap.", "gap", search, 10, fetch)
        self.assertEqual(text, '{"found": false}')
        ends = {r["kind"] + str(r.get("turn")): r for r in self.lines() if r.get("phase") == "end"}
        first, second = ends["model1"], ends["model2"]
        self.assertEqual(first["conversation"], second["conversation"])
        self.assertEqual(second["parent"], first["id"])
        self.assertEqual(ends["search1"]["parent"], first["id"])
        self.assertEqual(ends["search1"]["tool"], "web_search")
        self.assertEqual(ends["page1"]["parent"], first["id"])
        self.assertEqual(first["role"], "researcher")


class TestKeysAndProgress(TraceCase):
    def test_progress_lines_are_throttled_and_redacted(self):
        key = "sk-progress-secret-99"
        delta = SimpleNamespace(type="text_delta", text=f"part of the answer {key} ")
        event = SimpleNamespace(type="content_block_delta", delta=delta)
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": key}), \
                mock.patch.object(trace, "PROGRESS_EVERY", 0):
            with trace.call("model", model="m", system=f"system {key}") as record:
                for _ in range(3):
                    record.feed(event)
                record["answer"] = {"content": [{"type": "text", "text": key}]}
        text = (Path(self.tmp.name) / "calls.jsonl").read_text()
        self.assertNotIn(key, text)
        progress = [r for r in self.lines("model") if r["phase"] == "progress"]
        self.assertEqual(len(progress), 3)
        self.assertEqual(progress[-1]["chars"], 3 * len(delta.text))
        self.assertIn("[key]", progress[-1]["tail"])
        with mock.patch.object(trace, "PROGRESS_EVERY", 60):
            with trace.call("model", model="m") as record:
                for _ in range(50):
                    record.feed(event)
        self.assertEqual(len([r for r in self.lines("model") if r["phase"] == "progress"]), 3)

    def test_nothing_without_a_run(self):
        trace.close()
        with trace.call("model") as record:
            record.feed(SimpleNamespace(type="chunk"))
            self.assertIsNone(record.id)
        trace.note("gate", ok=True)


class TestSummary(unittest.TestCase):
    def test_shapes_of_answers(self):
        chat = {"choices": [{"message": {"content": "hi", "reasoning_content": "think",
                                         "tool_calls": [{"function": {"name": "web_search",
                                                                      "arguments": '{"query": "q"}'}}]}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2}}
        out = trace.summary({"kind": "model", "phase": "end", "id": "x", "answer": chat})
        self.assertEqual((out["tokens_in"], out["tokens_out"], out["says"]), (5, 2, "hi"))
        self.assertEqual(out["tools"], [{"name": "web_search", "query": "q"}])
        message = {"content": [{"type": "thinking", "thinking": "t"}, {"type": "text", "text": "a"},
                               {"type": "server_tool_use", "name": "web_fetch", "input": {"url": "u"}}],
                   "usage": {"input_tokens": 9, "output_tokens": 1}}
        out = trace.summary({"kind": "model", "phase": "end", "answer": message})
        self.assertEqual((out["tokens_in"], out["reasoning_chars"]), (9, 1))
        self.assertEqual(out["tools"], [{"name": "web_fetch", "url": "u"}])
        start = trace.summary({"kind": "model", "phase": "start", "request": {
            "model": "m", "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "ask"}]}})
        self.assertEqual((start["model"], start["asks"]), ("m", "ask"))
        search = trace.summary({"kind": "search", "phase": "end", "request": {
            "url": "https://api.search.brave.com/res/v1/web/search?q=bits"},
            "answer": {"web": {"results": [{"url": "https://a", "title": "A"}]}}})
        self.assertEqual(search["query"], "bits")
        self.assertEqual(search["results"][0]["url"], "https://a")


if __name__ == "__main__":
    unittest.main()
