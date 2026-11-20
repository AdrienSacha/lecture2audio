"""Model clients against a fake HTTP layer under the real SDKs: no network, no key.

The anthropic and openai SDKs take an `http_client`; the tests give them an httpx2 client
whose MockTransport records each request and answers with JSON or a server-sent event
stream, so request building, streaming, retries and error types are the SDKs' own."""
import io
import json
import os
import tempfile
import unittest
import urllib.error
from email.message import Message
from pathlib import Path
from unittest import mock

import httpx2

from lecture2audio import cli, llm, tts
from lecture2audio.llm import (AnthropicLLM, HostedLLM, LLMError, OpenAICompatLLM, OpenAILLM,
                               extract_json, make_llm, retry_wait, strip_reasoning)


# ---------------------------------------------------------------- fake server for the SDKs

def sse(events, done=False) -> httpx2.Response:
    """A server-sent event stream; `events` are (event name or None, data dict)."""
    body = "".join((f"event: {name}\n" if name else "") + f"data: {json.dumps(data)}\n\n"
                   for name, data in events) + ("data: [DONE]\n\n" if done else "")
    return httpx2.Response(200, headers={"content-type": "text/event-stream"},
                           content=body.encode())


def error(status, body, headers=None) -> httpx2.Response:
    return httpx2.Response(status, json=body, headers=headers or {})


def anthropic_stream(content, stop="end_turn", stop_details=None, usage=(10, 5)):
    """The stream of a Messages API answer whose final content is `content`."""
    message = {"id": "msg_1", "type": "message", "role": "assistant", "model": "m",
               "content": [], "stop_reason": None, "stop_sequence": None,
               "usage": {"input_tokens": usage[0], "output_tokens": 0}}
    events = [("message_start", {"type": "message_start", "message": message})]
    for i, block in enumerate(content):
        events += [("content_block_start", {"type": "content_block_start", "index": i,
                                            "content_block": block}),
                   ("content_block_stop", {"type": "content_block_stop", "index": i})]
    events += [("message_delta", {"type": "message_delta",
                                  "delta": {"stop_reason": stop, "stop_sequence": None,
                                            "stop_details": stop_details},
                                  "usage": {"output_tokens": usage[1]}}),
               ("message_stop", {"type": "message_stop"})]
    return sse(events)


def anthropic_answer(text="{}", stop="end_turn", **extra):
    return anthropic_stream([{"type": "thinking", "thinking": "", "signature": "sig"},
                             {"type": "text", "text": text}], stop, **extra)


def chat_stream(content="{}", finish="stop", usage=(7, 3), **delta):
    """The stream of a Chat Completions answer. `delta` adds message fields
    (reasoning_content, tool_calls, refusal)."""
    base = {"id": "c1", "object": "chat.completion.chunk", "created": 0, "model": "m"}
    first = {"role": "assistant", "content": content, **delta}
    if "tool_calls" in first:
        first["tool_calls"] = [dict(c, index=i) for i, c in enumerate(first["tool_calls"])]
    chunks = [dict(base, choices=[{"index": 0, "delta": first, "finish_reason": None}]),
              dict(base, choices=[{"index": 0, "delta": {}, "finish_reason": finish}])]
    if usage:
        chunks.append(dict(base, choices=[], usage={"prompt_tokens": usage[0],
                                                    "completion_tokens": usage[1],
                                                    "total_tokens": sum(usage)}))
    return sse([(None, c) for c in chunks], done=True)


def responses_stream(response):
    """The stream of a Responses API answer: created, then its final event."""
    kind = {"completed": "response.completed", "incomplete": "response.incomplete",
            "failed": "response.failed"}[response["status"]]
    full = {"id": "resp_1", "object": "response", "created_at": 0, "model": "m",
            "parallel_tool_calls": True, "tool_choice": "auto", "tools": [], **response}
    start = dict(full, status="in_progress", output=[])
    return sse([("response.created", {"type": "response.created", "sequence_number": 0,
                                      "response": start}),
                (kind, {"type": kind, "sequence_number": 1, "response": full})])


