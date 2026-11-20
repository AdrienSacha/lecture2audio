"""Language-model clients on the official SDKs.

Providers:
- `AnthropicLLM`: Claude through the `anthropic` SDK (streamed Messages API, server-side
  web search and fetch for research, server-side refusal fallback).
- `OpenAILLM`: OpenAI through the `openai` SDK (streamed Chat Completions for text, the
  Responses API with its web_search tool for research).
- `HostedLLM`: any other hosted OpenAI-compatible API (DeepSeek, Kimi, OpenRouter, or
  --llm compat), through the `openai` SDK with that provider's own key and address.
- `OpenAICompatLLM`: a local OpenAI-compatible server (Ollama, LM Studio, llama.cpp
  server, vLLM). These servers document `max_tokens`, not `max_completion_tokens`.
- `FakeLLM`: for tests and dry runs.

The SDKs are imported when a client is built, so dry runs and tests that use `FakeLLM`
do not load them. Retries (connection errors, 408, 409, 429, 5xx, honouring
`retry-after`) are the SDKs' own; every request streams, so long answers never hit a
read timeout.
"""
from __future__ import annotations

import base64
import contextlib
import email.utils
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Callable

from . import trace


class LLMError(RuntimeError):
    """A failed model call. `status` and `body` are set for HTTP errors."""

    def __init__(self, message: str, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


MAX_TOKENS = 16000
# Thinking and reasoning tokens count inside the output limit (Claude's max_tokens,
# OpenAI's max_completion_tokens, DeepSeek's max_tokens). OpenAI's reasoning guide
# recommends reserving at least 25,000 tokens for reasoning and output.
REASONING_ROOM = 32000
MAX_RETRIES = 4  # passed to the SDKs, which back off exponentially and honour retry-after

ANTHROPIC_URL = "https://api.anthropic.com"
OPENAI_URL = "https://api.openai.com/v1"
LOCAL_URL = "http://localhost:11434/v1"  # Ollama; LM Studio uses :1234/v1, llama.cpp :8080/v1
# Models that accept the server-side refusal fallback (`fallbacks: "default"`, beta
# server-side-fallback-2026-07-01) on the Claude API.
FALLBACK_MODELS = {"claude-fable-5-1", "claude-fable-5", "claude-opus-5-5", "claude-opus-5",
                   "claude-sonnet-5-5"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Models that take `output_config.effort` (Haiku 4.5 and older reject it). Claude Opus 5.5
# and Haiku 5.5 default to `medium`, so the level is always set explicitly.
EFFORT_MODELS = re.compile(r"claude-(fable|mythos|opus-5|sonnet-5|haiku-5|opus-4-[5-9]|sonnet-4-6)")
CUT_OFF = "The answer was cut off ({}). Use a smaller --max-part-words."


class LLM:
    """Base class. Subclasses call `_count` so that the report can show the cost of a run."""
    name = "llm"
    base_url = ""
    unreachable_hint = ""
    _passthrough: tuple = ()  # SDK errors that `_errors` lets through to the caller
    vision = False  # True when the model is known to read images (`read_image`)

    def __init__(self):
        self.usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0}
        self._lock = threading.Lock()

    def _count(self, input_tokens: int | None = 0, output_tokens: int | None = 0) -> None:
        with self._lock:
            self.usage["calls"] += 1
            self.usage["input_tokens"] += int(input_tokens or 0)
            self.usage["output_tokens"] += int(output_tokens or 0)

    def complete(self, system: str, user: str, max_tokens: int = MAX_TOKENS) -> str:
        raise NotImplementedError

    def read_image(self, system: str, user: str, png: bytes,
                   max_tokens: int = MAX_TOKENS) -> str:
        """Answer about one PNG image (a rendered slide)."""
        raise LLMError(f"{self.name} cannot read images.")

    def research(self, system: str, user: str, max_tokens: int = MAX_TOKENS,
                 depth: int = 30) -> tuple[str, list[dict]]:
        """Answer with web search. Returns (answer text, documents), where each document is
        {"url", "title", "text"}; "text" is the page or passage the provider returned, or None
        when the provider gives only the address (the caller then fetches the page)."""
        raise LLMError(f"{self.name} cannot search the web; research needs --llm anthropic "
                       "or openai.")

    @contextlib.contextmanager
    def _errors(self):
        """Turn the SDK's typed errors into LLMError with a readable message."""
        sdk = self._sdk
        try:
            yield
        except sdk.APIStatusError as exc:
            body = exc.body if exc.body is not None else ""
            text = body if isinstance(body, str) else json.dumps(body)
            raise LLMError(f"HTTP {exc.status_code} from {self.name}: "
                           f"{_error_message(body) or exc.message}", exc.status_code,
                           text) from None
        except sdk.APIConnectionError as exc:  # includes APITimeoutError
            raise LLMError(f"Could not reach {self.base_url} ({exc.__class__.__name__}: "
                           f"{exc}).{self.unreachable_hint}") from None
        except sdk.APIError as exc:  # an error event inside a stream
            raise LLMError(f"{self.name}: {exc.message}") from None
        except self._passthrough:
            raise
        except (getattr(sdk, "AnthropicError", None) or sdk.OpenAIError) as exc:
            raise LLMError(f"{self.name}: {exc}") from None  # e.g. no credentials found


def _error_message(body) -> str:
    """'type: message' from an API error body ({"error": {...}} or the error itself)."""
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError:
            return body[:500]
    if not isinstance(body, dict):
        return ""
    error = body.get("error", body)
    if isinstance(error, dict) and error.get("message"):
        kind = error.get("type") or error.get("code")
        return f"{kind}: {error['message']}" if kind else str(error["message"])
    return error if isinstance(error, str) else ""


# --- kept for tts.py, whose speech request is still plain urllib (no SDK covers it here)
RETRY_STATUS = (408, 409, 429, 500, 502, 503, 504, 529)
MAX_WAIT = 60.0


def retry_wait(headers, delay: float) -> float:
    """Seconds to wait before a retry: the `retry-after` header if any, else `delay`."""
    value = headers.get("retry-after") if headers is not None else None
    if value:
        try:
            return min(max(float(value), 0.0), MAX_WAIT)
        except ValueError:
            try:
                when = email.utils.parsedate_to_datetime(value)
                return min(max(when.timestamp() - time.time(), 0.0), MAX_WAIT)
            except (TypeError, ValueError):
                pass
    return delay


def should_retry(status: int, headers, body: str) -> bool:
    hint = (headers.get("x-should-retry") or "").lower() if headers is not None else ""
    if hint in ("true", "false"):
        return hint == "true"
    if status == 429 and "insufficient_quota" in body:
        return False  # OpenAI: no credit left; waiting does not help
    return status in RETRY_STATUS


# ------------------------------------------------------------------------------- Claude

def _anthropic_credentials() -> bool:
    """Whether the anthropic SDK can find credentials: a key or token in the environment,
    a profile, workload identity federation, or an `ant auth login` profile on disk."""
    env = os.environ
    if any(env.get(k) for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE",
                                "ANTHROPIC_IDENTITY_TOKEN", "ANTHROPIC_IDENTITY_TOKEN_FILE")):
        return True
    folder = Path(env.get("ANTHROPIC_CONFIG_DIR") or Path.home() / ".config" / "anthropic")
    return (folder / "configs").is_dir()


