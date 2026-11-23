"""Frozen whole-answer wire schemas; codecs own validation and copying."""

from typing import NotRequired, TypedDict

from codey.research.knowledge_update_types import KnowledgeUpdate
from codey.research.report_values import CitationBinding
from codey.types.json_value import JsonObject


def evidence_status_suffix(scope_version: int, state: str) -> str:
    return "\n\n**Evidence status: " + state + ".**" if scope_version >= 10 and state != "answered" else ""


class CitationOccurrence(TypedDict):
    label: str
    start: int
    end: int


class AnswerScopePayload(TypedDict):
    task_digest: str
    aliases: list[list[str]]
    attempt_refs: list[str]
    schema_version: int
    fields: NotRequired[list[JsonObject]]
    declared_fields_ready: NotRequired[bool]
    execution_summary: NotRequired[str]
    execution_refs: NotRequired[list[str]]
    calculations: NotRequired[list[JsonObject]]
    primary_passages: NotRequired[list[str]]


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
