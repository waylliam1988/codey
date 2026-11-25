"""Frozen whole-answer wire schemas; codecs own validation and copying."""

from typing import NotRequired, TypedDict

from codey.research.knowledge_update_types import KnowledgeUpdate
from codey.research.report_values import CitationBinding
from codey.types.json_value import JsonObject


def evidence_status_suffix(requires_primary: bool, state: str) -> str:
    return "\n\n**Evidence status: " + state + ".**" if requires_primary and state != "answered" else ""


class CitationOccurrence(TypedDict):
    label: str
    start: int
    end: int


class AnswerScopePayload(TypedDict):
    task_digest: str
    aliases: list[list[str]]
    attempt_refs: list[str]
    schema_version: int
    fields: list[JsonObject]
    declared_fields_ready: bool
    execution_summary: str
    execution_refs: list[str]
    calculations: list[JsonObject]
    primary_passages: list[str] | None


class UnsignedAnswerSubmission(TypedDict):
    schema_version: int
    task_digest: str
    markdown: str
    state: str
    scope: AnswerScopePayload
    citations: list[CitationBinding]
    knowledge_updates: NotRequired[list[KnowledgeUpdate]]


class AnswerSubmission(UnsignedAnswerSubmission):
    submission_digest: str
