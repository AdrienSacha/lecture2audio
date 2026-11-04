"""An independent fact-check of the script, by a small local model of another family.

Every "lecture" sentence of the verified script cites the ideas it states (`ideas`), and every
idea cites its transcript units, so each sentence can be checked against exactly the few units
it claims to come from. MiniCheck (Tang, Laban and Durrett, EMNLP 2024), a 770M fact-checker
that matches GPT-4 on grounding documents at about 400 times lower cost, scores whether those
units support the sentence. It is a second opinion from a model that shares no weights with the
writer or the verifier, so it does not share their blind spots (Ji, 2026, on correlated errors
of judges built on one model). Its findings are reported, never used to rewrite: speech
recognition noise in the units can make a correct sentence look unsupported.

Optional: `uv pip install -e ".[factcheck]"`. The model is imported inside the function.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from .outline import Outline

THRESHOLD = 0.5


def _evidence(outline: Outline, sentence: dict, by_id: dict, part_text: str) -> str:
    """The transcript units the sentence's ideas cite; the whole part when it cites none."""
    units: list[int] = []
    for cited in sentence.get("ideas") or []:
        idea = by_id.get(str(cited))
        if not idea:
            continue
        found = idea.get("units") or [u for n in idea.get("nodes", [])
                                      for u in _node_units(outline, n)]
        units += [u for u in found if u not in units]
    return " ".join(outline.text(u) for u in sorted(units)) if units else part_text


def _node_units(outline: Outline, node_id: str) -> list[int]:
    try:
        return outline.node(str(node_id)).own_units()
    except KeyError:
        return []


def run_factcheck(work: Path, outline: Outline, records: list[dict], ideas: list[dict],
                  log: Callable[[str], None] = print,
                  model: str = "flan-t5-large") -> dict | None:
    """{"model", "checked", "unsupported": [{part, sentence, text, score}]}, cached per part,
    or None when MiniCheck is not installed."""
    try:
        from minicheck.minicheck import MiniCheck
    except ImportError:
        return None
    directory = Path(work) / "factcheck"
    directory.mkdir(parents=True, exist_ok=True)
    by_id = {str(i["id"]): i for i in ideas}
    scorer = None
    checked, unsupported = 0, []
    for record in records:
        cache = directory / f"part_{int(record['part']):03d}.json"
        if cache.exists():
            result = json.loads(cache.read_text(encoding="utf-8"))
        else:
            part_text = record.get("source", "")
            script = [s for section in record["sections"] for s in section["sentences"]]
            pairs = [(k, s) for k, s in enumerate(script, 1) if s["origin"] == "lecture"]
            if scorer is None:
                log(f"[factcheck] loading MiniCheck ({model})")
                scorer = MiniCheck(model_name=model,
                                   cache_dir=str(Path.home() / ".cache" / "minicheck"))
            docs = [_evidence(outline, s, by_id, part_text) for _, s in pairs]
            labels, scores, _, _ = scorer.score(docs=docs, claims=[s["text"] for _, s in pairs]) \
                if pairs else ([], [], None, None)
            result = {"checked": len(pairs), "unsupported": [
                {"part": record["part"], "sentence": k, "text": s["text"],
                 "score": round(float(p), 3)}
                for (k, s), p in zip(pairs, scores) if float(p) < THRESHOLD]}
            cache.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        checked += result["checked"]
        unsupported += result["unsupported"]
    log(f"[factcheck] {checked} sentence(s) checked, {len(unsupported)} without support")
    return {"model": model, "checked": checked, "unsupported": unsupported}
