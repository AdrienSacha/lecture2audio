"""The outline: a tree of parts, points and sub-points laid over the transcript.

The transcript is cut into numbered units (about one sentence each). A model groups the units
into a tree of at most three levels and marks the units that are noise. Everything here is plain
code: it checks the model's tree (every unit used exactly once, parts in the lecture's order,
no part too long) and derives what the later stages need from it.
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field

MAX_DEPTH = 3
LEVEL_NAMES = {1: "part", 2: "point", 3: "sub-point"}


@dataclass
class Node:
    id: str
    title: str
    level: int
    spans: list[tuple[int, int]]
    children: list["Node"] = field(default_factory=list)

    def walk(self):
        """This node, then its descendants, in reading order."""
        yield self
        for child in self.children:
            yield from child.walk()

    def own_units(self) -> list[int]:
        return [u for first, last in self.spans for u in range(first, last + 1)]

    def all_units(self) -> list[int]:
        return [u for node in self.walk() for u in node.own_units()]

    def to_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "level": self.level,
                "spans": [list(s) for s in self.spans],
                "children": [c.to_dict() for c in self.children]}

    @classmethod
    def from_dict(cls, data: dict) -> "Node":
        return cls(data["id"], data["title"], data["level"], [tuple(s) for s in data["spans"]],
                   [cls.from_dict(c) for c in data["children"]])


@dataclass
class Outline:
    title: str
    parts: list[Node]
    noise: list[tuple[int, int]]
    terms: list[dict]
    units: list[str]
    fallback: str | None = None  # why the outline was built by code instead of the model

    def noise_units(self) -> list[int]:
        return [u for first, last in self.noise for u in range(first, last + 1)]

    def node(self, node_id: str) -> Node:
        for part in self.parts:
            for node in part.walk():
                if node.id == node_id:
                    return node
        raise KeyError(node_id)

    def text(self, number: int) -> str:
        return self.units[number - 1]

    def to_dict(self) -> dict:
        return {"title": self.title, "parts": [p.to_dict() for p in self.parts],
                "noise": [list(s) for s in self.noise], "terms": self.terms,
                "units": self.units, "fallback": self.fallback}

    @classmethod
    def from_dict(cls, data: dict) -> "Outline":
        return cls(data["title"], [Node.from_dict(p) for p in data["parts"]],
                   [tuple(s) for s in data["noise"]], data["terms"], data["units"],
                   data.get("fallback"))


# ------------------------------------------------------------------- helpers

def ranges(numbers) -> str:
    """[3, 4, 5, 9] -> '3-5, 9'."""
    numbers = sorted(set(numbers))
    out, start, prev = [], None, None
    for n in numbers:
        if start is None:
            start = prev = n
        elif n == prev + 1:
            prev = n
        else:
            out.append(f"{start}-{prev}" if prev > start else str(start))
            start = prev = n
    if start is not None:
        out.append(f"{start}-{prev}" if prev > start else str(start))
    return ", ".join(out)


def _words(outline_units: list[str], numbers) -> int:
    return sum(len(outline_units[n - 1].split()) for n in numbers)


def _spans(value, where: str, n: int, errors: list[str]) -> list[tuple[int, int]]:
    if value in (None, []):
        return []
    if not isinstance(value, list):
        errors.append(f'{where}: "units" must be a list of [first, last] pairs')
        return []
    spans = []
    for item in value:
        if isinstance(item, int) and not isinstance(item, bool):
            item = [item, item]
        if (not isinstance(item, list) or len(item) != 2
                or not all(isinstance(x, int) and not isinstance(x, bool) for x in item)
                or not 1 <= item[0] <= item[1] <= n):
            errors.append(f"{where}: {item!r} is not a [first, last] pair between 1 and {n}")
            continue
        spans.append((item[0], item[1]))
    return spans


def _merge_single_children(raw: dict) -> dict:
    """A node with exactly one child says nothing about structure: fold the child into it."""
    units = list(raw.get("units") or [])
    children = raw.get("children") or []
    while isinstance(children, list) and len(children) == 1 and isinstance(children[0], dict):
        units += list(children[0].get("units") or [])
        children = children[0].get("children") or []
    out = dict(raw)
    out["units"] = units
    out["children"] = [_merge_single_children(c) if isinstance(c, dict) else c
                       for c in (children if isinstance(children, list) else [])]
    return out


def _build(raw, node_id: str, level: int, n: int, errors: list[str]) -> Node | None:
    where = f"{LEVEL_NAMES.get(level, 'node')} {node_id}"
    if not isinstance(raw, dict):
        errors.append(f"{where} must be an object with title, units and children")
        return None
    title = str(raw.get("title", "")).strip()
    if not title:
        errors.append(f"{where} has no title")
    spans = _spans(raw.get("units"), where, n, errors)
    children_raw = raw.get("children") or []
    if not isinstance(children_raw, list):
        errors.append(f'{where}: "children" must be a list')
        children_raw = []
    if children_raw and level >= MAX_DEPTH:
        errors.append(f"{where} is at the third level and cannot have children: "
                      "fold them into it (at most three levels: part, point, sub-point)")
        children_raw = []
    children = [c for i, raw_child in enumerate(children_raw, 1)
                if (c := _build(raw_child, f"{node_id}.{i}", level + 1, n, errors)) is not None]
    node = Node(node_id, title, level, spans, children)
    if not node.all_units():
        errors.append(f"{where} ('{title}') has no units")
    return node


# ------------------------------------------------------------------- parse + check

def parse_outline(data, units: list[str], max_part_words: int) -> tuple[Outline | None, list[str]]:
    """Check the model's outline. Returns (outline, []) or (None, errors to send back)."""
    n = len(units)
    if not isinstance(data, dict) or not isinstance(data.get("parts"), list) or not data["parts"]:
        return None, ['the answer must be a JSON object with a non-empty "parts" list']
    errors: list[str] = []
    parts = [p for i, raw in enumerate(data["parts"], 1)
             if (p := _build(_merge_single_children(raw) if isinstance(raw, dict) else raw,
                             str(i), 1, n, errors)) is not None]
    noise = _spans(data.get("noise"), "noise", n, errors)

    count = Counter(u for p in parts for u in p.all_units())
    count.update(u for first, last in noise for u in range(first, last + 1))
    missing = [u for u in range(1, n + 1) if not count[u]]
    double = [u for u, c in count.items() if c > 1]
    if missing:
        errors.append(f"units {ranges(missing)} are in no node and not in noise: "
                      "every unit must appear exactly once")
    if double:
        errors.append(f"units {ranges(double)} appear more than once: every unit must appear "
                      "exactly once")

    for k in range(len(parts) - 1):
        here, after = parts[k].all_units(), parts[k + 1].all_units()
        if here and after and max(here) >= min(after):
            errors.append(f"part {k + 1} holds unit {max(here)} but part {k + 2} starts at unit "
                          f"{min(after)}: parts must follow the lecture's order, each a block of "
                          "consecutive units; reorder only inside a part")
    for part in parts:
        words = _words(units, part.all_units())
        if words > max_part_words:
            errors.append(f"part {part.id} has {words} words; split it into consecutive parts of "
                          f"at most {max_part_words} words")

    terms = []
    raw_terms = data.get("terms") or []
    if not isinstance(raw_terms, list):
        errors.append('"terms" must be a list of {"term", "part"}')
        raw_terms = []
    for item in raw_terms:
        if (not isinstance(item, dict) or not str(item.get("term", "")).strip()
                or not isinstance(item.get("part"), int) or not 1 <= item["part"] <= len(parts)):
            errors.append(f'term {item!r} must be {{"term": "...", "part": a part number '
                          f"from 1 to {len(parts)}}}")
            continue
        terms.append({"term": str(item["term"]).strip(), "part": item["part"]})

    if errors:
        return None, errors
    title = str(data.get("title", "")).strip() or parts[0].title
    return Outline(title, parts, noise, terms, units), []


