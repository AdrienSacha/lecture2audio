# ADR-0007: A local reference library, searched before the web, with checked quotes

**Status:** Accepted
**Date:** 20 November 2026
**Deciders:** The project owner.

## Context

Gaps in a lecture (an untaught slide idea, an undefined term, an assumed fundamental, a skipped step) are filled only with facts whose quote code finds word for word in a page (`enrich.check_facts`, ADR-0002). Until now the only pages were web pages. The student usually owns better sources: the course textbook and reference books, which use the course's own terms and notation. NotebookLM-style tools let a user add such sources; the owner wants the same, without giving up the rule that every quote is checked by code.

## Decision

1. **`library.py` keeps the books in one SQLite database with an FTS5 index** (standard library `sqlite3`, BM25 ranking, `porter unicode61` tokenizer), at `L2A_LIBRARY` (default `~/.lecture2audio/library/library.db`; `off` disables it). One row per book (id, title, file, SHA-256 of the file, page count) and one indexed row per page. Pages are read with pypdf; a page under `ocr.MIN_CHARS` characters goes through the existing OCR path when it is installed. Adding the same file twice does nothing (the hash is unique).
2. **Command:** `lecture2audio library add <pdf...> [--title]`, `library list`, `library remove <id>`, `library search "<query>"`. Like `key`, it runs before the saved keys are loaded: it never needs a key.
3. **Research prefers the library.** The program-run research loop (`llm.research_loop`) offers `search_library` and `read_pages` before `web_search` and `fetch_page` when the library holds books, and runs alone on the library when no web search engine is set up. For every provider, including those that search on their own (Anthropic, OpenAI), the request carries the best pages for the gap's query and term under `LIBRARY`. The prompt (`prompts/research.md`) tells the model to look in the books first.
4. **Same check as the web.** A fact from a book names its source `library:<book>:p<page>` as its `url`; `check_facts` finds its quote word for word in that stored page (or in that page joined to the next one, for a quote across a page break) and rejects it otherwise. The answer's JSON and its gate do not change. Kept facts carry the title "Title, p. N", which `script.md`, `reference.md`, the vault's Sources note and the recitation bibliography cite instead of an address.
5. **Termbase.** For each required term, the best page that names it is passed to the termbase call as `REFERENCE`; the lecture's usage still wins.
6. **An empty, missing or disabled library changes nothing**: no section is added to any request and no tool is offered.

## Consequences

- Facts come from the student's own course material more often, with page citations a student can open.
- Privacy: the index stays on this machine. The passages a run uses (search hits, pages read, quotes) are sent to the model provider like the rest of the lecture.
- Research results are cached per gap (`research/gap_*.json`); a book added later is used only after `--redo research`.
- BM25 on words, not embeddings: a gap phrased with words the book does not use can miss the right page. The model can search again with other words; semantic search is left for later.
- Scanned books without OCR installed are stored with empty pages, which `library add` reports.