class AnthropicLLM(LLM):
    """Claude through the anthropic SDK, always streamed (beta namespace, for `fallbacks`).

    No `temperature` and no `thinking` field are sent: current models reject sampling
    parameters and disabled thinking, and run adaptive thinking when it is omitted. Depth
    is set with `output_config.effort`.
    """

    def __init__(self, model: str, api_key: str | None = None, base_url: str | None = None,
                 timeout: float = 600, fallbacks: bool = True, effort: str | None = "high",
                 max_retries: int = MAX_RETRIES, http_client=None):
        super().__init__()
        import anthropic
        self._sdk = anthropic
        self.name = f"anthropic:{model}"
        self.model = model
        self.base_url = (base_url or ANTHROPIC_URL).rstrip("/")
        api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not (api_key or _anthropic_credentials()):
            raise LLMError("ANTHROPIC_API_KEY is not set (export it in your shell, or sign in "
                           "with `ant auth login`).")
        # Server-side fallback exists only on the Claude API itself, not behind proxies.
        self.fallbacks = (fallbacks and model in FALLBACK_MODELS
                          and self.base_url == ANTHROPIC_URL)
        self.effort = effort if effort and EFFORT_MODELS.match(model) else None
        self.client = anthropic.Anthropic(api_key=api_key, base_url=self.base_url,
                                          timeout=timeout, max_retries=max_retries,
                                          http_client=http_client)

    def _send(self, system: str, messages: list, max_tokens: int, tools=None):
        params = {"model": self.model, "max_tokens": max_tokens + REASONING_ROOM,
                  "system": system, "messages": messages}
        if tools:
            params["tools"] = tools
        if self.effort:
            params["output_config"] = {"effort": self.effort}
        if self.fallbacks:
            params["betas"], params["fallbacks"] = [FALLBACK_BETA], "default"
        try:
            with trace.call("model", host=self.base_url, **params) as record, \
                    self._errors(), self.client.beta.messages.stream(**params) as stream:
                message = stream.get_final_message()
                record["answer"] = message
        except LLMError as exc:
            if not (self.fallbacks and exc.status == 400
                    and ("anthropic-beta" in exc.body or "fallbacks" in exc.body)):
                raise
            self.fallbacks = False  # the beta is not enabled for this organisation
            return self._send(system, messages, max_tokens, tools)
        self._count(message.usage.input_tokens, message.usage.output_tokens)
        stop = message.stop_reason
        if stop == "refusal":
            details = message.stop_details
            raise LLMError(f"{self.name} declined the request (refusal, category "
                           f"{getattr(details, 'category', None)}): "
                           f"{getattr(details, 'explanation', None) or ''}")
        if stop == "max_tokens":
            raise LLMError(CUT_OFF.format("max_tokens"))
        if stop == "model_context_window_exceeded":
            raise LLMError(CUT_OFF.format("context window exceeded"))
        return message

    @staticmethod
    def _text(message) -> str:
        return "".join(b.text for b in message.content if b.type == "text")

    vision = True

    def read_image(self, system: str, user: str, png: bytes,
                   max_tokens: int = MAX_TOKENS) -> str:
        image = {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                             "data": base64.standard_b64encode(png).decode()}}
        message = self._send(system, [{"role": "user", "content": [
            image, {"type": "text", "text": user}]}], max_tokens)
        return self._text(message)

    def complete(self, system: str, user: str, max_tokens: int = MAX_TOKENS) -> str:
        message = self._send(system, [{"role": "user", "content": user}], max_tokens)
        if message.stop_reason == "pause_turn":
            raise LLMError(f"{self.name} paused the turn (pause_turn) without any server tool.")
        return self._text(message)

    @staticmethod
    def search_tools(depth: int) -> list[dict]:
        """Server tools with dynamic filtering (current Opus and Sonnet models; no beta
        header). `depth` is the number of searches, and of page reads, allowed."""
        return [{"type": "web_search_20260209", "name": "web_search", "max_uses": depth},
                {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": depth,
                 "citations": {"enabled": True}, "max_content_tokens": 30000}]

    def research(self, system: str, user: str, max_tokens: int = MAX_TOKENS,
                 depth: int = 30) -> tuple[str, list[dict]]:
        messages = [{"role": "user", "content": user}]
        documents: list[dict] = []
        # Each continuation resumes a server loop of about ten tool steps.
        continuations = max(5, depth // 4)
        for _ in range(continuations + 1):
            message = self._send(system, messages, max_tokens, self.search_tools(depth))
            blocks = [block.to_dict() for block in message.content]
            documents += anthropic_documents(blocks)
            if message.stop_reason != "pause_turn":
                return self._text(message), documents
            # The server-side loop paused: send its turn back unchanged and it resumes.
            # Consecutive assistant messages are combined by the API into one turn.
            messages = messages + [{"role": "assistant", "content": blocks}]
        raise LLMError(f"{self.name}: web research did not finish after "
                       f"{continuations} continuations.")


def anthropic_documents(blocks: list[dict]) -> list[dict]:
    """Pages read by web_fetch, and passages cited from web_search results."""
    documents = []
    for block in blocks:
        kind = block.get("type")
        if kind == "web_fetch_tool_result":
            result = block.get("content") or {}
            if result.get("type") != "web_fetch_result":
                continue  # an error object, e.g. {"type": "web_fetch_tool_error", ...}
            document = result.get("content") or {}
            source = document.get("source") or {}
            text = source.get("data") if source.get("type") == "text" else None
            documents.append({"url": result.get("url", ""), "title": document.get("title") or "",
                              "text": text})
        elif kind == "text":
            for citation in block.get("citations") or []:
                if citation.get("url") and citation.get("cited_text"):
                    documents.append({"url": citation["url"], "title": citation.get("title") or "",
                                      "text": citation["cited_text"]})
    return documents


# ------------------------------------------------------------- OpenAI-compatible APIs

class OpenAICompatLLM(LLM):
    """A local OpenAI-compatible server through the openai SDK.

    Ollama, LM Studio, llama.cpp server and vLLM all document `max_tokens`. No API key is
    needed; L2A_LOCAL_API_KEY is sent if set (llama.cpp --api-key, vLLM --api-key).
    OPENAI_API_KEY is never sent here, so it cannot leak to another host.
    """
    provider = "local"
    token_param = "max_tokens"
    system_role = "system"
    reasoning_room = 0
    extra: dict = {}  # provider-specific request fields (extra_body), added to every request
    unreachable_hint = (" Is the local server running? (Ollama: `ollama serve`, port 11434; "
                        "LM Studio: port 1234; llama.cpp: port 8080; vLLM: port 8000; set the "
                        "address with --base-url.)")
    NO_KEY = "not-needed"  # the SDK needs a key; local servers ignore it

    def __init__(self, model: str, base_url: str = LOCAL_URL, api_key: str | None = None,
                 timeout: float = 1800, max_retries: int = MAX_RETRIES, http_client=None):
        super().__init__()
        import openai
        self._sdk = openai
        self.name = f"{self.provider}:{model}"
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key if api_key is not None else self._key()
        if not self.api_key and self.provider != "local":
            raise LLMError(f"{self.key_env} is not set (export it in your shell).")
        # The key is always given explicitly, so the SDK never falls back to OPENAI_API_KEY;
        # the organisation and project headers it would read from the environment are
        # dropped for every host but OpenAI's.
        headers = ({} if self.provider == "openai" else
                   {"OpenAI-Organization": openai.omit, "OpenAI-Project": openai.omit})
        self._passthrough = (openai.LengthFinishReasonError,
                             openai.ContentFilterFinishReasonError)
        self.client = openai.OpenAI(api_key=self.api_key or self.NO_KEY, base_url=self.base_url,
                                    timeout=timeout, max_retries=max_retries,
                                    default_headers=headers, http_client=http_client)

    key_env = "L2A_LOCAL_API_KEY"

    def _key(self) -> str | None:
        return os.environ.get(self.key_env)

    def _wrong_token_param(self, exc: LLMError) -> bool:
        body = exc.body.lower()
        return exc.status == 400 and self.token_param in body and any(
            w in body for w in ("unsupported", "not supported", "unknown", "unrecognized",
                                "extra"))

    def _completion(self, messages: list[dict], max_tokens: int, **params):
        """One streamed Chat Completions call; returns the first choice. A server that
        knows only the other token-limit parameter makes the client switch once."""
        sdk = self._sdk
        for attempt in (0, 1):
            request = {"model": self.model, "messages": messages,
                       self.token_param: max_tokens + self.reasoning_room,
                       "stream_options": {"include_usage": True}, **params}
            if self.extra:
                request["extra_body"] = self.extra
            try:
                with trace.call("model", host=self.base_url, **request) as record, \
                        self._errors(), self.client.chat.completions.stream(**request) as stream:
                    completion = stream.get_final_completion()
                    record["answer"] = completion
            except LLMError as exc:
                if attempt or not self._wrong_token_param(exc):
                    raise
                self.token_param = ("max_tokens" if self.token_param == "max_completion_tokens"
                                    else "max_completion_tokens")
                continue
            except sdk.LengthFinishReasonError as exc:
                self._count_usage(exc.completion)
                raise LLMError(CUT_OFF.format(self.token_param)) from None
            except sdk.ContentFilterFinishReasonError as exc:
                self._count_usage(exc.completion)
                raise LLMError(f"{self.name}: the answer was withheld by the content "
                               "filter.") from None
            self._count_usage(completion)
            if not completion.choices:
                raise LLMError(f"{self.name} answered without choices.")
            message = completion.choices[0].message
            if message.refusal:
                raise LLMError(f"{self.name} declined the request: {message.refusal}")
            return message
        raise LLMError("unreachable")

    def _count_usage(self, completion) -> None:
        usage = completion.usage if completion is not None else None
        self._count(usage and usage.prompt_tokens, usage and usage.completion_tokens)

    def complete(self, system: str, user: str, max_tokens: int = MAX_TOKENS) -> str:
        message = self._completion([{"role": self.system_role, "content": system},
                                    {"role": "user", "content": user}], max_tokens)
        return strip_reasoning(message.content or "")

    def read_image(self, system: str, user: str, png: bytes,
                   max_tokens: int = MAX_TOKENS) -> str:
        """Chat Completions with an image part. Whether the model reads images depends on
        the model served: `vision` is True only where it is known (OpenAI)."""
        address = "data:image/png;base64," + base64.standard_b64encode(png).decode()
        message = self._completion([
            {"role": self.system_role, "content": system},
            {"role": "user", "content": [{"type": "text", "text": user},
                                         {"type": "image_url", "image_url": {"url": address}}]}],
            max_tokens)
        return strip_reasoning(message.content or "")

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             tool_choice: str = "auto", max_tokens: int = MAX_TOKENS) -> dict:
        """One Chat Completions turn with function tools. Returns the assistant message as a
        dict: role, content, any `tool_calls`, and the reasoning fields (`reasoning_content`
        for DeepSeek and Kimi, `reasoning_details` for OpenRouter) that some servers require
        back in the next turn."""
        params = {"tools": tools, "tool_choice": tool_choice} if tools else {}
        message = self._completion(messages, max_tokens, **params)
        out = {"role": "assistant", "content": message.content}
        if message.tool_calls:
            out["tool_calls"] = [{"id": c.id, "type": "function",
                                  "function": {"name": c.function.name,
                                               "arguments": c.function.arguments}}
                                 for c in message.tool_calls if c.type == "function"]
        extra = message.model_extra or {}
        for key in ("reasoning_content", "reasoning_details"):
            if extra.get(key) is not None:
                out[key] = extra[key]
        return out


