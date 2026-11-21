"""A made-up run for the app's control room: `calls.jsonl`, `events.jsonl`, the outline, the
idea ledger, the script and the cards, in the shapes the program writes them, with a research
tool loop (turns, searches, pages, a refused page), parallel parts, a repair, a gate that
rejects an answer and a model error. No model and no network are involved.

    python tests/room_fixture.py OUT [--name demo] [--parts 6] [--live SPEED]

Without --live the whole run is written at once, dated in the past. With --live the lines are
appended in real time (SPEED times faster than the run itself), so the screen can be watched
while it fills, the ledger is rewritten as parts finish, as the real one is.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

WORDS = ("register bit mask shift device status flag serial port byte clear set toggle "
         "operation value field read write configure interrupt clock signal").split()


def sentence(rng: random.Random, n: int = 12) -> str:
    return " ".join(rng.choice(WORDS) for _ in range(n)).capitalize() + "."


class Script:
    """A timeline of actions: (seconds from the start, function)."""

    def __init__(self, work: Path, rng: random.Random):
        self.work, self.rng, self.actions, self.ids = work, rng, [], 0

    def at(self, t: float, action) -> None:
        self.actions.append((t, len(self.actions), action))

    def new_id(self) -> str:
        self.ids += 1
        return f"{self.rng.getrandbits(32):08x}{self.ids:04x}"

    def line(self, t: float, name: str, record: dict) -> None:
        def write(base: float, record=record):
            stamp = datetime.fromtimestamp(base + t, timezone.utc).isoformat(timespec="milliseconds")
            data = dict(record)
            if name == "calls.jsonl":
                data = {"time": stamp, **data}
            else:
                data["time"] = round(base + t, 3)
            with open(self.work / name, "a", encoding="utf-8") as out:
                out.write(json.dumps(data, ensure_ascii=False) + "\n")
        self.at(t, write)

    def stage(self, t0: float, t1: float, name: str) -> None:
        self.line(t0, "events.jsonl", {"stage": name, "state": "running"})
        self.line(t1, "events.jsonl", {"stage": name, "state": "done", "seconds": round(t1 - t0, 1)})

    def model(self, t0: float, seconds: float, ctx: dict, system: str, user: str,
              answer: str = "{}", reasoning: str = "", tools=None, error: str | None = None,
              model: str = "deepseek-flash") -> str:
        call_id = self.new_id()
        meta = {"id": call_id, **{k: v for k, v in ctx.items() if v is not None}, "model": model}
        request = {"host": "https://api.deepseek.com", "model": model,
                   "messages": [{"role": "system", "content": system},
                                {"role": "user", "content": user}],
                   "max_tokens": 48000}
        self.line(t0, "calls.jsonl", {"kind": "model", "phase": "start", **meta, "request": request})
        chars = 0
        for k in range(1, int(seconds)):
            chars += self.rng.randint(80, 400)
            self.line(t0 + k, "calls.jsonl", {"kind": "model", "phase": "progress", "id": call_id,
                                               "chars": chars // 3, "thinking": chars,
                                               "tail": sentence(self.rng, 8), "seconds": k})
        if error:
            self.line(t0 + seconds, "calls.jsonl", {"kind": "model", "phase": "end", **meta,
                                                     "error": error, "seconds": seconds})
            return call_id
        message = {"role": "assistant", "content": answer, "reasoning_content": reasoning}
        if tools:
            message["tool_calls"] = tools
        self.line(t0 + seconds, "calls.jsonl", {
            "kind": "model", "phase": "end", **meta, "seconds": seconds,
            "answer": {"id": "c", "object": "chat.completion", "model": model,
                       "choices": [{"index": 0, "finish_reason": "stop", "message": message}],
                       "usage": {"prompt_tokens": len(user) // 3 + 900,
                                 "completion_tokens": len(answer) // 3 + len(reasoning) // 3}}})
        return call_id

    def gate(self, t: float, ctx: dict, parent: str, what: str, errors=()) -> None:
        self.line(t, "calls.jsonl", {"kind": "gate", "phase": "point", "id": self.new_id(),
                                      **ctx, "parent": parent, "what": what,
                                      "ok": not errors, "errors": list(errors)})

    def tool(self, t0: float, seconds: float, kind: str, ctx: dict, request: dict, end: dict) -> str:
        call_id = self.new_id()
        self.line(t0, "calls.jsonl", {"kind": kind, "phase": "start", "id": call_id, **ctx,
                                       "request": request})
        self.line(t0 + seconds, "calls.jsonl", {"kind": kind, "phase": "end", "id": call_id,
                                                 **ctx, "seconds": seconds, **end})
        return call_id


def build(work: Path, parts: int, rng: random.Random) -> Script:
    s = Script(work, rng)
    titles = [f"{rng.choice(WORDS).capitalize()} {rng.choice(WORDS)} and {rng.choice(WORDS)}"
              for _ in range(parts)]
    outline = {"title": "Bit manipulation on a serial device",
               "parts": [{"title": t, "units": [], "children": [
                   {"title": f"{t}, first point", "units": [[1, 2]], "children": []},
                   {"title": f"{t}, second point", "units": [[3, 4]], "children": []}]}
                         for t in titles], "noise": [], "terms": []}
    ideas = [{"idea_id": f"T{p}.{k}", "node_id": f"{p}.{1 + (k > 4)}",
              "kind": rng.choice(["claim", "step", "definition", "example", "formula"]),
              "statement": sentence(rng, rng.randint(8, 16)), "source_units": str(k),
              "covered_in_card": "", "status": "pending"}
             for p in range(1, parts + 1) for k in range(1, rng.randint(6, 11))]
    status: dict[str, tuple[str, str]] = {}

    def ledger(t: float, updates: dict[str, tuple[str, str]]):
        def write(_base):
            status.update(updates)
            path = work / "ideas.csv"
            tmp = path.with_name("ideas.csv.tmp")
            with open(tmp, "w", newline="", encoding="utf-8") as out:
                writer = csv.DictWriter(out, fieldnames=list(ideas[0]), lineterminator="\n")
                writer.writeheader()
                for row in ideas:
                    st, card = status.get(row["idea_id"], ("pending", ""))
                    writer.writerow(dict(row, status=st, covered_in_card=card))
            os.replace(tmp, path)
        s.at(t, write)

    def save(t: float, name: str, data: dict):
        def write(_base):
            path = work / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        s.at(t, write)

    s.line(0, "calls.jsonl", {"kind": "run", "phase": "open", "pid": 4242})
    s.stage(0, 1, "transcribe")
    # outline
    s.stage(1, 40, "outline")
    c = s.model(1.5, 37, {"stage": "outline", "role": "planner", "prompt": "outline", "seat": "writer"},
                "You build the outline of a lecture.", "## UNITS\n" + sentence(rng, 60),
                json.dumps(outline)[:3000], sentence(rng, 120))
    s.gate(38.6, {"stage": "outline", "role": "planner"}, c, "outline")
    save(39, "outline.json", outline)
    # gaps, then ideas in parallel parts
    s.stage(40, 70, "gaps")
    c = s.model(40.5, 28, {"stage": "gaps", "role": "gap search", "prompt": "coverage", "seat": "writer"},
                "You find the gaps of a lecture.", sentence(rng, 90),
                json.dumps({"gaps": [{"id": f"G{k}", "query": sentence(rng, 5)} for k in range(1, 5)]}),
                sentence(rng, 80))
    s.gate(68.6, {"stage": "gaps", "role": "gap search"}, c, "gap search")
    s.stage(70, 70 + 25 * ((parts + 2) // 3), "ideas")
    for p in range(1, parts + 1):
        t0 = 70.5 + 25 * ((p - 1) // 3)
        ctx = {"stage": "ideas", "role": "idea lister", "prompt": "ideas", "seat": "writer",
               "part": str(p), "part_title": titles[p - 1]}
        c = s.model(t0, rng.uniform(12, 23), ctx, "You list the atomic ideas of one part.",
                    "## TRANSCRIPT OF THIS PART\n" + sentence(rng, 80),
                    json.dumps({"ideas": [i["statement"] for i in ideas if i["idea_id"].startswith(f"T{p}.")]}),
                    sentence(rng, 60))
        s.gate(t0 + 23.5, {k: ctx[k] for k in ("stage", "role", "part")}, c, f"ideas of part {p}")
    t = 70 + 25 * ((parts + 2) // 3)
    ledger(t, {})
    # research: four gaps in parallel, each a tool loop
    s.stage(t, t + 60, "research")
    for g in range(1, 5):
        part = str(rng.randint(1, parts))
        base = {"stage": "research", "role": "researcher", "prompt": "research", "seat": "writer",
                "gap": f"G{g}", "part": part, "part_title": titles[int(part) - 1]}
        conv, t0, asker = s.new_id(), t + 0.5 + g * 1.3, None
        query = f"{rng.choice(WORDS)} {rng.choice(WORDS)} definition"
        url = f"https://en.wikipedia.org/wiki/{rng.choice(WORDS).capitalize()}_{g}"
        for turn in (1, 2, 3):
            ctx = dict(base, conversation=conv, turn=turn, parent=asker)
            if turn == 1:
                tools = [{"id": "t1", "type": "function", "function": {"name": "web_search", "arguments": json.dumps({"query": query})}}]
            elif turn == 2:
                tools = [{"id": "t2", "type": "function", "function": {"name": "fetch_page", "arguments": json.dumps({"url": url})}}]
            else:
                tools = None
            asker = s.model(t0, 8, ctx, "You research one gap of a lecture.", sentence(rng, 40),
                            "" if tools else json.dumps({"found": True, "facts": [{"quote": sentence(rng, 10), "url": url}]}),
                            sentence(rng, 50), tools)
            t0 += 8.3
            tctx = dict(base, conversation=conv, turn=turn, parent=asker)
            if turn == 1:
                s.tool(t0, 1.4, "search", dict(tctx, tool="web_search"),
                       {"url": "https://api.deepseek.com/search", "payload": {"query": query}},
                       {"answer": {"results": [{"url": url, "title": "Wikipedia: " + query, "text": sentence(rng)},
                                               {"url": f"https://example.edu/notes/{g}", "title": "Course notes", "text": sentence(rng)}]}})
                t0 += 1.6
            elif turn == 2:
                if g == 3:  # a refused page
                    s.tool(t0, 0.8, "page", dict(tctx, tool="fetch_page"), {"url": f"https://paywalled.example.com/{g}"},
                           {"error": "FetchError: could not read https://paywalled.example.com/3: HTTP Error 403: Forbidden"})
                    t0 += 1
                s.tool(t0, 2.2, "page", dict(tctx, tool="fetch_page"), {"url": url},
                       {"status": 200, "type": "text/html", "bytes": rng.randint(40000, 200000)})
                t0 += 2.4
    t += 60
    # rewrite: writer, checker, gates, a repair, a rejected answer, a model error
    width = 3
    rounds = (parts + width - 1) // width
    s.stage(t, t + rounds * 95, "rewrite")
    for p in range(1, parts + 1):
        t0 = t + 0.5 + 95 * ((p - 1) // width) + rng.uniform(0, 3)
        part = {"part": str(p), "part_title": titles[p - 1], "stage": "rewrite"}
        mine = [i["idea_id"] for i in ideas if i["idea_id"].startswith(f"T{p}.")]
        attempts = 2 if p % 4 == 2 else 1
        sentences = []
        for attempt in range(1, attempts + 1):
            wctx = dict(part, role="writer", prompt="rewrite", seat="writer", attempt=attempt,
                        repair=f"{p}.1" if attempt > 1 else None)
            if p == 3 and attempt == 1:  # an overloaded server, then the same call again
                s.model(t0, 3, wctx, "You rewrite one part of a lecture.", sentence(rng, 120),
                        error="LLMError: HTTP 529 from deepseek:deepseek-flash: overloaded")
                t0 += 4
            sentences = [{"origin": "lecture", "text": sentence(rng, 14), "ideas": [i]} for i in mine]
            c = s.model(t0, rng.uniform(18, 30), wctx, "You rewrite one part of a lecture.",
                        "## TRANSCRIPT OF THIS PART\n" + sentence(rng, 150),
                        json.dumps({"sections": [{"node": f"{p}.1", "sentences": sentences}]})[:4000],
                        sentence(rng, 150))
            t0 += 31
            if p == 5 and attempt == 1:
                s.gate(t0, dict(part, role="writer"), c, f"script of part {p}",
                       ["not valid JSON (Expecting ',' delimiter: line 1 column 812)"])
                c = s.model(t0 + .3, 14, dict(wctx, **{"try": 2}), "You rewrite one part of a lecture.",
                            sentence(rng, 150) + "\n\n## PREVIOUS ANSWER REJECTED", "{}", sentence(rng, 40))
                t0 += 15
            s.gate(t0, dict(part, role="writer"), c, f"script of part {p}")
            vctx = dict(part, role="checker", prompt="verify", seat="checker", attempt=attempt,
                        repair=wctx["repair"])
            c = s.model(t0 + .4, rng.uniform(14, 22), vctx, "You are a strict reviewer.",
                        "## CHECKLIST (JSON)\n" + sentence(rng, 60),
                        json.dumps({"issues": [] if attempt == attempts else [{"type": "missing", "node": f"{p}.1"}]}),
                        sentence(rng, 90), model="deepseek-v4-pro")
            t0 += 23
            s.gate(t0, dict(part, role="checker"), c, f"verification of part {p}")
            t0 += 1
        done = {i: ("disputed" if k == 2 and p % 3 == 0 else "missing_script" if k == 4 and p % 5 == 1
                    else "scripted", "") for k, i in enumerate(mine)}
        save(t0, f"rewrite/part_{p:03d}.json", {
            "part": str(p), "title": titles[p - 1],
            "sections": [{"node": f"{p}.1", "sentences": sentences}],
            "ideas": [{"idea": i, "status": "taught" if st != "missing_script" else "missing",
                       "detail": "" if st == "scripted" else sentence(rng, 10)} for i, (st, _) in done.items()]})
        ledger(t0 + .1, done)
    t += rounds * 95
    s.stage(t, t + 20, "spoken")
    for p in range(1, parts + 1):
        t0 = t + 0.5 + 6 * ((p - 1) // width)
        s.model(t0, 5, {"stage": "spoken", "role": "voice script", "prompt": "spoken", "seat": "writer",
                        "part": str(p), "part_title": titles[p - 1]},
                "You write the exact words a voice should say.", sentence(rng, 60), sentence(rng, 60), "")
    t += 20
    s.stage(t, t + rounds * 60, "course")
    for p in range(1, parts + 1):
        t0 = t + 0.5 + 60 * ((p - 1) // width)
        part = {"part": str(p), "part_title": titles[p - 1], "stage": "course"}
        c = s.model(t0, 25, dict(part, role="course writer", prompt="course", seat="writer", attempt=1),
                    "You write the course material.", sentence(rng, 100), sentence(rng, 80), sentence(rng, 60))
        s.gate(t0 + 25.2, dict(part, role="course writer"), c, f"cards of part {p}")
        c = s.model(t0 + 26, 20, dict(part, role="course judge", prompt="course_check", seat="checker", attempt=1),
                    "You check the course material.", sentence(rng, 100), "{}", sentence(rng, 60),
                    model="deepseek-v4-pro")
        s.gate(t0 + 46.2, dict(part, role="course judge"), c, f"judgement of part {p}")
        mine = [i["idea_id"] for i in ideas if i["idea_id"].startswith(f"T{p}.")]
        cards = [{"id": f"{p}.{k}", "title": sentence(rng, 4)[:-1]} for k in range(1, 4)]
        save(t0 + 47, f"course/part_{p:03d}.json", {"part": str(p), "cards": cards})
        updates = {}
        for k, i in enumerate(mine):
            if k == 4 and p % 5 == 1:
                updates[i] = ("missing_cards", "")
            elif k == 2 and p % 3 == 0:
                updates[i] = ("disputed", cards[k % 3]["id"])
            else:
                updates[i] = ("verified", cards[k % 3]["id"])
        ledger(t0 + 47.1, updates)
    t += rounds * 60
    s.stage(t, t + 2, "ledger")
    return s


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("out")
    parser.add_argument("--name", default="room-demo")
    parser.add_argument("--parts", type=int, default=6)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--live", type=float, default=0, help="append in real time, SPEED times faster")
    args = parser.parse_args(argv)
    work = Path(args.out) / args.name
    work.mkdir(parents=True, exist_ok=True)
    for name in ("calls.jsonl", "events.jsonl", "ideas.csv", "outline.json", "report.md"):
        (work / name).unlink(missing_ok=True)
    for folder in ("rewrite", "course"):
        for path in (work / folder).glob("*.json"):
            path.unlink()
    script = build(work, args.parts, random.Random(args.seed))
    actions = sorted(script.actions)
    total = actions[-1][0]
    if not args.live:
        base = time.time() - total - 3600
        for _t, _k, action in actions:
            action(base)
        (work / "report.md").write_text("# Report\n\nA made-up run for the control room.\n", encoding="utf-8")
        print(f"{work}: {len(actions)} actions, {total:.0f} s of run")
        return 0
    base = time.time()
    began = time.monotonic()
    for t, _k, action in actions:
        wait = t / args.live - (time.monotonic() - began)
        if wait > 0:
            time.sleep(wait)
        action(base + (time.monotonic() - began) - t)  # dated now
    print(f"{work}: done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
