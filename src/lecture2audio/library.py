"""The reference library: the user's own textbooks and reference PDFs, searched before the web.

`lecture2audio library add book.pdf` reads each page (pypdf's text layer; a page with fewer than
`ocr.MIN_CHARS` characters goes through OCR when it is installed) and stores it in one SQLite
database with an FTS5 index, ranked by BM25 (standard library only). The research stage then
offers the library to the model (the `search_library` and `read_pages` tools of the research
loop, or the best passages in the request for providers that search on their own), and a fact
taken from a book is kept only if its quote is found word for word in the stored page, as for
a web page (`enrich.check_facts`). Its source is `library:<book>:p<page>`, cited as
"Title, p. N".

The database is `L2A_LIBRARY` (default `~/.lecture2audio/library/library.db`; `off` turns the
library off). It stays on this machine; the passages a run uses are sent to the model provider.
A book is identified by the hash of its file, so adding the same file twice does nothing. A
missing or empty library changes nothing in a run.
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from . import ocr

DEFAULT_PATH = "~/.lecture2audio/library/library.db"
PREFIX = "library:"
SNIPPET_TOKENS = 48  # FTS5 allows at most 64
MAX_QUERY_WORDS = 24

_SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, file TEXT NOT NULL, hash TEXT NOT NULL UNIQUE,
    page_count INTEGER NOT NULL, ocr_pages INTEGER NOT NULL DEFAULT 0,
    empty_pages INTEGER NOT NULL DEFAULT 0, added TEXT NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS pages USING fts5(
    book UNINDEXED, page UNINDEXED, text, tokenize = 'porter unicode61 remove_diacritics 2');
"""


class LibraryError(RuntimeError):
    pass


def path() -> Path | None:
    """The database file, or None when the library is turned off (`L2A_LIBRARY=off`)."""
    value = os.environ.get("L2A_LIBRARY", DEFAULT_PATH).strip()
    if value.lower() in ("off", "0", "none", ""):
        return None
    return Path(value).expanduser()


def source_id(book: str, page: int) -> str:
    return f"{PREFIX}{book}:p{int(page)}"


def is_source(url: str) -> bool:
    return str(url).strip().startswith(PREFIX)


def parse_source(url: str) -> tuple[str, int] | None:
    """(book id, page) of `library:<book>:p<page>`, or None."""
    match = re.fullmatch(r"library:([\w.-]+):p(\d+)", str(url).strip())
    return (match.group(1), int(match.group(2))) if match else None


def citation(title: str, page: int) -> str:
    return f"{title}, p. {int(page)}"


def _slug(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40].strip("-") or "book"


def _fts_query(query: str) -> str:
    """Every word of the query as a quoted FTS5 term, joined by OR: BM25 ranks the pages that
    hold the most and the rarest of them first. Quoting keeps AND, NOT, * and quotes literal."""
    words = re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", str(query)))
    unique = list(dict.fromkeys(w.lower() for w in words))[:MAX_QUERY_WORDS]
    return " OR ".join(f'"{w}"' for w in unique)


def _read_pdf(file: Path, log=None) -> tuple[list[str], int]:
    """The text of each page, and how many pages were read by OCR."""
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise LibraryError("Reading a PDF needs pypdf (uv pip install pypdf).") from exc
    from .sources import _LIGATURES
    try:
        reader = PdfReader(str(file))
        texts = [(page.extract_text() or "").translate(_LIGATURES).strip()
                 for page in reader.pages]
    except Exception as exc:  # pypdf raises many kinds of errors on damaged files
        raise LibraryError(f"{file.name}: not a readable PDF ({exc})") from None
    read = 0
    short = [i for i, t in enumerate(texts) if len(t) < ocr.MIN_CHARS]
    if short and ocr.available():
        if log:
            log(f"  {len(short)} page(s) without a text layer: reading them by OCR")
        for index in short:
            try:
                found = ocr.read_image(ocr.render_page(file, index)).strip()
            except Exception as exc:  # one unreadable picture never stops the import
                if log:
                    log(f"  page {index + 1}: OCR failed ({exc})")
                continue
            if len(found) > len(texts[index]):
                texts[index], read = found, read + 1
    return texts, read