class FakeServer:
    """Answers the SDK's requests in order. Each answer is an httpx2.Response or an
    exception to raise (a connection error)."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append({"url": str(request.url), "headers": dict(request.headers),
                              "body": json.loads(request.content or b"null")})
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def client(self) -> httpx2.Client:
        return httpx2.Client(transport=httpx2.MockTransport(self))


# ---------------------------------------------------------------- urllib fakes (tts.py)

class FakeResponse:
    def __init__(self, body: bytes, content_type: str = "application/json"):
        self.body = body
        self.headers = {"content-type": content_type}

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(url, status, body, headers=None):
    message = Message()
    for key, value in (headers or {}).items():
        message[key] = value
    return urllib.error.HTTPError(url, status, "error", message,
                                  io.BytesIO(json.dumps(body).encode()))


class FakeHTTP:
    """Replaces urllib.request.urlopen. Each item of `answers` is a dict (JSON answer),
    a (bytes, content type) tuple, or an exception factory `lambda url: exc`."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append({"url": request.full_url, "timeout": timeout,
                              "headers": {k.lower(): v for k, v in request.header_items()},
                              "body": json.loads(request.data)})
        answer = self.answers.pop(0)
        if callable(answer):
            raise answer(request.full_url)
        if isinstance(answer, tuple):
            return FakeResponse(*answer)
        return FakeResponse(json.dumps(answer).encode())


class Base(unittest.TestCase):
    def setUp(self):
        self.sleeps = []
        patcher = mock.patch.object(llm.time, "sleep", self.sleeps.append)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "ak", "OPENAI_API_KEY": "ok"})
        env.start()
        self.addCleanup(env.stop)
        for name in ("L2A_LOCAL_API_KEY", "OPENAI_ORG_ID", "OPENAI_PROJECT_ID"):
            os.environ.pop(name, None)

    def claude(self, server, model="claude-opus-5-5", **options):
        return AnthropicLLM(model, http_client=server.client(), **options)

    def gpt(self, server, model="gpt-6.1-sol", **options):
        return OpenAILLM(model, http_client=server.client(), **options)

    def local(self, server, model="m", **options):
        return OpenAICompatLLM(model, http_client=server.client(), **options)


