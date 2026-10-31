"""Explicit payload builders for scripted providers; no execution or repair logic."""
from __future__ import annotations

import hashlib

from codey.research.passages import passage_ref


def answer_args(answer: str, *, finding: str = "f1") -> dict:
    return {"report_sections": {"coverage": [{
        "question_ref": "q1", "state": "partial", "answer": answer,
        "findings": [finding], "investigations": [], "task_refs": ["r1"],
    }]}}


def finding_args(url: str, text: str, *, claim: str | None = None,
                 start: int = 0, end: int | None = None) -> dict:
    version = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return {"question_ref": "q1", "claim": claim or text,
            "passage_ref": passage_ref(url, version, start, len(text) if end is None else end)}


def helium_case() -> dict:
    """A modern positive case, separate from the immutable legacy scripts."""
    import json

    url = "https://example.com/helium"
    text = "Helium is separated from natural gas streams."
    calls = [
        ("research_plan", {"questions": ["How is helium obtained?"]}),
        ("web_search", {"query": "helium production", "question_ref": "q1"}),
        ("open_result", {"result_id": "r1", "question_ref": "q1"}),
        ("save_finding", finding_args(url, text)),
        ("done", answer_args(text)),
    ]
    return {"id": "research/current-evidence-synthesis", "boundary": "research_loop",
            "max_turns": 5, "replies": [json.dumps({"tool": name, "args": args}) for name, args in calls]}
