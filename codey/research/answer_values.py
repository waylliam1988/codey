"""Frozen whole-answer wire schemas; codecs own validation and copying."""

from typing import NotRequired, TypedDict

from codey.research.knowledge_update_types import KnowledgeUpdate
from codey.research.report_values import CitationBinding


class CitationOccurrence(TypedDict):
    label: str
    start: int
    end: int


class AnswerScopePayload(TypedDict):
    task_digest: str
    aliases: list[list[str]]
    attempt_refs: list[str]
    schema_version: int


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