class TestAnthropic(Base):
    def test_payload_and_parsing(self):
        server = FakeServer(anthropic_answer('{"a": 1}'))
        model = self.claude(server, "claude-sonnet-5-5")
        self.assertEqual(model.complete("SYS", "USER"), '{"a": 1}')
        sent = server.requests[0]
        self.assertEqual(sent["url"], "https://api.anthropic.com/v1/messages")
        self.assertIn("anthropic-version", sent["headers"])
        self.assertEqual(sent["headers"]["authorization"], "Bearer ak")  # anthropic 2.x
        self.assertIn("server-side-fallback-2026-07-01", sent["headers"]["anthropic-beta"])
        body = sent["body"]
        self.assertTrue(body["stream"])
        self.assertEqual(body["system"], "SYS")
        self.assertEqual(body["messages"], [{"role": "user", "content": "USER"}])
        self.assertEqual(body["max_tokens"], llm.MAX_TOKENS + llm.REASONING_ROOM)
        self.assertEqual(body["fallbacks"], "default")
        self.assertEqual(body["output_config"], {"effort": "high"})
        for banned in ("temperature", "top_p", "top_k", "thinking"):
            self.assertNotIn(banned, body)
        self.assertEqual(model.usage, {"calls": 1, "input_tokens": 10, "output_tokens": 5})

    def test_no_fallback_for_other_models_or_proxies(self):
        server = FakeServer(anthropic_answer(), anthropic_answer())
        self.claude(server, "claude-haiku-4-5").complete("s", "u")
        AnthropicLLM("claude-opus-5-5", base_url="http://proxy.local",
                     http_client=server.client()).complete("s", "u")
        for sent in server.requests:
            self.assertNotIn("fallbacks", sent["body"])
            self.assertNotIn("anthropic-beta", sent["headers"])
        self.assertNotIn("output_config", server.requests[0]["body"])  # Haiku 4.5: no effort
        self.assertEqual(server.requests[1]["body"]["output_config"], {"effort": "high"})
        self.assertTrue(server.requests[1]["url"].startswith("http://proxy.local/v1/messages"))

    def test_fallback_beta_rejected_then_retried_without(self):
        body = {"type": "error", "error": {"type": "invalid_request_error", "message":
                "Unexpected value(s) `server-side-fallback-2026-07-01` for the `anthropic-beta` "
                "header."}}
        server = FakeServer(error(400, body), anthropic_answer("ok"))
        model = self.claude(server)
        self.assertEqual(model.complete("s", "u"), "ok")
        self.assertNotIn("fallbacks", server.requests[1]["body"])
        self.assertFalse(model.fallbacks)

    def test_stop_reasons(self):
        for stop, phrase in (("max_tokens", "cut off"), ("model_context_window_exceeded", "context"),
                             ("pause_turn", "pause_turn")):
            with self.assertRaisesRegex(LLMError, phrase):
                self.claude(FakeServer(anthropic_answer(stop=stop))).complete("s", "u")
        refusal = anthropic_answer("", stop="refusal", stop_details={
            "type": "refusal", "category": "bio", "explanation": "declined"})
        with self.assertRaisesRegex(LLMError, "declined.*bio"):
            self.claude(FakeServer(refusal)).complete("s", "u")

    def test_sdk_retries_overloaded(self):
        overloaded = {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}
        server = FakeServer(error(529, overloaded, {"retry-after-ms": "1"}),
                            error(429, overloaded, {"retry-after-ms": "1"}),
                            anthropic_answer("fine"))
        self.assertEqual(self.claude(server).complete("s", "u"), "fine")
        self.assertEqual(len(server.requests), 3)

    def test_client_error_not_retried(self):
        body = {"type": "error", "error": {"type": "not_found_error", "message": "model: x"}}
        server = FakeServer(error(404, body))
        with self.assertRaisesRegex(LLMError, "HTTP 404.*not_found_error: model: x") as caught:
            self.claude(server, "x").complete("s", "u")
        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(len(server.requests), 1)

    def test_x_should_retry_false_is_honoured(self):
        server = FakeServer(error(500, {}, {"x-should-retry": "false"}))
        with self.assertRaises(LLMError):
            self.claude(server).complete("s", "u")
        self.assertEqual(len(server.requests), 1)

    def test_network_errors_reported(self):
        server = FakeServer(*[httpx2.ConnectError("refused")] * 2)
        with self.assertRaisesRegex(LLMError, "Could not reach https://api.anthropic.com"):
            self.claude(server, max_retries=1).complete("s", "u")
        self.assertEqual(len(server.requests), 2)

    def test_missing_key(self):
        os.environ.pop("ANTHROPIC_API_KEY")
        with mock.patch.dict(os.environ, {"ANTHROPIC_CONFIG_DIR": "/nonexistent"}), \
                self.assertRaisesRegex(LLMError, "ANTHROPIC_API_KEY"):
            AnthropicLLM("claude-opus-5-5")