class HostedLLM(OpenAICompatLLM):
    """A hosted OpenAI-compatible API (DeepSeek, Kimi, OpenRouter, or any other with
    --llm compat). Each has its own key variable, so no key is sent to another host."""
    unreachable_hint = ""

    def __init__(self, provider: str, model: str, base_url: str | None = None,
                 timeout: float = 1800, http_client=None):
        default_url, self.key_env = PRESETS[provider]
        self.provider = provider
        if not (base_url or default_url):
            raise LLMError("--llm compat needs --base-url (the API address, ending in /v1).")
        if provider == "deepseek":
            # Thinking mode at high effort; its tokens count in max_tokens, so leave room.
            # With tools, every reply's reasoning_content must be sent back (research_loop
            # does), or the API answers 400.
            self.extra = {"thinking": {"type": "enabled"}, "reasoning_effort": "high"}
            self.reasoning_room = REASONING_ROOM
        super().__init__(model, base_url or default_url, None, timeout, http_client=http_client)


# Hosted OpenAI-compatible providers: (default base URL, API key variable).
PRESETS = {"deepseek": ("https://api.deepseek.com", "DEEPSEEK_API_KEY"),
           "kimi": ("https://api.moonshot.ai/v1", "MOONSHOT_API_KEY"),
           "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
           "compat": (None, "L2A_COMPAT_API_KEY")}


