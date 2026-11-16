"""Explicit Markdown fields projected from original read receipts, never inferred."""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import Field, dataclass, fields
from typing import TYPE_CHECKING

from codey.policies.tool_requirement import ToolObservation, ToolSuccessRequirement, _contains_value, report_read_scope
from codey.research.ledger_receipts import source_observation
from codey.runtime.core.models import ToolResult
from codey.types.json_value import (
    JsonObject,
    JsonValue,
    decode_json_object,
    require_json_boolean,
    require_json_string,
    require_json_strings,
)

if TYPE_CHECKING:
    from codey.research.answer_values import CitationOccurrence


@dataclass(frozen=True)
class AnswerField:
    pointer: str
    label: str
    value: str | None
    labels: tuple[str, ...]
    feedback: str
    allow_missing: bool

    def to_payload(self) -> JsonObject:
        return {"pointer": self.pointer, "label": self.label, "value": self.value,
                "labels": list(self.labels), "feedback": self.feedback, "allow_missing": self.allow_missing}

    @classmethod
    def from_payload(cls, raw: object) -> AnswerField:
        row = decode_json_object(raw)
        members: tuple[Field[object], ...] = fields(cls)
        if set(row) != {member.name for member in members} or not (row["value"] is None or isinstance(row["value"], str)):
            raise ValueError("invalid frozen answer field")
        return cls(require_json_string(row["pointer"], "pointer"), require_json_string(row["label"], "label"),
                   row["value"], tuple(require_json_strings(row["labels"], "labels")),
                   require_json_string(row["feedback"], "feedback"), require_json_boolean(row["allow_missing"], "allow_missing"))


def freeze_field(rule: ToolSuccessRequirement, observation: ToolObservation,
                 results: Mapping[str, ToolResult], aliases: tuple[tuple[str, str], ...]) -> AnswerField:
    """Pin the actual value and the read passages containing it in this request."""
    value = observation.value
    feedback = observation.evaluation.feedback
    passages: set[str] = set()
    if observation.evaluation.passed:
        for ref in observation.result_refs:
            result = results[ref]
            _source, text, _pages = source_observation(result.canonical.get("research_observation"))
            scope = report_read_scope(result, rule.browser_text, include_missing=True) if rule.browser_text is not None else None
            lower = scope.start if scope is not None else None
            upper = scope.end if scope is not None else None
            if scope is not None and lower is not None and isinstance(value, str) and rule.browser_text is not None:
                lower += scope.text.index(rule.browser_text.prefix) + len(rule.browser_text.prefix)
                upper = lower + len(value)
            rows = result.canonical.get("read_passages")
            for raw in rows if isinstance(rows, list) else list[JsonValue]():
                row = decode_json_object(raw)
                start, end = row.get("start"), row.get("end")
                if (type(start) is int and type(end) is int
                        and lower is not None and upper is not None
                        and (start <= lower < upper <= end if value is not None else start < upper and end > lower)
                        and (value is None or isinstance(value, str) and value in text[start:end])):
                    passages.add(require_json_string(row["ref"], "passage ref"))
    labels = tuple(label for label, ref in aliases if ref in passages)
    if not feedback and (not labels or value is not None and not isinstance(value, str)):
        feedback = "The selected field is not covered by an original task-read passage. Read the required region first."
    return AnswerField(rule.answer_pointer or "", rule.report_label, value if isinstance(value, str) else None,
                       labels, feedback, rule.report_allow_missing)


def _literal(value: str) -> str:
    fence = "`" * (1 + max((len(match[0]) for match in re.finditer(r"`+", value)), default=0))
    return fence + " " + value + " " + fence


def render_fields(fields: tuple[AnswerField, ...], markdown: str, state: str) -> str:
    """Expand explicit slots before citation offsets/digests; validate direct prose too."""
    for field in fields:
        if field.feedback:
            raise ValueError(field.label + ": " + field.feedback)
        if field.value is None and (state == "answered" or not field.allow_missing):
            raise ValueError(field.label + ": not established. Report the declared gap as partial/unresolved; hard execution requirements still apply.")
        rendered = (_literal(field.label) + ": " + _literal(field.value) + " "
                    + "".join(f"[{label}]" for label in field.labels) if field.value is not None
                    else _literal(field.label) + ": Not established from the inspected source. "
                    + "".join(f"[{label}]" for label in field.labels))
        markdown = markdown.replace("{{field:" + field.pointer + "}}", rendered)
    return markdown


def validate_fields(fields: tuple[AnswerField, ...], markdown: str,
                    citations: Sequence[CitationOccurrence]) -> None:
    """Use the same whole-document citation positions as the published report."""
    for field in fields:
        lines = (markdown[markdown.rfind("\n", 0, row["start"]) + 1:
                          markdown.find("\n", row["end"]) if "\n" in markdown[row["end"]:] else len(markdown)]
                 for row in citations if row["label"] in field.labels)
        if field.value is None:
            covered = any(_literal(field.label) + ": Not established from the inspected source." in line for line in lines)
        else:
            covered = any(_contains_value(line, field.value) for line in lines)
        if not covered:
            raise ValueError(field.label + ": missing exact value or its required source version. "
                             "Repair only the answer using {{field:" + field.pointer + "}}; no new click or edit is needed.")