class TestOpenAI(Base):
    def test_payload_and_parsing(self):
        server = FakeServer(chat_stream('{"b": 2}'))
        model = self.gpt(server)
        self.assertEqual(model.complete("SYS", "USER"), '{"b": 2}')
        sent = server.requests[0]
        self.assertEqual(sent["url"], "https://api.openai.com/v1/chat/completions")
        self.assertEqual(sent["headers"]["authorization"], "Bearer ok")
        body = sent["body"]
        self.assertTrue(body["stream"])
        self.assertEqual(body["stream_options"], {"include_usage": True})
        self.assertNotIn("max_tokens", body)
        self.assertEqual(body["max_completion_tokens"], llm.MAX_TOKENS + llm.REASONING_ROOM)
        self.assertEqual(body["messages"][0], {"role": "developer", "content": "SYS"})
        self.assertNotIn("temperature", body)
        self.assertEqual(model.usage, {"calls": 1, "input_tokens": 7, "output_tokens": 3})
        self.assertEqual(model.name, "openai:gpt-6.1-sol")

    def test_requires_key(self):
        os.environ.pop("OPENAI_API_KEY")
        with self.assertRaisesRegex(LLMError, "OPENAI_API_KEY"):
            OpenAILLM("gpt-6.1-sol")

    def test_refusal_null_content_filter_and_cut_off(self):
        with self.assertRaisesRegex(LLMError, "declined.*can't help"):
            self.gpt(FakeServer(chat_stream(None, refusal="I can't help with that."))
                     ).complete("s", "u")
        with self.assertRaisesRegex(LLMError, "content filter"):
            self.gpt(FakeServer(chat_stream(None, finish="content_filter"))).complete("s", "u")
        model = self.gpt(FakeServer(chat_stream("partial", finish="length")))
        with self.assertRaisesRegex(LLMError, "cut off.*max_completion_tokens"):
            model.complete("s", "u")
        self.assertEqual(model.usage["calls"], 1)
        self.assertEqual(self.gpt(FakeServer(chat_stream(None))).complete("s", "u"), "")

    def test_switches_token_parameter_when_server_rejects_it(self):
        body = {"error": {"message": "Unsupported parameter: 'max_completion_tokens'.",
                          "type": "invalid_request_error"}}
        server = FakeServer(error(400, body), chat_stream("x"))
        model = self.gpt(server, "some-model", base_url="https://compat.example/v1")
        self.assertEqual(model.complete("s", "u"), "x")
        self.assertIn("max_tokens", server.requests[1]["body"])
        self.assertNotIn("max_completion_tokens", server.requests[1]["body"])
        self.assertEqual(server.requests[0]["body"]["messages"][0]["role"], "system")

    def test_insufficient_quota_not_retried(self):
        body = {"error": {"message": "You exceeded your current quota", "type": "insufficient_quota",
                          "code": "insufficient_quota"}}
        server = FakeServer(error(429, body, {"x-should-retry": "false"}))
        with self.assertRaisesRegex(LLMError, "quota"):
            self.gpt(server).complete("s", "u")
        self.assertEqual(len(server.requests), 1)


class TestLocal(Base):
    def test_payload_without_openai_key(self):
        os.environ["OPENAI_ORG_ID"] = "org-secret"
        server = FakeServer(chat_stream("<think>let me think {not json}</think>\n{\"c\": 3}"))
        model = self.local(server, "qwen3:8b")
        self.assertEqual(model.complete("SYS", "USER"), '{"c": 3}')
        sent = server.requests[0]
        self.assertEqual(sent["url"], "http://localhost:11434/v1/chat/completions")
        self.assertNotIn("ok", sent["headers"].get("authorization", ""))  # OPENAI_API_KEY
        self.assertNotIn("openai-organization", sent["headers"])
        self.assertEqual(sent["body"]["max_tokens"], llm.MAX_TOKENS)
        self.assertNotIn("max_completion_tokens", sent["body"])
        self.assertEqual(sent["body"]["messages"][0]["role"], "system")
        self.assertEqual(model.name, "local:qwen3:8b")

    def test_local_key_and_base_url(self):
        os.environ["L2A_LOCAL_API_KEY"] = "lk"
        server = FakeServer(chat_stream("{}"))
        OpenAICompatLLM("m", "http://localhost:8080/v1/", http_client=server.client()
                        ).complete("s", "u")
        self.assertEqual(server.requests[0]["url"], "http://localhost:8080/v1/chat/completions")
        self.assertEqual(server.requests[0]["headers"]["authorization"], "Bearer lk")

    def test_unreachable_server_hint(self):
        server = FakeServer(*[httpx2.ConnectError("Connection refused")] * 5)
        with self.assertRaisesRegex(LLMError, "ollama serve"):
            self.local(server, max_retries=0).complete("s", "u")

    def test_server_without_usage(self):
        model = self.local(FakeServer(chat_stream("{}", usage=None)))
        self.assertEqual(model.complete("s", "u"), "{}")
        self.assertEqual(model.usage, {"calls": 1, "input_tokens": 0, "output_tokens": 0})