class OpenAILLM(OpenAICompatLLM):
    """OpenAI. `max_tokens` is deprecated there and rejected by reasoning models, so
    `max_completion_tokens` is sent, with room for the reasoning tokens."""
    provider = "openai"
    vision = True
    token_param = "max_completion_tokens"
    reasoning_room = REASONING_ROOM
    key_env = "OPENAI_API_KEY"
    unreachable_hint = ""

    def __init__(self, model: str, base_url: str = OPENAI_URL, api_key: str | None = None,
                 timeout: float = 900, http_client=None):
        super().__init__(model, base_url, api_key, timeout, http_client=http_client)
        # o1 and later models take `developer` messages in place of `system` ones.
        if self.base_url == OPENAI_URL:
            self.system_role = "developer"

    def research(self, system: str, user: str, max_tokens: int = MAX_TOKENS,
                 depth: int = 30) -> tuple[str, list[dict]]:
        """Responses API with the web_search tool, streamed. It returns cited addresses,
        not page text."""
        final = None
        with self._errors():
            with self.client.responses.create(
                    model=self.model, instructions=system, input=user, stream=True,
                    tools=[{"type": "web_search", "search_context_size": "high"}],
                    include=["web_search_call.action.sources"], max_tool_calls=depth,
                    max_output_tokens=max_tokens + self.reasoning_room) as stream:
                for event in stream:
                    if event.type in ("response.completed", "response.incomplete",
                                      "response.failed"):
                        final = event.response
        if final is None:
            raise LLMError(f"{self.name}: the response stream ended without a final response.")
        out = final.to_dict()
        usage = out.get("usage") or {}
        self._count(usage.get("input_tokens"), usage.get("output_tokens"))
        if out.get("status") == "incomplete":
            reason = (out.get("incomplete_details") or {}).get("reason")
            if reason == "max_output_tokens":
                raise LLMError(CUT_OFF.format("max_output_tokens"))
            raise LLMError(f"{self.name}: the answer is incomplete ({reason}).")
        if out.get("error"):
            raise LLMError(f"{self.name}: {_error_message(out['error']) or out['error']}")
        return openai_response_text(self.name, out)


