"""Completion context input forms keep receipt-bound resources."""

from types import SimpleNamespace

import pytest

from codey.operations.completion_context import CompletionContext
from codey.research.ledger import OpenedSource
from codey.research.passages import ReadPassage
from codey.types.json_value import JsonObject


@pytest.mark.parametrize("as_mapping", [False, True])
def test_completion_context_keeps_current_document_detached(as_mapping: bool) -> None:
    document: JsonObject = {"target_id": "page", "generation": 2}
    row: object = {"browser_document": document} if as_mapping else SimpleNamespace(browser_document=document)

    context = CompletionContext.from_value(row)
    document["generation"] = 3
    assert context.browser_document == {"target_id": "page", "generation": 2}


@pytest.mark.parametrize("as_mapping", [False, True])
def test_completion_context_keeps_original_passage_resolver(as_mapping: bool) -> None:
    url = "https://example.test/receipt"
    source = OpenedSource(url, url, "Original receipt", "2026-11-25", "test-hash")
    passage = ReadPassage("p1", source, "Original text", (), 0, 13)

    def resolve(reference: str) -> ReadPassage:
        assert reference == passage.ref
        return passage

    row: object = {"resolve_quote": resolve} if as_mapping else SimpleNamespace(resolve_quote=resolve)
    context = CompletionContext.from_value(row)
    assert context.resolve_quote is not None
    assert context.resolve_quote("p1") is passage


@pytest.mark.parametrize("field", ["browser_document", "resolve_quote"])
def test_completion_context_rejects_malformed_receipt_resources(field: str) -> None:
    with pytest.raises(ValueError):
        CompletionContext.from_value({field: "invalid"})


def test_quote_resolver_rejects_an_invalid_passage_when_used() -> None:
    def resolve(_reference: str) -> str:
        return "arbitrary text"

    context = CompletionContext.from_value({"resolve_quote": resolve})
    assert context.resolve_quote is not None
    with pytest.raises(ValueError, match="must return a ReadPassage"):
        context.resolve_quote("p1")