class TestHosted(Base):
    def test_deepseek_thinking_payload(self):
        server = FakeServer(chat_stream("{}", reasoning_content="hmm"))
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "dk"}):
            model = HostedLLM("deepseek", "deepseek-flash", http_client=server.client())
        self.assertEqual(model.complete("s", "u"), "{}")
        sent = server.requests[0]
        self.assertEqual(sent["url"], "https://api.deepseek.com/chat/completions")
        self.assertEqual(sent["headers"]["authorization"], "Bearer dk")
        self.assertEqual(sent["body"]["thinking"], {"type": "enabled"})
        self.assertEqual(sent["body"]["reasoning_effort"], "high")
        self.assertEqual(sent["body"]["max_tokens"], llm.MAX_TOKENS + llm.REASONING_ROOM)
        self.assertEqual(sent["body"]["messages"][0]["role"], "system")

    def test_presets_send_only_their_own_key(self):
        keys = {"MOONSHOT_API_KEY": "mk", "OPENROUTER_API_KEY": "rk", "L2A_COMPAT_API_KEY": "ck",
                "OPENAI_ORG_ID": "org-secret"}
        cases = (("kimi", None, "https://api.moonshot.ai/v1/chat/completions", "mk"),
                 ("openrouter", None, "https://openrouter.ai/api/v1/chat/completions", "rk"),
                 ("compat", "https://x.example/v1", "https://x.example/v1/chat/completions", "ck"))
        with mock.patch.dict(os.environ, keys):
            for provider, url, expected, key in cases:
                server = FakeServer(chat_stream("{}"))
                HostedLLM(provider, "m", url, http_client=server.client()).complete("s", "u")
                sent = server.requests[0]
                self.assertEqual(sent["url"], expected)
                self.assertEqual(sent["headers"]["authorization"], f"Bearer {key}")
                self.assertNotIn("openai-organization", sent["headers"])
                self.assertNotIn("thinking", sent["body"])

    def test_chat_returns_tool_calls_and_reasoning(self):
        call = {"id": "c1", "type": "function",
                "function": {"name": "web_search", "arguments": '{"query": "q"}'}}
        server = FakeServer(chat_stream(None, finish="tool_calls", reasoning_content="why",
                                        tool_calls=[call]))
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "dk"}):
            model = HostedLLM("deepseek", "deepseek-flash", http_client=server.client())
        message = model.chat([{"role": "user", "content": "u"}], llm.LOOP_TOOLS)
        self.assertEqual(message, {"role": "assistant", "content": None, "tool_calls": [call],
                                   "reasoning_content": "why"})
        self.assertEqual(server.requests[0]["body"]["tool_choice"], "auto")
        self.assertEqual(server.requests[0]["body"]["tools"], llm.LOOP_TOOLS)


class TestHelpers(unittest.TestCase):
    def test_strip_reasoning(self):
        self.assertEqual(strip_reasoning("<think>a { b</think>answer"), "answer")
        self.assertEqual(strip_reasoning("reasoning without opening tag</think>\nanswer"), "answer")
        self.assertEqual(strip_reasoning("<THINKING>x</THINKING> y"), "y")
        self.assertEqual(strip_reasoning("plain"), "plain")

    def test_extract_json_after_think_and_fence(self):
        text = "<think>maybe {\"wrong\": 1}</think>\n```json\n{\"right\": 2}\n```"
        self.assertEqual(extract_json(text), {"right": 2})

    def test_retry_wait(self):
        self.assertEqual(retry_wait({"retry-after": "3"}, 2.0), 3.0)
        self.assertEqual(retry_wait({"retry-after": "9999"}, 2.0), llm.MAX_WAIT)
        self.assertEqual(retry_wait({}, 2.0), 2.0)
        self.assertEqual(retry_wait({"retry-after": "Wed, 21 Oct 2015 07:28:00 GMT"}, 2.0), 0.0)

    def test_make_llm_unknown(self):
        with self.assertRaises(ValueError):
            make_llm("other", "m")