def openai_response_text(name: str, out: dict) -> tuple[str, list[dict]]:
    """Answer text and cited addresses of a Responses API result (as a dict)."""
    text, documents = [], []
    for item in out.get("output") or []:
        if item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if part.get("type") == "refusal":
                raise LLMError(f"{name} declined the request: {part.get('refusal')}")
            if part.get("type") != "output_text":
                continue
            text.append(part.get("text", ""))
            for note in part.get("annotations") or []:
                if note.get("type") == "url_citation" and note.get("url"):
                    documents.append({"url": note["url"], "title": note.get("title") or "",
                                      "text": None})
    return strip_reasoning("".join(text)), documents


class FakeLLM(LLM):
    """For tests: `handler(system, user) -> str`. `documents(system, user)`, if given, is
    what `research` returns as the pages it read."""

    def __init__(self, handler: Callable[[str, str], str], documents=None):
        super().__init__()
        self.name = "fake"
        self.handler = handler
        self.documents = documents
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str, max_tokens: int = MAX_TOKENS) -> str:
        with self._lock:
            self.calls.append((system, user))
        self._count()
        return self.handler(system, user)

    def research(self, system: str, user: str, max_tokens: int = MAX_TOKENS,
                 depth: int = 30) -> tuple[str, list[dict]]:
        text = self.complete(system, user, max_tokens)
        return text, (self.documents(system, user) if self.documents else [])


