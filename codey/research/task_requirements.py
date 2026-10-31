"""Lossless task text units, derived from the original task rather than a model plan."""

from __future__ import annotations

import re
from typing import Any

_BOUNDARY = re.compile(r"[\n,;!?\u3002\uff0c\uff1b\uff01\uff1f]+|\.(?=\s|$)")
_LEGACY_SENTENCE_BOUNDARY = re.compile(r"[\n!?\u3002\uff01\uff1f]+|\.(?=\s|$)")
_CLOSERS = r'''["'’”»）)\]】》」』}]*'''
_SENTENCE_BOUNDARY = re.compile(r"(?:[\n!?\u3002\uff01\uff1f]+|\.(?=" + _CLOSERS + r"(?:\s|$)))" + _CLOSERS)
# Full matches only: any substantive clause or unrecognized modifier keeps the
# original unit in question accounting. Never infer requirements from a plan.
_CHINESE_REPORT = re.compile(
    r"(?:请)?(?:给出|提供|撰写|写出|输出)(?:一份|一篇)?"
    r"(?:(?P<approx>约|大约)(?P<count>[1-9][0-9]{1,5})字的?)?"
    r"(?P<language>中文)?(?:研究结论|研究报告|报告|结论)"
)
_CHINESE_LANGUAGE = re.compile(r"(?:请)?(?:使用|用|以)中文(?:回答|作答|撰写|输出)")


def text_units(text: str, *, sentences: bool = False, legacy_sentence_boundaries: bool = False) -> list[dict[str, Any]]:
    """Retain original offsets, punctuation and whitespace without semantic inference."""
    rows: list[dict[str, Any]] = []
    start = 0
    boundary = _SENTENCE_BOUNDARY if sentences else _BOUNDARY
    if sentences and legacy_sentence_boundaries:
        boundary = _LEGACY_SENTENCE_BOUNDARY
    for end in [*(match.end() for match in boundary.finditer(text)), len(text)]:
        span = text[start:end]
        if span.strip():
            rows.append({"unit": len(rows) + 1, "start": start, "end": end, "text": span})
        start = end
    if not rows:
        raise ValueError("research scope requires nonempty text")
    return rows


def task_requirements(task: str) -> list[dict[str, Any]]:
    """Freeze every nonblank span; text segmentation is not semantic interpretation."""
    return [{"task_ref": f"r{row['unit']}", **{key: row[key] for key in ("start", "end", "text")}}
            for row in text_units(task)]


def report_requirements(task: str) -> list[dict[str, Any]]:
    """Recognize only standalone, explicit output requirements; retain provenance.

    Approximate Chinese lengths count non-whitespace conclusion characters,
    excluding citations, with an inclusive 20% tolerance. Language is a bounded
    script check (Han characters must outnumber Latin letters), not semantics.
    """
    result = []
    for row in task_requirements(task):
        text = row["text"].strip().rstrip("，,；;。.!！?？").strip()
        compact = re.sub(r"\s+", "", text)
        match = _CHINESE_REPORT.fullmatch(compact)
        language_only = _CHINESE_LANGUAGE.fullmatch(compact)
        if match is None and language_only is None:
            continue
        constraints: dict[str, Any] = {"scope": "conclusion", "counting": "non_whitespace_without_citations"}
        if language_only or (match and match["language"]):
            constraints["language"] = "zh"
        if match and match["count"]:
            count = int(match["count"])
            constraints.update(min_characters=(count * 4 + 4) // 5, max_characters=count * 6 // 5)
        # A bare 'give a report' is a known output shape, not a research topic.
        result.append({**row, "constraints": constraints})
    return result


def coverage_requirements(task: str) -> list[dict[str, Any]]:
    format_refs = {row["task_ref"] for row in report_requirements(task)}
    return [row for row in task_requirements(task) if row["task_ref"] not in format_refs]


def task_requirement_context(task: str) -> dict[str, Any]:
    return {"coverage_task_refs": coverage_requirements(task),
            "report_requirements_checked_by_application": report_requirements(task)}


def validate_report_requirements(conclusion: str, task: str, *, from_findings: bool = False) -> None:
    """Check canonical conclusion once, before any report views are published."""
    text = re.sub(r"\[(?:f[1-9][0-9]*|[0-9]+)\]", "", conclusion)
    count = sum(not char.isspace() for char in text)
    repair = ("Revise a local finding against its original passages or select its supported replacement; "
              "done adopts findings verbatim and accepts no answer prose. Never pad with unsupported claims."
              if from_findings else "Revise existing answers; do not add a question or task_refs row.")
    for row in report_requirements(task):
        constraint = row["constraints"]
        ref = row["task_ref"]
        if "min_characters" in constraint and not constraint["min_characters"] <= count <= constraint["max_characters"]:
            raise ValueError(f"Report format {ref}: conclusion has {count} non-whitespace characters; "
                f"required {constraint['min_characters']}..{constraint['max_characters']} (citations, "
                "headings and source tables excluded). " + repair)
        if constraint.get("language") == "zh":
            han = len(re.findall(r"[\u3400-\u9fff]", text))
            latin = len(re.findall(r"[a-zA-Z]", text))
            if not han or han <= latin:
                raise ValueError(f"Report format {ref}: conclusion must be primarily Chinese "
                    "(Han characters must outnumber Latin letters). " + repair)


def validate_task_accounting(coverage: list[dict[str, Any]], task: str) -> None:
    """Check explicit accounting, never whether an answer means what it claims."""
    original = {row["task_ref"] for row in task_requirements(task)}
    expected = {row["task_ref"] for row in coverage_requirements(task)}
    observed: set[str] = set()
    for index, row in enumerate(coverage):
        refs = row.get("task_refs")
        if (not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs)
                or len(refs) != len(set(refs)) or not set(refs) <= original):
            raise ValueError(f"report_sections.coverage[{index}].task_refs must select original task rN ids: "
                             + ", ".join(sorted(original)))
        observed.update(refs)
    if not expected <= observed:
        missing = expected - observed
        raise ValueError("report_sections.coverage must account for every original task unit in task_refs; missing: "
                         + ", ".join(sorted(missing))
                         + ". Give its answer or specific unresolved gap; do not drop the obligation. "
                         "Edit task_refs in the existing question rows; do not append or duplicate qN records. "
                         "Missing original text:\n" + "\n".join(
                             f"{row['task_ref']}: {row['text']!r}" for row in task_requirements(task)
                             if row["task_ref"] in missing))