class TestCliModels(Base):
    def args(self, *extra):
        return cli.build_parser().parse_args(["text", "x.txt", *extra])

    def test_defaults_per_provider(self):
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "dk"}):
            rewrite, verify = cli._llms(self.args())
        self.assertEqual((rewrite.name, verify.name),
                         ("deepseek:deepseek-flash", "deepseek:deepseek-v4-pro"))
        self.assertEqual(rewrite.extra["thinking"], {"type": "enabled"})
        rewrite, verify = cli._llms(self.args("--llm", "anthropic"))
        self.assertEqual((rewrite.name, verify.name),
                         ("anthropic:claude-sonnet-5-5", "anthropic:claude-opus-5-5"))
        rewrite, verify = cli._llms(self.args("--llm", "openai"))
        self.assertEqual((rewrite.name, verify.name), ("openai:gpt-6.1-sol", "openai:gpt-6-astra"))

    def test_cross_provider_verifier(self):
        rewrite, verify = cli._llms(self.args("--llm", "anthropic", "--verify-llm", "openai"))
        self.assertIsInstance(rewrite, AnthropicLLM)
        self.assertIsInstance(verify, OpenAILLM)
        self.assertEqual(verify.base_url, llm.OPENAI_URL)
        self.assertEqual(verify.model, "gpt-6-astra")
        rewrite, verify = cli._llms(self.args("--llm", "local", "--rewrite-model", "qwen3",
                                              "--base-url", "http://localhost:1234/v1",
                                              "--verify-llm", "anthropic"))
        self.assertEqual(rewrite.base_url, "http://localhost:1234/v1")
        self.assertEqual(verify.base_url, llm.ANTHROPIC_URL)

    def test_local_needs_a_model(self):
        with self.assertRaisesRegex(SystemExit, "ollama list"):
            cli._llms(self.args("--llm", "local"))
        rewrite, verify = cli._llms(self.args("--llm", "local", "--rewrite-model", "qwen3"))
        self.assertEqual(verify.model, "qwen3")
        with self.assertRaisesRegex(SystemExit, "--verify-model"):
            cli._llms(self.args("--verify-llm", "local"))


class TestOpenAITTS(Base):
    def test_payload_and_audio(self):
        fake = FakeHTTP((b"RIFF....WAVE", "audio/wav"))
        with mock.patch.object(tts.urllib.request, "urlopen", fake), \
                tempfile.TemporaryDirectory() as folder:
            out = Path(folder) / "a.wav"
            tts.OpenAITTS().synth("Hello.", out)
            self.assertEqual(out.read_bytes(), b"RIFF....WAVE")
        sent = fake.requests[0]
        self.assertEqual(sent["url"], "https://api.openai.com/v1/audio/speech")
        self.assertEqual(sent["body"]["model"], "gpt-4o-mini-tts")
        self.assertEqual(sent["body"]["voice"], "marin")
        self.assertEqual(sent["body"]["response_format"], "wav")
        self.assertIn("instructions", sent["body"])

    def test_old_model_has_no_instructions(self):
        payload = tts.OpenAITTS(model="tts-1").payload("Hi")
        self.assertEqual(payload["voice"], "alloy")
        self.assertNotIn("instructions", payload)

    def test_retry_then_error(self):
        fake = FakeHTTP(lambda url: http_error(url, 503, {}, {"retry-after": "1"}),
                        lambda url: http_error(url, 400, {"error": {"message": "bad voice"}}))
        with mock.patch.object(tts.urllib.request, "urlopen", fake), \
                tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(tts.TTSError, "400.*bad voice"):
                tts.OpenAITTS().synth("Hi", Path(folder) / "a.wav")
        self.assertEqual(self.sleeps, [1.0])

    def test_json_body_is_not_audio(self):
        fake = FakeHTTP((b'{"error": "x"}', "application/json"))
        with mock.patch.object(tts.urllib.request, "urlopen", fake), \
                tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(tts.TTSError, "no audio"):
                tts.OpenAITTS().synth("Hi", Path(folder) / "a.wav")


