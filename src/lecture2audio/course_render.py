"""Course material as a Markdown vault, ready for Obsidian (and readable anywhere).

One note per part, one note per term, and an index, a glossary, a notation table, the sources and
a review note. Terms are linked to their notes on first use in every card, and every term note
lists what it requires, what requires it, what it must not be confused with and where it is
used, so the notes form a graph. Math is LaTeX between dollar signs; figures are numbered still
frames, each a Mermaid diagram or an ASCII drawing in a callout; answers to the check questions
and prerequisite reminders are collapsed callouts. When the audio was made, each part note and
the index embed their recording (copied into audio/), and a "Full Lecture" note transcludes the
parts in order, so the lecture reads as one chapter.
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from . import enrich
from .library import is_source
from .course import _texts
from .outline import Outline

ASSETS = Path(__file__).parent / "assets"

LABELS = {
    "en": {"needs": "Prerequisites", "external": "From outside the lecture",
           "read_all": "Read the whole lecture as one chapter", "book": "Full Lecture",
           "plain": "In short", "proof": "Proof", "exercises": "Exercises",
           "exercise": "Exercise", "solution": "Solution", "answer": "Answer",
           "external_text": "The lecture does not state this. It is added from the sources:",
           "definition": "Definition", "concept": "Idea", "procedure": "Procedure",
           "remark": "Remark", "instance": "Instance", "example": "Worked example",
           "step": "Figure, step", "symbol": "Symbol", "meaning": "Meaning",
           "illustration": "Illustration added by the course material: its numbers are not in "
                           "the lecture.",
           "review": "Review of earlier parts", "previous": "Previous", "next": "Next",
           "parts": "Parts", "reference": "Reference", "defined_in": "Defined in",
           "requires": "Requires", "required_by": "Needed by",
           "confusable": "Not to be confused with", "used_in": "Used in", "none": "None.",
           "cards": "cards", "terms": "terms", "diagrams": "figures", "checks": "check questions",
           "lectures": "lectures", "lectures_title": "Lectures", "minutes": "min read", "glossary": "Glossary", "notation": "Notation",
           "sources": "Sources", "final": "Review", "card": "card"},
    "fr": {"needs": "Prérequis", "external": "Hors du cours",
           "read_all": "Lire tout le cours comme un seul chapitre", "book": "Cours complet",
           "plain": "En bref", "proof": "Démonstration", "exercises": "Exercices",
           "exercise": "Exercice", "solution": "Solution", "answer": "Réponse",
           "external_text": "Le cours ne le dit pas. C'est ajouté depuis les sources :",
           "definition": "Définition", "concept": "Idée", "procedure": "Procédure",
           "remark": "Remarque", "instance": "Cas concret", "example": "Exemple détaillé",
           "step": "Figure, étape", "symbol": "Symbole", "meaning": "Signification",
           "illustration": "Illustration ajoutée par le support : ses nombres ne sont pas dans "
                           "le cours.",
           "review": "Révision des parties précédentes", "previous": "Précédent",
           "next": "Suivant", "parts": "Parties", "reference": "Référence",
           "defined_in": "Défini dans", "requires": "Nécessite", "required_by": "Nécessaire à",
           "confusable": "À ne pas confondre avec", "used_in": "Utilisé dans", "none": "Aucun.",
           "cards": "fiches", "terms": "termes", "diagrams": "figures",
           "checks": "questions de contrôle", "lectures": "cours", "lectures_title": "Cours", "minutes": "min de lecture",
           "glossary": "Glossaire", "notation": "Notation", "sources": "Sources",
           "final": "Révision", "card": "fiche"},
}

_ILLEGAL = re.compile(r'[\\/:*?"<>|#^\[\]]+')
_VERBATIM = re.compile(r"(\$\$.+?\$\$|\$[^$\n]+\$|`[^`\n]*`)", re.S)


def clean(text: str) -> str:
    """A title that is safe in a file name and in a [[link]]."""
    return " ".join(_ILLEGAL.sub(" ", str(text)).split())


def quote(text: str) -> str:
    """The text as the body of a callout."""
    return "\n".join("> " + line if line else ">" for line in str(text).splitlines())


def front(value) -> str:
    """A YAML value (JSON strings are valid YAML)."""
    return json.dumps(value, ensure_ascii=False)


def link_terms(text: str, terms: list[dict], seen: set[int], skip: frozenset = frozenset()) -> str:
    """The text with each known term turned into a [[link]] once per card. Math and code are
    left as they are."""
    out = []
    for piece in _VERBATIM.split(text):
        if _VERBATIM.fullmatch(piece):
            out.append(piece)
            continue
        hits: list[tuple[int, int, int]] = []
        for index in sorted(range(len(terms)), key=lambda i: -len(terms[i]["term"])):
            if index in seen or index in skip:
                continue
            match = enrich.term_pattern(terms[index]["term"]).search(piece)
            if match and not any(a < match.end() and match.start() < b for a, b, _ in hits):
                hits.append((match.start(), match.end(), index))
        last = 0
        for start, end, index in sorted(hits):
            out.append(piece[last:start])
            name, shown = clean(terms[index]["term"]), piece[start:end]
            out.append(f"[[{name}]]" if shown == name else f"[[{name}|{shown}]]")
            seen.add(index)
            last = end
        out.append(piece[last:])
    return "".join(out)


def bold_definition(card: dict, terms: list[dict], seen: set[int], skip: frozenset) -> str:
    """The definition, with the defined term in bold, once, and the other terms linked."""
    text = card["definition"]
    for term in card["terms"]:
        match = enrich.term_pattern(term).search(text)
        if match:
            return (link_terms(text[:match.start()], terms, seen, skip)
                    + f"**{match.group(0)}**" + link_terms(text[match.end():], terms, seen, skip))
    return link_terms(text, terms, seen, skip)


def render_card(card: dict, terms: list[dict], index: dict[str, int], source_links: list[str]):
    seen: set[int] = set()
    own = frozenset(index[enrich._key(t)] for t in card["terms"] if enrich._key(t) in index)
    view = dict(card)
    view["definition"] = bold_definition(card, terms, seen, own) if card["definition"] else ""
    view["instance"] = link_terms(card["instance"], terms, seen, own)
    view["plain"] = card.get("plain", "")
    view["proof"] = card.get("proof", [])
    view["explanation"] = [link_terms(p, terms, seen, own) for p in card["explanation"]]
    if card["example"]:
        view["example"] = dict(card["example"], steps=[link_terms(s, terms, seen, own)
                                                        for s in card["example"]["steps"]])
    view["external"] = card["origin"] == "external"
    view["sources"] = source_links
    return view


def place_audio(source: Path, folder: Path, name: str) -> str:
    """Copy a recording into the lecture's audio/ folder of the vault, so that Obsidian can play
    it in a note; return its file name. An unchanged copy is not copied again."""
    target = folder / "audio" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    stat = Path(source).stat()
    if not (target.exists() and target.stat().st_size == stat.st_size
            and target.stat().st_mtime >= stat.st_mtime):
        shutil.copy2(source, target)
    return name


def merge_terms(lectures: list[dict]) -> list[dict]:
    """The terms of the whole course, one entry each: the entry of the lecture that defines the
    term (a later lecture only borrows it), in the order of the lectures."""
    merged: dict[str, dict] = {}
    for number, lecture in enumerate(lectures):
        for term in lecture["terms"]:
            key = enrich._key(term["term"])
            if key not in merged or (merged[key].get("from_series") and not term.get("from_series")):
                merged[key] = dict(term, lecture=number)
    return list(merged.values())


def write_vault(root: Path, lectures: list[dict], language: str, title: str | None = None) -> dict:
    """Write the notes of one or several lectures into `root`; return counts for the report.

    Each lecture is {"name", "outline", "course", "records", "terms"}, and optionally "audio":
    {"full": path, "parts": {part id: path}}, the recordings to embed (copied into audio/ next
    to the notes). Notes are named with the lecture's name so that several lectures, or a vault
    that holds other notes, do not clash."""
    labels = LABELS.get(language, LABELS["en"])
    root = Path(root)
    (root / "Terms").mkdir(parents=True, exist_ok=True)
    terms = merge_terms(lectures)
    index = {enrich._key(t["term"]): i for i, t in enumerate(terms)}
    title = clean(title or lectures[0]["outline"].title)
    env = Environment(loader=FileSystemLoader(ASSETS), autoescape=False, trim_blocks=True,
                      lstrip_blocks=True, keep_trailing_newline=True)
    env.filters["quote"] = quote
    written = 0

    def write(path: Path, template: str, **context) -> None:
        nonlocal written
        path.write_text(env.get_template(template).render(
            labels=labels, fm=front, **context).rstrip() + "\n", encoding="utf-8")
        written += 1

    # names of the notes of every lecture
    for number, lecture in enumerate(lectures):
        outline, prefix = lecture["outline"], clean(lecture["name"])
        lecture["prefix"] = prefix
        lecture["folder"] = root if len(lectures) == 1 else root / prefix
        lecture["folder"].mkdir(parents=True, exist_ok=True)
        lecture["notes"] = {p["part"]: f"{prefix} – {int(p['part']):02d} "
                                       f"{clean(outline.node(p['part']).title)}"
                            for p in lecture["course"]["parts"]}
        lecture["card_node"] = {c["id"]: c["node"] for p in lecture["course"]["parts"]
                                for c in p["cards"]}

    def heading(lecture: dict, node_id: str) -> str:
        return f"{node_id} {clean(lecture['outline'].node(node_id).title)}"

    def node_link(lecture: dict, node_id: str, text: str | None = None) -> str:
        part = node_id.split(".")[0]
        note = lecture["notes"][part]
        target = note if lecture["outline"].node(node_id).level == 1 \
            else f"{note}#{heading(lecture, node_id)}"
        return f"[[{target}|{text or heading(lecture, node_id)}]]"

    def review_items(lecture: dict, items):
        return [dict(i, link=node_link(lecture, lecture["card_node"][i["card"]],
                                       f'{labels["card"]} {i["card"]}'))
                for i in items if i["card"] in lecture["card_node"]]

    # where each term is defined and used, over all lectures
    defined_at: dict[int, tuple[int, str]] = {}
    used_in: dict[int, list[tuple[int, str]]] = {i: [] for i in range(len(terms))}
    part_terms: dict[tuple[int, str], list[int]] = {}
    for number, lecture in enumerate(lectures):
        for part in lecture["course"]["parts"]:
            for card in part["cards"]:
                blob = "\n".join(_texts(card))
                own = {enrich._key(t) for t in card["terms"]}
                for i, term in enumerate(terms):
                    if enrich._key(term["term"]) in own:
                        defined_at.setdefault(i, (number, card["node"]))
                    elif enrich.term_pattern(term["term"]).search(blob):
                        if (number, card["node"]) not in used_in[i]:
                            used_in[i].append((number, card["node"]))
                        key = (number, part["part"])
                        part_terms.setdefault(key, [])
                        if i not in part_terms[key]:
                            part_terms[key].append(i)

    diagrams = cards_total = words = checks = 0
    for number, lecture in enumerate(lectures):
        outline, course, notes = lecture["outline"], lecture["course"], lecture["notes"]
        by_record = {r["part"]: r for r in lecture["records"]}
        parts = course["parts"]
        audio = lecture.get("audio") or {}
        part_audio = {pid: place_audio(path, lecture["folder"],
                                       f"{lecture['prefix']} – {int(pid):02d}{Path(path).suffix}")
                      for pid, path in (audio.get("parts") or {}).items() if Path(path).exists()}
        full_audio = (place_audio(audio["full"], lecture["folder"],
                                  f"{lecture['prefix']}{Path(audio['full']).suffix}")
                      if audio.get("full") and Path(audio["full"]).exists() else "")
        book = f"{lecture['prefix']} – {labels['book']}"
        for p, part_data in enumerate(parts):
            part = outline.node(part_data["part"])
            groups, order = [], []
            for card in part_data["cards"]:
                if card["node"] not in order:
                    order.append(card["node"])
            for node_id in order:
                node = outline.node(node_id)
                cards = []
                for card in (c for c in part_data["cards"] if c["node"] == node_id):
                    diagrams += bool(card["diagram"])
                    cards_total += 1
                    checks += len(card["checks"])
                    words += sum(len(t.split()) for t in
                                 [card["definition"], card["instance"], *card["explanation"]] if t)
                    links = [f"[[{lecture['prefix']} – Sources#{x}|{x}]]" for x in card["sources"]]
                    cards.append(render_card(card, terms, index, links))
                groups.append({"heading": heading(lecture, node_id) if node.level > 1 else "",
                               "depth": node.level, "cards": cards})
            earlier = [i for i in part_terms.get((number, part.id), [])
                       if i in defined_at and (defined_at[i][0], int(defined_at[i][1].split(".")[0]))
                       < (number, int(part.id))]
            write(lecture["folder"] / f"{notes[part.id]}.md", "part.md.j2", part=part,
                  title=clean(part.title),
                  intro=(by_record.get(part.id, {}).get("preview") or {}).get("intro", ""),
                  prerequisites=[f"[[{clean(terms[i]['term'])}]]" for i in earlier],
                  groups=groups, prev=notes[parts[p - 1]["part"]] if p else "",
                  next=notes[parts[p + 1]["part"]] if p + 1 < len(parts) else "",
                  review=review_items(lecture, course["review"].get(part.id, [])),
                  part_audio_file=part_audio.get(part.id, ""),
                  exercises=part_data.get("exercises", []))
        final = review_items(lecture, course["review"].get("final", []))
        lecture["final"] = final
        write(lecture["folder"] / f"{lecture['prefix']} – Index.md", "index.md.j2",
              title=clean(outline.title), stats=[], final_review=final,
              full_audio_file=full_audio, book_note=book,
              review_note=f"{lecture['prefix']} – Review",
              sources_note=f"{lecture['prefix']} – Sources", parts=[
                  {"note": notes[p["part"]], "intro": (by_record.get(p["part"], {}).get("preview")
                                                       or {}).get("intro", "")} for p in parts])
        # the continuous reading view: every part transcluded, in order
        write(lecture["folder"] / f"{book}.md", "book.md.j2", title=clean(outline.title),
              full_audio_file=full_audio, parts=[notes[p["part"]] for p in parts])
        sources = {}
        for record in lecture["records"]:
            for x in record.get("supplements", []):
                sources[x["id"]] = {"id": x["id"], "text": x.get("text", ""),
                                    "quote": x.get("quote", ""),
                                    "url": x["url"] if str(x.get("url", "")).startswith(
                                        ("http://", "https://")) else
                                    x.get("title", "") if is_source(x.get("url", "")) else ""}
        write(lecture["folder"] / f"{lecture['prefix']} – Sources.md", "back.md.j2",
              kind="sources", heading=f"{lecture['prefix']} – {labels['sources']}",
              sources=list(sources.values()))
        if final:
            write(lecture["folder"] / f"{lecture['prefix']} – Review.md", "back.md.j2",
                  kind="review", heading=f"{lecture['prefix']} – {labels['final']}", review=final)

    required_by: dict[str, list[str]] = {}
    for term in terms:
        for dep in term["depends_on"]:
            required_by.setdefault(enrich._key(dep), []).append(term["term"])

    def term_link(name: str) -> str:
        return f"[[{clean(name)}]]" if enrich._key(name) in index else name

    for i, term in enumerate(terms):
        where = "–"
        if i in defined_at:
            where = node_link(lectures[defined_at[i][0]], defined_at[i][1])
        write(root / "Terms" / f"{clean(term['term'])}.md", "term.md.j2", term=term,
              defined_in=where, requires=[term_link(d) for d in term["depends_on"]],
              required_by=[term_link(t) for t in required_by.get(enrich._key(term["term"]), [])],
              confusable=[term_link(c) for c in term["confusable"]],
              used_in=[node_link(lectures[n], node) for n, node in used_in[i]], aliases=None)

    prefix = title
    owners = {}
    notation = []
    for lecture in lectures:
        for p in lecture["course"]["parts"]:
            for c in p["cards"]:
                for n in c["notation"]:
                    if n["symbol"] not in owners:
                        owners[n["symbol"]] = node_link(lecture, c["node"])
                        notation.append(dict(n, link=owners[n["symbol"]]))
    write(root / f"{prefix} – Glossary.md", "back.md.j2", kind="glossary",
          heading=f"{prefix} – {labels['glossary']}",
          terms=[{"link": f"[[{clean(t['term'])}]]", "definition": t["definition"]}
                 for t in terms])
    write(root / f"{prefix} – Notation.md", "back.md.j2", kind="notation",
          heading=f"{prefix} – {labels['notation']}", notation=notation)
    stats = [f'{len(lectures)} {labels["lectures"]}'] if len(lectures) > 1 else []
    stats += [f'{cards_total} {labels["cards"]}', f'{len(terms)} {labels["terms"]}',
              f'{diagrams} {labels["diagrams"]}', f'{checks} {labels["checks"]}',
              f'{max(1, round(words / 180))} {labels["minutes"]}']
    write(root / "00 Course.md", "course.md.j2", title=title, stats=stats,
          lectures=[{"index": f"{l['prefix']} – Index", "title": clean(l["outline"].title)}
                    for l in lectures],
          reference=[f"{prefix} – Glossary", f"{prefix} – Notation"])
    entry = (lectures[0]["folder"] / f"{lectures[0]['prefix']} – Index.md" if len(lectures) == 1
             else root / "00 Course.md")
    return {"cards": cards_total, "diagrams": diagrams, "notes": written, "words": words,
            "terms": len(terms), "entry": str(entry)}
