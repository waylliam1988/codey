"""Knowledge mutation decoding preserves the shapes declared to tool callers."""

import pytest

from codey.research.tool_args import KnowledgeCommitRequest
from codey.types.json_value import JsonObject


def _commit_with_aliases(aliases: object) -> KnowledgeCommitRequest:
    return KnowledgeCommitRequest.decode({
        "commit_id": "commit",
        "changes": [{"expected_revision": 0, "value": {
            "id": "engine", "kind": "entity", "content": "A database engine", "origin": "source",
            "aliases": aliases,
        }}],
    })


@pytest.mark.parametrize("aliases", ["SQLite", "", {"SQLite": "Database"}])
def test_knowledge_commit_requires_an_alias_array(aliases: str | JsonObject) -> None:
    with pytest.raises(ValueError, match="aliases.*array"):
        _commit_with_aliases(aliases)


def test_knowledge_commit_retains_complete_aliases() -> None:
    request = _commit_with_aliases(["SQLite", "database engine"])
    assert request.mutations[0].value.aliases == ("SQLite", "database engine")