if __name__ == "__main__":
    unittest.main()


class TestTrace(unittest.TestCase):
    """calls.jsonl holds every request in full, never the key."""

    def test_model_call_is_written_in_full_without_the_key(self):
        from lecture2audio import trace
        with tempfile.TemporaryDirectory() as tmp:
            server = FakeServer(chat_stream("answer", reasoning_content="thinking"),
                                error(400, {"error": {"message": "bad"}}))
            key = "sk-secret-123456"
            with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": key}):
                model = HostedLLM("deepseek", "deepseek-flash", http_client=server.client())
                trace.open_run(tmp)
                try:
                    model.complete("system text", f"user text {key}")
                    with self.assertRaises(LLMError):
                        model.complete("s", "u")
                finally:
                    trace.close()
            text = (Path(tmp) / "calls.jsonl").read_text()
            self.assertNotIn(key, text)
            lines = [json.loads(line) for line in text.splitlines()]
            self.assertEqual(lines[0]["kind"], "run")
            start, end, _start2, second = [l for l in lines if l["kind"] == "model"
                                           and l["phase"] in ("start", "end")]
            self.assertEqual((start["phase"], end["phase"], start["id"]), ("start", "end", end["id"]))
            self.assertEqual(start["model"], "deepseek-flash")
            self.assertEqual(start["request"]["host"], "https://api.deepseek.com")
            self.assertEqual([m["content"] for m in start["request"]["messages"]],
                             ["system text", "user text [key]"])
            message = end["answer"]["choices"][0]["message"]
            self.assertEqual((message["content"], message["reasoning_content"]),
                             ("answer", "thinking"))
            self.assertEqual(end["answer"]["usage"]["prompt_tokens"], 7)
            self.assertIn("HTTP 400", second["error"])

    def test_streamed_events_are_counted_by_the_sdks_own_events(self):
        from lecture2audio import trace
        with tempfile.TemporaryDirectory() as tmp:
            server = FakeServer(chat_stream("hello world", reasoning_content="deep thought"),
                                anthropic_stream([{"type": "text", "text": ""}]))
            with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "k" * 12,
                                              "ANTHROPIC_API_KEY": "a" * 12}), \
                    mock.patch.object(trace, "PROGRESS_EVERY", 0):
                trace.open_run(tmp)
                try:
                    HostedLLM("deepseek", "deepseek-flash", http_client=server.client()).complete("s", "u")
                    AnthropicLLM("claude-opus-5-5", http_client=server.client()).complete("s", "u")
                finally:
                    trace.close()
            lines = [json.loads(l) for l in (Path(tmp) / "calls.jsonl").read_text().splitlines()]
            progress = [l for l in lines if l.get("phase") == "progress"]
            self.assertEqual((progress[0]["chars"], progress[0]["thinking"]),
                             (len("hello world"), len("deep thought")))
            ends = [l for l in lines if l.get("phase") == "end"]
            self.assertEqual([e["model"] for e in ends], ["deepseek-flash", "claude-opus-5-5"])

    def test_nothing_is_written_without_a_run(self):
        from lecture2audio import trace
        trace.close()
        with trace.call("model", host="h") as record:
            record["answer"] = "x"
        trace.write("model", host="h")  # no file, no error