class Library:
    """One library database. Each method opens its own connection, so the research threads
    can search at the same time; reading never creates the file."""

    def __init__(self, file: str | Path):
        self.file = Path(file).expanduser()

    @classmethod
    def default(cls) -> "Library | None":
        file = path()
        return cls(file) if file else None

    @classmethod
    def existing(cls) -> "Library | None":
        """The library of `L2A_LIBRARY` when it holds at least one book, else None."""
        library = cls.default()
        return library if library is not None and library.size() else None

    # -- connections
    def _write(self) -> sqlite3.Connection:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.file)
        db.executescript(_SCHEMA)
        return db

    def _read(self) -> sqlite3.Connection | None:
        if not self.file.is_file():
            return None
        try:
            db = sqlite3.connect(self.file.resolve().as_uri() + "?mode=ro", uri=True)
            db.execute("SELECT 1 FROM books LIMIT 1")
            return db
        except sqlite3.Error:
            return None

    # -- writing
    def add(self, paths, title: str | None = None, log=print) -> list[dict]:
        """Adds each PDF; a file already in the library (same hash) is skipped. Returns one
        {"book", "title", "pages", "status"} per file, status "added" or "already there"."""
        paths, results = [paths] if isinstance(paths, (str, Path)) else [*paths], []
        for item in paths:
            file = Path(item).expanduser()
            if not file.is_file():
                raise LibraryError(f"{file}: no such file")
            if file.suffix.lower() != ".pdf":
                raise LibraryError(f"{file.name}: only PDF files can be added")
            digest = hashlib.sha256(file.read_bytes()).hexdigest()
            db = self._write()
            try:
                known = db.execute("SELECT id, title, page_count FROM books WHERE hash = ?",
                                   (digest,)).fetchone()
                if known:
                    results.append({"book": known[0], "title": known[1], "pages": known[2],
                                    "status": "already there"})
                    continue
                name = title if title and len(paths) == 1 else \
                    re.sub(r"[_\s]+", " ", file.stem).strip() or file.stem
                if log:
                    log(f"reading {file.name}")
                texts, read = _read_pdf(file, log)
                base = book = _slug(name)
                taken = {row[0] for row in db.execute("SELECT id FROM books")}
                n = 2
                while book in taken:
                    book, n = f"{base}-{n}", n + 1
                empty = sum(1 for t in texts if len(t) < ocr.MIN_CHARS)
                with db:
                    db.execute("INSERT INTO books VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                               (book, name, str(file.resolve()), digest, len(texts), read, empty,
                                datetime.now(timezone.utc).isoformat(timespec="seconds")))
                    db.executemany("INSERT INTO pages (book, page, text) VALUES (?, ?, ?)",
                                   [(book, k, t) for k, t in enumerate(texts, 1) if t])
                results.append({"book": book, "title": name, "pages": len(texts),
                                "ocr_pages": read, "empty_pages": empty, "status": "added"})
            finally:
                db.close()
        return results

    def remove(self, book: str) -> bool:
        if not self.file.is_file():
            return False
        db = self._write()
        try:
            with db:
                gone = db.execute("DELETE FROM books WHERE id = ?", (book,)).rowcount
                db.execute("DELETE FROM pages WHERE book = ?", (book,))
            return bool(gone)
        finally:
            db.close()

    # -- reading
    def list(self) -> list[dict]:
        db = self._read()
        if db is None:
            return []
        try:
            rows = db.execute("SELECT id, title, page_count, ocr_pages, empty_pages, file, added "
                              "FROM books ORDER BY title COLLATE NOCASE").fetchall()
        finally:
            db.close()
        keys = ("book", "title", "pages", "ocr_pages", "empty_pages", "file", "added")
        return [dict(zip(keys, row)) for row in rows]

    def size(self) -> int:
        db = self._read()
        if db is None:
            return 0
        try:
            return db.execute("SELECT count(*) FROM books").fetchone()[0]
        finally:
            db.close()

    def search(self, query: str, k: int = 5) -> list[dict]:
        """The best pages for a query: [{"book", "title", "page", "source", "text"}], where
        "text" is a snippet of the page around the words found."""
        match = _fts_query(query)
        db = self._read() if match else None
        if db is None:
            return []
        try:
            rows = db.execute(
                "SELECT pages.book, books.title, pages.page, "
                f"snippet(pages, 2, '', '', ' ... ', {SNIPPET_TOKENS}) "
                "FROM pages JOIN books ON books.id = pages.book "
                "WHERE pages MATCH ? ORDER BY bm25(pages) LIMIT ?", (match, int(k))).fetchall()
        except sqlite3.Error:
            return []
        finally:
            db.close()
        return [{"book": b, "title": t, "page": int(p), "source": source_id(b, p),
                 "text": " ".join(s.split())} for b, t, p, s in rows]

    def title(self, book: str) -> str | None:
        db = self._read()
        if db is None:
            return None
        try:
            row = db.execute("SELECT title FROM books WHERE id = ?", (book,)).fetchone()
        finally:
            db.close()
        return row[0] if row else None

    def page_texts(self, book: str, pages) -> dict[int, str]:
        wanted = sorted({int(p) for p in pages})
        db = self._read()
        if db is None or not wanted:
            return {}
        try:
            marks = ",".join("?" * len(wanted))
            rows = db.execute(f"SELECT page, text FROM pages WHERE book = ? AND page IN ({marks})",
                              (book, *wanted)).fetchall()
        finally:
            db.close()
        return {int(p): t for p, t in rows}

    def read_pages(self, book: str, pages) -> str:
        """The text of some pages of a book, each under a line "[library:<book>:p<N>]"."""
        found = self.page_texts(book, pages)
        return "\n\n".join(f"[{source_id(book, p)}]\n{t}" for p, t in sorted(found.items()))


