"""Reference syntax remains identical within the local grammar compiler subset."""

import pytest
from jsonschema import Draft202012Validator

from codey.research.knowledge_updates import normalize_updates, updates_schema


@pytest.mark.parametrize("reference,accepted", [
    ("e1", True), ("[e1]", True), ("writer_limit", True), ("[writer_limit]", True),
    ("A" + "b" * 63, True), ("[" + "A" + "b" * 63 + "]", True),
    ("", False), ("1key", False), ("[e1", False), ("e1]", False),
    ("[[e1]]", False), ("e1 e2", False), ("a/b", False), ("a" * 65, False),
])
@pytest.mark.parametrize("field", ["references", "limited_by", "refuted_by"])
def test_reference_wire_schema_preserves_bounds_and_bracket_forms(reference, accepted, field):
    schema = updates_schema()["items"]["properties"][field]["items"]
    # This is a provider interoperability boundary: the real 1.117.1 compiler
    # rejects (?...) regex syntax before generating any grammar.
    assert "(?" not in schema["pattern"]
    assert Draft202012Validator(schema).is_valid(reference) == accepted


@pytest.mark.parametrize("reference", ["e1", "[e1]"])
def test_grouping_change_preserves_normalized_evidence_identity(reference):
    result = normalize_updates([{"key": "k1", "kind": "claim", "content": "The original evidence supports this claim.",
                                 "references": [reference]}], {"e1"})
    assert result[0]["references"] == [{"ref": "e1", "relation": "supported_by"}]
