"""Decode tool JSON into domain values before performing knowledge effects."""

from dataclasses import dataclass

from codey.knowledge.model import KnowledgeMutation, KnowledgeObject
from codey.types.json_value import decode_json_object, require_json_integer, require_json_string


@dataclass(frozen=True)
class KnowledgeCommitRequest:
    commit_id: str
    mutations: tuple[KnowledgeMutation, ...]

    @classmethod
    def decode(cls, value: object) -> "KnowledgeCommitRequest":
        args = decode_json_object(value)
        identity = require_json_string(args.get("commit_id"), "commit_id").strip()
        if not identity:
            raise ValueError("commit_id required")
        rows = args.get("changes")
        if not isinstance(rows, list) or not rows:
            raise ValueError("changes must be a nonempty list of {value, expected_revision}")
        mutations: list[KnowledgeMutation] = []
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"value", "expected_revision"}:
                raise ValueError("each change needs value and expected_revision")
            mutations.append(KnowledgeMutation(KnowledgeObject.from_dict(decode_json_object(row["value"])),
                require_json_integer(row["expected_revision"], "expected_revision")))
        return cls(identity, tuple(mutations))


def decode_conditions(value: object) -> dict[str, dict[str, str]] | None:
    if value is None:
        return None
    row = decode_json_object(value)
    result: dict[str, dict[str, str]] = {}
    for name, condition in row.items():
        if not isinstance(condition, dict):
            raise ValueError("condition must be an object")
        result[name] = {key: require_json_string(item, key) for key, item in condition.items()}
    KnowledgeObject("condition-validation", "claim", "validation", "model_extraction", conditions=result)
    return result