# -- the functions of the default library (L2A_LIBRARY)

def _default() -> Library:
    library = Library.default()
    if library is None:
        raise LibraryError("the library is turned off (L2A_LIBRARY=off)")
    return library


def add(paths, title: str | None = None, log=print) -> list[dict]:
    return _default().add(list(paths), title, log)


def remove(book: str) -> bool:
    return _default().remove(book)


def list_books() -> list[dict]:
    library = Library.default()
    return library.list() if library else []


def search(query: str, k: int = 5) -> list[dict]:
    library = Library.default()
    return library.search(query, k) if library else []


def read_pages(book: str, pages) -> str:
    library = Library.default()
    return library.read_pages(book, pages) if library else ""


# -- what research uses

def passages(library: Library | None, queries: list[str], k: int = 3,
             max_chars: int = 4000) -> list[dict]:
    """The best pages for some queries, whole (up to `max_chars`), without repeats: what the
    request of a provider that searches on its own carries, so that it can quote a book."""
    if library is None:
        return []
    seen, out = set(), []
    for query in queries:
        for hit in library.search(query, k):
            if hit["source"] in seen:
                continue
            seen.add(hit["source"])
            text = library.page_texts(hit["book"], [hit["page"]]).get(hit["page"], "")
            out.append(dict(hit, text=text[:max_chars]))
            if len(out) >= k:
                return out
    return out


def render_passages(hits: list[dict]) -> str:
    return "\n\n".join(f"[{h['source']}] {citation(h['title'], h['page'])}\n{h['text']}"
                       for h in hits)


def quote_pages(library: Library | None, url: str) -> tuple[list[str], str] | None:
    """For a fact whose source is `library:<book>:p<page>`: the texts its quote may be found
    in (the page, and the page joined to the next one, for a quote across a page break) and
    its citation "Title, p. N". None when the book or the page is not in the library."""
    parsed = parse_source(url)
    if library is None or parsed is None:
        return None
    book, page = parsed
    title = library.title(book)
    texts = library.page_texts(book, [page, page + 1])
    if title is None or page not in texts:
        return None
    joined = texts[page] + (" " + texts[page + 1] if page + 1 in texts else "")
    return [texts[page], joined], citation(title, page)
