"""Bounded question coverage values and rendering; no execution or storage."""

from __future__ import annotations

import re
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
    rows = []
    seen = set()
    for index, row in enumerate(raw):
        path = f"report_sections.coverage[{index}]"
        if not isinstance(row, dict) or set(row) != {"question_ref", "state", "answer", "findings", "investigations"}:
            raise ValueError(path + " requires question_ref, state, answer (or specific gap), findings, investigations")
        qid = row["question_ref"]
        if not isinstance(qid, str) or re.fullmatch(r"q[1-8]", qid) is None or qid in seen:
            raise ValueError(path + ".question_ref must be a distinct q1..q8")
        seen.add(qid)
        if not isinstance(row["state"], str) or row["state"] not in QUESTION_STATES:
            raise ValueError(path + ".state must be evidence_found, partial, not_investigated or unanswered_after_search")
        if not isinstance(row["answer"], str) or not 1 <= len(row["answer"].strip()) <= 4000:
            raise ValueError(path + ".answer must state the answer or the specific unresolved gap (1..4000 characters)")
        findings = row["findings"]
        if (not isinstance(findings, list) or len(findings) > 16
                or any(not isinstance(ref, str) or re.fullmatch(r"f[1-9][0-9]*", ref) is None for ref in findings)
                or len(set(findings)) != len(findings)):
            raise ValueError(path + ".findings requires exact raw finding references, e.g. [\"f1\"], without brackets in ids")
        if bool(findings) != (row["state"] in {"evidence_found", "partial"}):
            raise ValueError(path + ": evidence_found/partial require findings; gap states require an empty findings array")
        attempts = row["investigations"]
        if (not isinstance(attempts, list) or len(attempts) > 32
                or any(not isinstance(ref, str) or re.fullmatch(r"a[1-9][0-9]*", ref) is None for ref in attempts)
                or len(set(attempts)) != len(attempts)):
            raise ValueError(path + ".investigations requires actual investigation references, e.g. [\"a1\"]")
        if row["state"] == "not_investigated" and attempts or row["state"] == "unanswered_after_search" and not attempts:
            raise ValueError(path + ": not_investigated needs no investigations; unanswered_after_search needs actual investigations")
        rows.append(dict(row))
    return rows


def render_coverage(raw: object, questions: tuple[str, ...] = ()) -> str:
    lines = []
    for row in sorted(coverage_rows(raw), key=lambda value: int(value["question_ref"][1:])):
        index = int(row["question_ref"][1:]) - 1
        question = questions[index] if index < len(questions) else row["question_ref"]
        refs = "".join(f"[{ref}]" for ref in row["findings"])
        attempts = "；调查回执：" + ", ".join(row["investigations"]) if row["investigations"] else ""
        lines.append(f"{row['question_ref']} · {question}\n{QUESTION_STATES[row['state']]}：{row['answer']} {refs}{attempts}".rstrip())
    return "\n\n".join(lines)
