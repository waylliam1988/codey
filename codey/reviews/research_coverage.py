"""Bounded question coverage values and rendering; no execution or storage."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

QUESTION_STATES = {
    "evidence_found": "已获得相关证据",
    "partial": "已有部分回答",
    "not_investigated": "尚未调查",
    "unanswered_after_search": "调查过但仍无法回答",
}


def coverage_rows(raw: object) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not 1 <= len(raw) <= 8:
        raise ValueError("report_sections.coverage requires 1..8 question records")
    rows: list[dict[str, Any]] = []
    seen = set()
    for index, row in enumerate(raw):
        path = f"report_sections.coverage[{index}]"
        keys = {"question_ref", "state", "answer", "findings", "investigations"}
        if not isinstance(row, dict) or set(row) not in (keys, keys | {"task_refs"}):
            raise ValueError(path + " requires question_ref, state, answer (or specific gap), findings, investigations")
        qid = row["question_ref"]
        if not isinstance(qid, str) or re.fullmatch(r"q[1-8]", qid) is None:
            raise ValueError(path + ".question_ref must be a distinct q1..q8")
        if qid in seen:
            previous = next(n for n, value in enumerate(rows) if value["question_ref"] == qid)
            raise ValueError(path + f".question_ref must be a distinct q1..q8: {qid} already appears at "
                             f"coverage[{previous}]. Edit that existing record's task_refs to repair accounting; "
                             "remove the duplicate row and keep each registered question exactly once.")
        seen.add(qid)
        if "task_refs" in row and (not isinstance(row["task_refs"], list) or len(row["task_refs"]) > 512
                or any(not isinstance(ref, str) or re.fullmatch(r"r[1-9][0-9]*", ref) is None for ref in row["task_refs"])
                or len(set(row["task_refs"])) != len(row["task_refs"])):
            raise ValueError(path + ".task_refs requires distinct original task rN ids")
        if not isinstance(row["state"], str) or row["state"] not in QUESTION_STATES:
            raise ValueError(path + ".state must be evidence_found, partial, not_investigated or unanswered_after_search")
        if not isinstance(row["answer"], str) or not 1 <= len(row["answer"].strip()) <= 4000:
            raise ValueError(path + ".answer must state the answer or the specific unresolved gap (1..4000 characters)")
        findings = row["findings"]
        if (not isinstance(findings, list) or len(findings) > 16
                or any(not isinstance(ref, str) or re.fullmatch(r"f[1-9][0-9]*", ref) is None for ref in findings)
                or len(set(findings)) != len(findings)):
            raise ValueError(path + ".findings requires exact raw finding references, e.g. [\"f1\"], without brackets in ids")
        attempts = row["investigations"]
        if (not isinstance(attempts, list) or len(attempts) > 32
                or any(not isinstance(ref, str) or re.fullmatch(r"a[1-9][0-9]*", ref) is None for ref in attempts)
                or len(set(attempts)) != len(attempts)):
            raise ValueError(path + ".investigations requires actual investigation references, e.g. [\"a1\"]")
        # Evidence sufficiency and state consistency belong to the completion
        # check. They can require investigation, not just resending formatted JSON.
        rows.append(dict(row))
    return rows


def render_coverage(raw: object, questions: tuple[str, ...] = (), *,
                    render_ref: Callable[[str], str] | None = None) -> str:
    lines = []
    for row in sorted(coverage_rows(raw), key=lambda value: int(value["question_ref"][1:])):
        index = int(row["question_ref"][1:]) - 1
        question = questions[index] if index < len(questions) else row["question_ref"]
        refs = "".join(render_ref(ref) if render_ref else f"[{ref}]" for ref in row["findings"])
        attempts = "；调查回执：" + ", ".join(row["investigations"]) if row["investigations"] else ""
        lines.append(f"{row['question_ref']} · {question}\n{QUESTION_STATES[row['state']]}：{row['answer']} {refs}{attempts}".rstrip())
    return "\n\n".join(lines)