def make_llm(provider: str, model: str, base_url: str | None = None) -> LLM:
    if provider == "anthropic":
        return AnthropicLLM(model, base_url=base_url)
    if provider == "openai":
        return OpenAILLM(model, base_url=base_url or OPENAI_URL)
    if provider == "local":
        return OpenAICompatLLM(model, base_url=base_url or LOCAL_URL)
    if provider in PRESETS:
        return HostedLLM(provider, model, base_url)
    raise ValueError(f"Unknown LLM provider: {provider}")


# ------------------------------------------------------------- research run by the program

LOOP_TOOLS = [
    {"type": "function", "function": {
        "name": "web_search",
        "description": "Search the web. Returns, for each result, its address, its title and "
                       "a passage. Use precise queries with the field's exact terms.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}},
                       "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "fetch_page",
        "description": "Read the text of a web page, usually one found with web_search. "
                       "Quotes must be copied from pages you read.",
        "parameters": {"type": "object", "properties": {"url": {"type": "string"}},
                       "required": ["url"]}}},
]
PAGE_CHARS = 20000
# Fields of an assistant turn that go back to the server: DeepSeek answers 400 when a
# reasoning_content of an earlier turn is missing in a conversation with tools.
TURN_FIELDS = ("role", "content", "tool_calls", "reasoning_content", "reasoning_details")


