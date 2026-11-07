"""Finite authored knowledge updates and their frozen transaction wire."""

from typing import TypedDict

from codey.types.json_value import JsonObject


class UpdateReference(TypedDict):
    ref: str
    relation: str


class KnowledgeUpdate(TypedDict):
    key: str
    kind: str
    content: str
    title: str
    aliases: list[str]
    conditions: dict[str, dict[str, str]]
    references: list[UpdateReference]


class KnowledgeBatch(TypedDict):
    updates: list[KnowledgeUpdate]
    scope: str
    scope_id: str
    session_id: str


class MutationPayload(TypedDict):
    value: JsonObject
    expected_revision: int
