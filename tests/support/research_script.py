"""Explicit payload builders for scripted providers; no execution or repair logic."""
from __future__ import annotations

import hashlib

from codey.research.passages import passage_ref


def answer_args(answer: str, *, finding: str = "f1") -> dict:
    return {"report_sections": {"coverage": [{
        "question_ref": "q1", "state": "partial", "answer": answer,
        "findings": [finding], "investigations": [], "task_refs": ["r1"],
    }]}}


def selection_args(*, finding: str = "f1") -> dict:
    return {"report_sections": {"coverage": [{
        "question_ref": "q1", "state": "partial", "findings": [finding], "task_refs": ["r1"],
    }]}}


def finding_args(url: str, text: str, *, claim: str | None = None,
                 start: int = 0, end: int | None = None) -> dict:
    version = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return {"question_ref": "q1", "claim": claim or text,
            "passage_ref": passage_ref(url, version, start, len(text) if end is None else end)}


def helium_case() -> dict:
    """A modern positive case, separate from the immutable legacy scripts."""
    import json

    text = "Helium is separated from natural gas streams."
    calls = [
        ("web_search", {"query": "helium production"}),
        ("open_result", {"result_id": "r1"}),
        ("submit_research_answer", {"state": "answered", "markdown": text + " [e1]."}),
    ]
    return {"id": "research/current-evidence-synthesis", "boundary": "research_loop",
            "contract": "whole-answer", "max_turns": 3,
            "replies": [json.dumps({"tool": name, "args": args}) for name, args in calls]}
