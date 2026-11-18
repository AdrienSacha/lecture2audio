"""LaTeX for the documents the program writes (the recitation notes and the project).

The model never writes a document. It writes text fields (plain Markdown with math between
dollar signs), and this module turns them into LaTeX: every character outside math is escaped,
math and TikZ are refused when they hold a command that reads files, writes files or redefines
macros, and the PDF is built without shell escape. A lecture's slides can hold text meant for
the model, so nothing a model returns is trusted to be harmless TeX.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

ASSETS = Path(__file__).parent / "assets"

# Commands that read or write files, run code, or change what later text means. None is needed
# to write a formula or to draw a figure.
FORBIDDEN = re.compile(
    r"\\(?:input|include|includegraphics|includeonly|openin|openout|read|readline|write|"
    r"immediate|csname|catcode|directlua|latelua|luaexec|luadirect|usepackage|RequirePackage|"
    r"documentclass|def|edef|gdef|xdef|let|futurelet|newcommand|renewcommand|providecommand|"
    r"newenvironment|renewenvironment|DeclareMathOperator|loop|repeat|special|pdfliteral|"
    r"pdfobj|verbatiminput|lstinputlisting|usetikzlibrary|tikzexternalize|url|href|hyperlink|"
    r"hypertarget|label|ref|cite|bibitem|begin\s*\{\s*(?:document|filecontents|verbatim|"
    r"lstlisting|Verbatim)\s*\}|end\s*\{\s*document\s*\})(?![A-Za-z])")

_SPECIAL = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#",
            "_": r"\_", "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}",
            "^": r"\textasciicircum{}"}
_ESCAPE = re.compile("[" + re.escape("".join(_SPECIAL)) + "]")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\ue000-\uf8ff\ufffd]")  # TeX stops on these
# $$display$$, $inline$, `code`, **bold**, *italic*, in that order of precedence
_TOKEN = re.compile(r"(\$\$.+?\$\$|\$[^$\n]+\$|`[^`\n]+`|\*\*[^*\n]+\*\*|(?<![\w*])\*[^*\n]+\*(?![\w*]))",
                    re.S)
_ITEM = re.compile(r"^\s*(?:[-*•]|(\d{1,2})[.)])\s+(.*)$")


def escape(text: str) -> str:
    """Plain text as LaTeX text."""
    return _ESCAPE.sub(lambda m: _SPECIAL[m.group(0)], _CONTROL.sub(" ", str(text)))


def slug(text: str) -> str:
    """An anchor name: letters and digits only."""
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-") or "x"


def tex_error(source: str) -> str | None:
    """Why a piece of math or of TikZ may not go into the document, or None."""
    found = FORBIDDEN.search(source)
    if found:
        return f"the command {found.group(0).strip()} is not allowed"
    depth = 0
    for char in re.sub(r"\\[{}\\]", "", source):
        depth += (char == "{") - (char == "}")
        if depth < 0:
            break
    if depth != 0:
        return "the braces { } do not match"
    if re.search(r"(?<!\\)%", source):
        return "a % starts a comment in LaTeX: write \\%"
    return None


def math_errors(text: str) -> list[str]:
    """Errors for every $...$ and $$...$$ of a text field."""
    return [f"{m.group(0)[:60]}: {why}" for m in _TOKEN.finditer(text)
            if m.group(0).startswith("$") and (why := tex_error(m.group(0).strip("$")))]


class Linker:
    """Links the first use of each term in a section to its glossary entry."""

    def __init__(self, terms: list[str] = ()):
        from .enrich import term_pattern
        self.patterns = [(t, term_pattern(t)) for t in sorted(terms, key=len, reverse=True)]
        self.seen: set[str] = set()

    def new_section(self) -> None:
        self.seen.clear()

    def plain(self, text: str) -> str:
        found = []
        for term, pattern in self.patterns:
            if term in self.seen:
                continue
            match = pattern.search(text)
            if match and not any(a < match.end() and match.start() < b for a, b, _ in found):
                found.append((match.start(), match.end(), term))
                self.seen.add(term)
        out, at = [], 0
        for start, end, term in sorted(found):
            out.append(escape(text[at:start]))
            out.append(rf"\hyperlink{{term:{slug(term)}}}{{{escape(text[start:end])}}}")
            at = end
        return "".join(out) + escape(text[at:])


def inline(text: str, linker: Linker | None = None) -> str:
    """One line of plain Markdown as LaTeX: math kept, the rest escaped."""
    out = []
    for piece in _TOKEN.split(str(text)):
        if not piece:
            continue
        if piece.startswith("$"):
            body = piece.strip("$").strip()
            if not body or tex_error(body):
                out.append(r"\texttt{" + escape(body) + "}")
            elif piece.startswith("$$"):
                out.append(display(body))
            else:
                out.append(f"${body}$")
        elif piece.startswith("`") and len(piece) > 2:
            out.append(r"\texttt{" + escape(piece[1:-1]) + "}")
        elif piece.startswith("**") and len(piece) > 4:
            out.append(r"\textbf{" + inline(piece[2:-2], linker) + "}")
        elif piece.startswith("*") and piece.endswith("*") and len(piece) > 2:
            out.append(r"\emph{" + inline(piece[1:-1], linker) + "}")
        else:
            out.append(linker.plain(piece) if linker else escape(piece))
    return "".join(out)


def display(formula: str) -> str:
    """A formula on its own line; several lines or alignment marks become an aligned block."""
    body = formula.strip()
    if tex_error(body):
        return "\n" + r"\begin{center}\texttt{" + escape(body) + r"}\end{center}" + "\n"
    if (r"\\" in body or "&" in body) and r"\begin{" not in body:
        body = r"\begin{aligned}" + body + r"\end{aligned}"
    return "\n\\[\n" + body + "\n\\]\n"


def block(text: str, linker: Linker | None = None) -> str:
    """A text field as paragraphs; lines that start with "- " or "1. " become lists."""
    out = []
    for paragraph in re.split(r"\n\s*\n", str(text).strip()):
        lines = [l for l in paragraph.splitlines() if l.strip()]
        items = [_ITEM.match(l) for l in lines]
        if lines and all(items):
            kind = "enumerate" if items[0].group(1) else "itemize"
            out.append(steps([m.group(2) for m in items], linker, kind))
        else:
            out.append(inline(" ".join(l.strip() for l in lines), linker))
    return "\n\n".join(o for o in out if o)


def steps(items: list[str], linker: Linker | None = None, kind: str = "enumerate") -> str:
    body = "\n".join(r"\item " + inline(i, linker) for i in items)
    return f"\\begin{{{kind}}}\n{body}\n\\end{{{kind}}}"


def verbatim(text: str, environment: str = "Verbatim") -> str:
    """Text set exactly as written (code, ASCII drawings)."""
    text = _CONTROL.sub(" ", str(text)).replace(f"\\end{{{environment}}}", f"\\end {{{environment}}}")
    return f"\\begin{{{environment}}}\n{text}\n\\end{{{environment}}}"


def url(address: str) -> str:
    """A clickable address. Characters TeX would read as commands are percent-encoded."""
    safe = "".join(c if c not in '\\{}^ "<>`|' else f"%{ord(c):02X}" for c in str(address).strip())
    return r"\url{" + safe.replace("%", r"\%").replace("#", r"\#") + "}"


def table(columns: list[str], rows: list[list[str]], linker: Linker | None = None) -> str:
    spec = "".join([r">{\raggedright\arraybackslash}X"] * len(columns))
    head = " & ".join(r"\textbf{" + inline(c, linker) + "}" for c in columns)
    body = "\n".join(" & ".join(inline(c, linker) for c in row) + r" \\" for row in rows)
    return (f"\\begin{{tabularx}}{{\\linewidth}}{{{spec}}}\n\\toprule\n{head} \\\\\n\\midrule\n"
            f"{body}\n\\bottomrule\n\\end{{tabularx}}")


def preamble(language: str, title: str, kind: str) -> str:
    """The shared preamble (assets/preamble.tex) for one document."""
    text = (ASSETS / "preamble.tex").read_text(encoding="utf-8")
    return (text.replace("%%BABEL%%", "french" if language == "fr" else "english")
            .replace("%%TITLE%%", escape(title)).replace("%%KIND%%", escape(kind)))


# ------------------------------------------------------------------ building

TIKZ_LIBRARIES = ("arrows.meta,positioning,shapes.geometric,shapes.multipart,calc,fit,"
                  "backgrounds,matrix,chains,decorations.pathreplacing,automata,trees")


def engine() -> list[str] | None:
    """The command that builds a PDF, or None when no LaTeX is installed. Shell escape stays
    off (the default), so a document cannot run a program."""
    if shutil.which("latexmk") and shutil.which("lualatex"):
        return ["latexmk", "-lualatex", "-interaction=nonstopmode", "-f", "-quiet",
                "-no-shell-escape"]
    if shutil.which("tectonic"):
        return ["tectonic", "--keep-logs", "--untrusted"]
    return None


def figure_error(tikz: str, timeout: int = 60) -> str | None:
    """Why a TikZ figure may not go into the document: a forbidden command, or it does not
    compile. Without pdflatex the figure is only screened, not compiled."""
    if "\\begin{tikzpicture}" not in tikz or "\\end{tikzpicture}" not in tikz:
        return r"a figure is one \begin{tikzpicture} ... \end{tikzpicture}"
    if why := tex_error(tikz):
        return why
    if not shutil.which("pdflatex"):
        return None
    source = ("\\documentclass[tikz,border=2pt]{standalone}\n\\usepackage{amsmath,amssymb}\n"
              f"\\usetikzlibrary{{{TIKZ_LIBRARIES}}}\n\\begin{{document}}\n{tikz}\n"
              "\\end{document}\n")
    with tempfile.TemporaryDirectory() as folder:
        (Path(folder) / "figure.tex").write_text(source, encoding="utf-8")
        try:
            run = subprocess.run(["pdflatex", "-interaction=nonstopmode", "-halt-on-error",
                                  "-no-shell-escape", "figure.tex"], cwd=folder,
                                 capture_output=True, text=True, errors="replace",
                                 timeout=timeout)
        except subprocess.TimeoutExpired:
            return "the figure takes too long to compile"
        if run.returncode == 0 and (Path(folder) / "figure.pdf").exists():
            return None
        lines = run.stdout.splitlines()
        at = next((k for k, l in enumerate(lines) if l.startswith("!")), None)
        return "it does not compile: " + (" ".join(lines[at:at + 3]) if at is not None
                                          else "unknown error")[:300]


def build_pdf(tex: Path, log=print, timeout: int = 600) -> dict:
    """Build `tex` into a PDF beside it. Returns {"pdf": path or None, "errors": [...]}:
    the build goes on past errors (a wrong formula must not cost the whole document), and the
    errors of the log are returned so that the report can list them."""
    tex = Path(tex)
    command = engine()
    if command is None:
        log(f"[pdf] no LaTeX found (MacTeX, TeX Live or tectonic): {tex.name} is written, "
            "the PDF is not built")
        return {"pdf": None, "errors": [], "engine": None}
    build = tex.parent / ".build" / tex.stem
    build.mkdir(parents=True, exist_ok=True)
    shutil.copy(tex, build / tex.name)
    try:
        subprocess.run([*command, tex.name], cwd=build, capture_output=True, text=True,
                       errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        log(f"[pdf] {tex.name}: the build was stopped after {timeout} seconds")
    made = build / (tex.stem + ".pdf")
    record = build / (tex.stem + ".log")
    errors = []
    if record.exists():
        lines = record.read_text(encoding="utf-8", errors="replace").splitlines()
        errors = [" ".join(lines[k:k + 2])[:200] for k, l in enumerate(lines) if l.startswith("!")]
    if not made.exists():
        log(f"[pdf] {tex.name} did not build ({errors[0] if errors else 'no PDF written'})")
        return {"pdf": None, "errors": errors, "engine": command[0]}
    target = tex.with_suffix(".pdf")
    shutil.copy(made, target)
    return {"pdf": target, "errors": errors, "engine": command[0]}