def research_loop(llm: "OpenAICompatLLM", system: str, user: str, search, depth: int = 30,
                  fetch=None) -> tuple[str, list[dict]]:
    """Web research for any model that can call functions: the model asks for searches and
    pages, the program runs them. `depth` is the number of tool calls allowed. Returns
    (final answer, documents read), like `LLM.research`."""
    from .web import FetchError, fetch_page
    fetch = fetch or fetch_page
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    documents: list[dict] = []
    used = 0
    while used < depth:
        message = llm.chat(messages, LOOP_TOOLS)
        messages.append({k: v for k, v in message.items() if k in TURN_FIELDS})
        calls = message.get("tool_calls") or []
        if not calls:
            return strip_reasoning(message.get("content") or ""), documents
        for call in calls:
            used += 1
            function = call.get("function") or {}
            try:
                args = json.loads(function.get("arguments") or "{}")
            except ValueError:
                args = {}
            try:
                if function.get("name") == "web_search":
                    results = search(str(args.get("query", ""))[:400])
                    documents += [dict(r, text=r.get("text") or None) for r in results]
                    result = json.dumps(results, ensure_ascii=False)
                elif function.get("name") == "fetch_page":
                    url = str(args.get("url", ""))
                    text = fetch(url)
                    documents.append({"url": url, "title": "", "text": text})
                    result = text[:PAGE_CHARS] + ("\n[page cut here]" if len(text) > PAGE_CHARS
                                                 else "")
                else:
                    result = f"error: unknown tool {function.get('name')}"
            except FetchError as exc:
                result = f"error: {exc}"
            messages.append({"role": "tool", "tool_call_id": call.get("id", ""),
                             "content": result})
    messages.append({"role": "user", "content": "The research budget is used up. Give your final "
                     "answer now, in the required JSON, using only the pages you read."})
    message = llm.chat(messages, LOOP_TOOLS, tool_choice="none")
    return strip_reasoning(message.get("content") or ""), documents


_THINK = re.compile(r"<(think|thinking|reasoning)>.*?</\1>", re.S | re.I)


def strip_reasoning(text: str) -> str:
    """Remove reasoning that local models print inside the answer (<think>...</think>).
    Some chat templates open the block in the prompt, so only `</think>` appears."""
    text = _THINK.sub("", text)
    match = list(re.finditer(r"</(?:think|thinking|reasoning)>", text, re.I))
    if match:
        text = text[match[-1].end():]
    return text.strip()


def extract_json(text: str) -> dict:
    """The first JSON object in a model answer, tolerating a code fence and a reasoning
    block around it."""
    t = strip_reasoning(text)
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in the model answer")
    return json.loads(t[start:end + 1])


def _between(text: str, heading: str) -> str:
    """The body of '## heading' in a user message, up to the next '## ' heading."""
    body = text.split(f"## {heading}\n", 1)[1]
    return re.split(r"\n## (?!#)", body, maxsplit=1)[0]


def _dry_ideas(transcript: str) -> list[dict]:
    """Pass-through: one idea per unit, the unit's own text."""
    ideas, current = [], None
    for line in transcript.splitlines():
        heading = re.match(r"^#{3,5} (\d+(?:\.\d+)*) ", line)
        if line.startswith("#"):
            current = heading.group(1) if heading else None
            continue
        unit = re.match(r"^\[(\d+)\] (.+)$", line)
        if unit and current:
            ideas.append({"kind": "claim", "idea": unit.group(2), "nodes": [current],
                          "units": [int(unit.group(1))]})
    return ideas


