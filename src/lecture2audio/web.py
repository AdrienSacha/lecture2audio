"""Fetching public web pages and checking that a quote really appears in one.

Used by the research stage: a fact found online is kept only if its quote can be found, word
for word, in a page the program read itself (or in a passage the search provider returned).
Standard library only. Only public hosts are fetched, so a URL in a model answer cannot make
the program read a page on the local network.
"""
from __future__ import annotations

import html
import ipaddress
import re
import socket
import urllib.error
import urllib.parse
import urllib.request

MAX_BYTES = 3_000_000
USER_AGENT = "lecture2audio/0.1 (study tool; fetches a page to check a quote)"


class FetchError(RuntimeError):
    pass


def public_url(url: str) -> bool:
    """http(s) URL whose host resolves only to public addresses."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or 443, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError):
        return False
    for info in infos:
        address = ipaddress.ip_address(info[4][0].split("%")[0])
        if not address.is_global:
            return False
    return bool(infos)


class _CheckedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not public_url(newurl):
            raise FetchError(f"redirect to a non-public address refused: {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def html_to_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style|noscript|svg|head)\b.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h[1-6]|tr|section|article)>", "\n", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    text = html.unescape(raw)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def _pdf_text(data: bytes) -> str:
    try:
        import io

        from pypdf import PdfReader
    except ImportError as exc:
        raise FetchError("the page is a PDF and pypdf is not installed") from exc
    reader = PdfReader(io.BytesIO(data))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def fetch_page(url: str, timeout: float = 20) -> str:
    """The text of a public page (HTML, plain text, or PDF if pypdf is installed)."""
    if not public_url(url):
        raise FetchError(f"not a public http(s) address: {url}")
    opener = urllib.request.build_opener(_CheckedRedirect)
    request = urllib.request.Request(url, headers={"user-agent": USER_AGENT,
                                                   "accept": "text/html,text/plain,application/pdf"})
    try:
        with opener.open(request, timeout=timeout) as response:
            kind = (response.headers.get("content-type") or "").lower()
            data = response.read(MAX_BYTES + 1)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise FetchError(f"could not read {url}: {exc}") from None
    if len(data) > MAX_BYTES:
        data = data[:MAX_BYTES]
    if "pdf" in kind or data[:5] == b"%PDF-":
        return _pdf_text(data)
    charset = re.search(r"charset=([\w-]+)", kind)
    text = data.decode(charset.group(1) if charset else "utf-8", "replace")
    return html_to_text(text) if "html" in kind or "<html" in text[:2000].lower() else text


_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"',
                         "–": "-", "—": "-", " ": " "})


def normalize(text: str) -> str:
    """Lower case, letters and digits only, single spaces: for comparing a quote with a page."""
    text = text.translate(_QUOTES).lower()
    return " ".join(re.findall(r"[^\W_]+", text))


def quote_found(quote: str, texts: list[str], min_words: int = 6) -> bool:
    """The quote has at least `min_words` words and appears word for word in one of the texts."""
    wanted = normalize(quote)
    if len(wanted.split()) < min_words:
        return False
    return any(wanted in normalize(t) for t in texts if t)


def same_page(a: str, b: str) -> bool:
    def key(url: str) -> str:
        parts = urllib.parse.urlsplit(url.strip())
        host = (parts.hostname or "").lower().removeprefix("www.")
        return host + parts.path.rstrip("/") + ("?" + parts.query if parts.query else "")
    return key(a) == key(b)


# ------------------------------------------------------------------- search engines
# Used by the research loop that the program runs itself (any model with tool calling).
# Each engine returns [{"url", "title", "text"}], where "text" is the passage the engine
# returned for that page.

SEARCH_ENGINES = {"deepseek": "DEEPSEEK_API_KEY", "tavily": "TAVILY_API_KEY",
                  "brave": "BRAVE_API_KEY", "searxng": None}


def _json_request(url: str, payload: dict | None, headers: dict, timeout: float = 30) -> dict:
    import json
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"accept": "application/json", "user-agent": USER_AGENT, **headers}
    if data is not None:
        headers["content-type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers,
                                     method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:300]
        raise FetchError(f"search failed (HTTP {exc.code}): {body}") from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise FetchError(f"search failed: {exc}") from None


def make_search(engine: str, key: str | None = None, base_url: str | None = None):
    """A function query -> results for one engine. Raises FetchError if a key is missing."""
    import os
    env = SEARCH_ENGINES[engine]
    key = key or (os.environ.get(env) if env else None)
    if env and not key:
        raise FetchError(f"search engine '{engine}' needs {env}")

    def deepseek(query: str) -> list[dict]:
        out = _json_request("https://api.deepseek.com/v1/tools/search",
                            {"query": query[:128], "count": 10, "need_content": False},
                            {"authorization": f"Bearer {key}"})
        return [{"url": r.get("url", ""), "title": r.get("title", ""),
                 "text": r.get("snippet") or r.get("summary") or ""}
                for r in out.get("results") or [] if r.get("url")]

    def tavily(query: str) -> list[dict]:
        out = _json_request("https://api.tavily.com/search",
                            {"query": query, "max_results": 10, "search_depth": "advanced"},
                            {"authorization": f"Bearer {key}"})
        return [{"url": r.get("url", ""), "title": r.get("title", ""), "text": r.get("content", "")}
                for r in out.get("results") or [] if r.get("url")]

    def brave(query: str) -> list[dict]:
        url = ("https://api.search.brave.com/res/v1/web/search?"
               + urllib.parse.urlencode({"q": query, "count": 10, "extra_snippets": "true"}))
        out = _json_request(url, None, {"x-subscription-token": key})
        return [{"url": r.get("url", ""), "title": r.get("title", ""),
                 "text": " ".join([r.get("description", "")] + (r.get("extra_snippets") or []))}
                for r in (out.get("web") or {}).get("results") or [] if r.get("url")]

    def searxng(query: str) -> list[dict]:
        root = (base_url or os.environ.get("SEARXNG_URL") or "").rstrip("/")
        if not root:
            raise FetchError("search engine 'searxng' needs --searxng-url or SEARXNG_URL")
        out = _json_request(f"{root}/search?" + urllib.parse.urlencode(
            {"q": query, "format": "json"}), None, {})
        return [{"url": r.get("url", ""), "title": r.get("title", ""), "text": r.get("content", "")}
                for r in (out.get("results") or [])[:10] if r.get("url")]

    return {"deepseek": deepseek, "tavily": tavily, "brave": brave, "searxng": searxng}[engine]


def pick_engine(preferred: str, provider: str, searxng_url: str | None = None) -> str | None:
    """'auto': the provider's own engine if it has one, else the first engine with a key."""
    import os
    if preferred != "auto":
        return preferred
    if provider == "deepseek" and os.environ.get("DEEPSEEK_API_KEY"):
        return "deepseek"
    for engine in ("tavily", "brave", "deepseek"):
        if os.environ.get(SEARCH_ENGINES[engine]):
            return engine
    if searxng_url or os.environ.get("SEARXNG_URL"):
        return "searxng"
    return None
