"""Small wire values owned by research receipt codecs."""

from typing import TypedDict

from codey.types.json_value import JsonObject


class SourceSearchLocator(TypedDict):
    offset: int
    page: int | None
    snippet: str


class SourceSearchObservation(TypedDict):
    source_url: str
    query: str
    hits: list[SourceSearchLocator]


class InvestigationRecord(TypedDict):
    tool: str
    ok: bool
    question_ref: str
    args: JsonObject
    passage_refs: list[str]