def _dry_outline(user: str) -> str:
    limit = int(re.search(r"At most (\d+) words per part", user).group(1))
    units = re.findall(r"^\[(\d+)\] (.*)$", _between(user, "UNITS"), flags=re.M)
    groups, current, words = [], [], 0
    for number, text in units:
        size = len(text.split())
        if current and words + size > limit:
            groups.append(current)
            current, words = [], 0
        current.append((int(number), text))
        words += size
    if current:
        groups.append(current)
    parts = []
    for group in groups:
        first, last = group[0][0], group[-1][0]
        title = " ".join(group[0][1].split()[:5])
        if len(group) >= 2:
            middle = group[len(group) // 2][0]
            children = [{"title": f"{title} (a)", "units": [[first, middle - 1]], "children": []},
                        {"title": f"{title} (b)", "units": [[middle, last]], "children": []}]
            parts.append({"title": title, "units": [], "children": children})
        else:
            parts.append({"title": title, "units": [[first, last]], "children": []})
    return json.dumps({"title": parts[0]["title"], "parts": parts, "noise": [], "terms": []})


def _dry_rewrite(user: str) -> str:
    shape = json.loads(_between(user, "REQUIRED STRUCTURE (JSON)"))
    ideas = (json.loads(_between(user, "IDEAS OF THIS PART (JSON)"))
             if "## IDEAS OF THIS PART (JSON)\n" in user else [])
    titles, texts, current = {}, {}, None
    for line in _between(user, "TRANSCRIPT OF THIS PART").splitlines():
        heading = re.match(r"^#{3,5} (\d+(?:\.\d+)*) (.*)$", line)
        if heading:
            current = heading.group(1)
            titles[current], texts[current] = heading.group(2), []
        elif line.startswith("#"):
            current = None
        elif current and (unit := re.match(r"^\[(\d+)\] (.*)$", line)):
            texts[current].append((int(unit.group(1)), unit.group(2)))
    def items(ids):
        return [{"node": i, "phrase": titles.get(i, i)} for i in ids]

    def cites(node, unit, first):
        """The ideas a pass-through sentence states: those citing its unit, and, on the first
        sentence of a node, those of the node that cite no unit."""
        return [i["id"] for i in ideas if node in i["nodes"]
                and (unit in (i.get("units") or []) or (first and not i.get("units")))]

    sections = []
    for node in shape["sections"]:
        sentences = [dict({"origin": "lecture", "text": t},
                          **({"ideas": cites(node, u, k == 0)} if ideas else {}))
                     for k, (u, t) in enumerate(texts.get(node, []))]
        if not sentences:
            sentences = [{"origin": "added", "text": f"{titles.get(node, node)}."}]
        section = {"node": node, "sentences": sentences}
        if node in shape["section_previews"]:
            section["preview"] = items(shape["section_previews"][node])
        sections.append(section)
    part = shape["sections"][0]
    first = next((t for n in shape["sections"] for _, t in texts.get(n, [])), titles.get(part, part))
    return json.dumps({"preview": {"intro": f"{titles.get(part, part)}.",
                                   "items": items(shape["preview_items"])},
                       "sections": sections, "recap": [first],
                       "questions": [{"question": "What does this part begin with?",
                                      "answer": first}],
                       "terms_defined": [], "reference": []})


def dry_run_handler(system: str, user: str) -> str:
    """Pass-through 'model' for smoke tests: keeps the text as it is and accepts everything."""
    if "You build the outline" in system:
        return _dry_outline(user)
    if "strict reviewer" in system:
        nodes = json.loads(_between(user, "CHECKLIST (JSON)"))["nodes"]
        ideas = (json.loads(_between(user, "IDEAS (JSON)")) if "## IDEAS (JSON)\n" in user
                 else [])
        return json.dumps({"points": [{"node": n["node"], "status": "complete", "detail": ""}
                                      for n in nodes], "issues": [],
                           "ideas": [{"idea": i["id"], "status": "taught", "detail": ""}
                                     for i in ideas]})
    if "exact words a voice should say" in system:
        return json.dumps({"blocks": json.loads(user)["blocks"]})
    if "You find the gaps" in system:
        first = re.search(r"^#{3,5} (\d+(?:\.\d+)*) ", user, flags=re.M).group(1)
        slides = sorted({int(n) for n in re.findall(r"^\[Slide (\d+)\]$", user, flags=re.M)})
        return json.dumps({"slide_ideas": [{"id": f"S{n}", "slide": n, "idea": f"slide {n}",
                                            "status": "covered", "nodes": [first]}
                                           for n in slides], "gaps": []})
    if "You list the atomic ideas" in system:
        return json.dumps({"ideas": _dry_ideas(_between(user, "TRANSCRIPT OF THIS PART"))})
    if "You build the termbase" in system:
        return json.dumps({"terms": []})
    if "You research one gap" in system:
        return json.dumps({"found": False, "facts": []})
    if "You check terminology" in system:
        pairs = json.loads(_between(user, "OCCURRENCES (JSON)"))
        return json.dumps({"uses": [dict(p, status="correct", detail="") for p in pairs]})
    if "You write the recitation notes" in system:
        from .recitation import dry_recitation
        return dry_recitation(user)
    if "You design the project" in system:
        from .project import dry_project
        return dry_project(user)
    if "You write the course material" in system:
        from .course import dry_course
        return dry_course(user)
    if "You read as a student" in system:
        return json.dumps({"loose": []})
    if "You check the course material" in system:
        from .course import dry_course_check
        return dry_course_check(user)
    return _dry_rewrite(user)