def fallback_outline(units: list[str], max_part_words: int, reason: str) -> Outline:
    """Parts of about max_part_words / 2 words, no points, no noise: used when the model fails."""
    target = max(1, max_part_words // 2)
    parts, start, words = [], 1, 0
    for number, unit in enumerate(units, 1):
        words += len(unit.split())
        if words >= target or number == len(units):
            index = len(parts) + 1
            parts.append(Node(str(index), f"Part {index}", 1, [(start, number)]))
            start, words = number + 1, 0
    return Outline(parts[0].title if parts else "Lecture", parts, [], [], units, reason)


# ------------------------------------------------------------------- derived views

def noise_by_part(outline: Outline) -> dict[str, list[int]]:
    """Each noise unit goes to the part whose range contains it, else the next part, else the last."""
    bounds = [(p.id, min(p.all_units()), max(p.all_units())) for p in outline.parts]
    result: dict[str, list[int]] = {p.id: [] for p in outline.parts}
    for unit in outline.noise_units():
        target = bounds[-1][0]
        for part_id, low, high in bounds:
            if unit <= high or unit < low:
                target = part_id
                break
        result[target].append(unit)
    return result


def moves(part: Node) -> list[dict]:
    """Spans that the outline placed before material the teacher said earlier (inside one part)."""
    found, latest = [], 0
    for node in part.walk():
        for first, last in node.spans:
            if first < latest:
                found.append({"node": node.id, "title": node.title, "units": [first, last],
                              "after_unit": latest})
            latest = max(latest, last)
    return found


def structure(part: Node) -> dict:
    """The shape the rewrite must have: one section per node, previews for nodes with children."""
    return {
        "sections": [n.id for n in part.walk()],
        "preview_items": [c.id for c in part.children],
        "section_previews": {n.id: [c.id for c in n.children]
                             for n in part.walk() if n is not part and len(n.children) >= 2},
    }


def checklist(part: Node) -> list[dict]:
    return [{"node": n.id, "title": n.title} for n in part.walk()]


def render_outline(outline: Outline, current: str | None = None) -> str:
    lines = [f"Lecture: {outline.title}"]
    for part in outline.parts:
        for node in part.walk():
            mark = "   <- the part you are working on" if node.id == current else ""
            lines.append("  " * (node.level - 1) + f"{node.id} {node.title}{mark}")
    return "\n".join(lines)


def render_part_transcript(outline: Outline, part: Node, noise: list[int]) -> str:
    """The part's units grouped under the outline's headings, with their numbers."""
    lines = []
    for node in part.walk():
        lines.append("#" * (node.level + 2) + f" {node.id} {node.title}")
        lines += [f"[{u}] {outline.text(u)}" for u in node.own_units()]
        lines.append("")
    if noise:
        lines.append("### Units marked as noise (filler, room talk). Ignore them unless the "
                     "feedback says they hold content.")
        lines += [f"[{u}] {outline.text(u)}" for u in noise]
    return "\n".join(lines).strip()


def part_source(outline: Outline, part: Node, noise: list[int]) -> str:
    numbers = sorted(set(part.all_units()) | set(noise))
    return " ".join(outline.text(u) for u in numbers)


def part_words(outline: Outline, part: Node) -> int:
    return _words(outline.units, part.all_units())


def dump(outline: Outline) -> str:
    return json.dumps(outline.to_dict(), ensure_ascii=False, indent=2)
