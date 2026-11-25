"""Long-term knowledge identities, independent of report/display budgets."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field

from codey.knowledge.note import is_safe_id
from codey.types.json_value import require_json_integer, require_json_string

KINDS = {"entity", "source", "evidence", "observation", "claim", "inference", "question", "answer"}
ORIGINS = {"source", "observed_result", "author_interpretation", "model_extraction", "model_inference",
           "user_statement", "experiment"}
STATUSES = {"candidate", "active", "stale", "withdrawn", "superseded", "contradicted"}
SCOPES = {"global", "project", "session", "personal"}
CONDITION_STATES = {"reported", "not_reported", "not_extracted", "not_applicable"}


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _array(value: object, name: str) -> Sequence[object]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{name} must be an array")
    return value


def _object(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    raw: Mapping[object, object] = value
    return {require_json_string(key, f"{name} key"): item for key, item in raw.items()}


@dataclass(frozen=True)
class KnowledgeReference:
    target: str
    revision: int
    relation: str

    def __post_init__(self) -> None:
        if not is_safe_id(self.target) or type(self.revision) is not int or self.revision < 1:
            raise ValueError("knowledge references require a stable target and positive revision")

    def to_dict(self) -> dict[str, str | int]:
        return {"target": self.target, "revision": self.revision, "relation": self.relation}

    @classmethod
    def _decode(cls, value: object) -> KnowledgeReference:
        row = _object(value, "knowledge reference")
        if row.keys() != {"target", "revision", "relation"}:
            raise ValueError("knowledge reference requires target, revision and relation")
        return cls(require_json_string(row["target"], "target"), require_json_integer(row["revision"], "revision"),
                   require_json_string(row["relation"], "relation"))


@dataclass(frozen=True)
class KnowledgeObject:
    id: str
    kind: str
    content: str
    origin: str
    title: str = ""
    status: str = "candidate"
    scope: str = "global"
    scope_id: str = ""
    aliases: tuple[str, ...] = ()
    conditions: dict[str, dict[str, str]] = field(default_factory=dict)
    provenance: Mapping[str, object] = field(default_factory=dict)
    references: tuple[KnowledgeReference, ...] = ()

    def __post_init__(self) -> None:
        if not is_safe_id(self.id) or self.kind not in KINDS or self.origin not in ORIGINS:
            raise ValueError("invalid knowledge identity, kind or origin")
        if not isinstance(self.content, str) or not self.content.strip():
            raise ValueError("knowledge content is required")
        if self.status not in STATUSES or self.scope not in SCOPES:
            raise ValueError("invalid knowledge status or scope")
        if (self.scope == "global") != (self.scope_id == ""):
            raise ValueError("scoped knowledge requires an owner; global knowledge has no scope_id")
        if self.origin == "user_statement" and self.scope == "global":
            raise ValueError("user statements require an explicit restricted scope")
        if self.kind == "inference" and self.origin != "model_inference":
            raise ValueError("inference must retain model_inference origin")
        if any(not isinstance(alias, str) or not alias.strip() for alias in self.aliases):
            raise ValueError("aliases must be nonempty strings")
        example = ('Expected a map from condition names to objects, for example '
                   '{"journal_mode":{"state":"reported","value":"WAL"},'
                   '"workload":{"state":"not_extracted"}}. '
                   'Do not use a flat {"name":...,"state":...,"value":...} object.')
        if not isinstance(self.conditions, dict):
            raise ValueError(f"conditions must be an object. {example}")
        for key, condition in self.conditions.items():
            path = f"conditions.{key}"
            if not isinstance(key, str) or not key or not isinstance(condition, dict):
                raise ValueError(f"{path} must be a condition object. {example}")
            if not isinstance(condition.get("state"), str) or condition["state"] not in CONDITION_STATES:
                raise ValueError(f"{path}.state must be one of {', '.join(sorted(CONDITION_STATES))}. {example}")
            if set(condition) - {"state", "value"}:
                raise ValueError(f"{path} permits only state and value. {example}")
            if any(not isinstance(value, str) for value in condition.values()):
                raise ValueError(f"{path}.state and value must be strings. {example}")
            if condition["state"] == "reported" and not condition.get("value", "").strip():
                raise ValueError(f"{path}: reported condition needs a value")
        canonical_json(self.to_dict())  # Reject non-JSON values and NaN at the boundary.

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "kind": self.kind,
            "content": self.content,
            "origin": self.origin,
            "title": self.title,
            "status": self.status,
            "scope": self.scope,
            "scope_id": self.scope_id,
            "aliases": list(self.aliases),
            "conditions": {name: dict(condition) for name, condition in self.conditions.items()},
            "provenance": deepcopy(dict(self.provenance)),
            "references": [ref.to_dict() for ref in self.references],
        }

    @property
    def interpretation_review(self) -> str:
        # No independent, version-pinned semantic assessment path exists yet.
        # A provenance flag supplied by the model cannot manufacture a review.
        return "unreviewed" if self.kind in {"claim", "inference", "answer"} else "not_applicable"

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> KnowledgeObject:
        data = dict(raw)
        value = cls(
            id=require_json_string(data.pop("id", None), "id"),
            kind=require_json_string(data.pop("kind", None), "kind"),
            content=require_json_string(data.pop("content", None), "content"),
            origin=require_json_string(data.pop("origin", None), "origin"),
            title=require_json_string(data.pop("title", ""), "title"),
            status=require_json_string(data.pop("status", "candidate"), "status"),
            scope=require_json_string(data.pop("scope", "global"), "scope"),
            scope_id=require_json_string(data.pop("scope_id", ""), "scope_id"),
            aliases=tuple(require_json_string(item, "alias") for item in _array(data.pop("aliases", ()), "aliases")),
            conditions={name: {key: require_json_string(item, f"conditions.{name}.{key}")
                               for key, item in _object(condition, f"conditions.{name}").items()}
                        for name, condition in _object(data.pop("conditions", {}), "conditions").items()},
            provenance=_object(data.pop("provenance", {}), "provenance"),
            references=tuple(KnowledgeReference._decode(item)
                             for item in _array(data.pop("references", ()), "references")),
        )
        if data:
            raise ValueError(f"unknown knowledge fields: {', '.join(sorted(data))}")
        return value


@dataclass(frozen=True)
class KnowledgeMutation:
    value: KnowledgeObject
    expected_revision: int = 0

    def __post_init__(self) -> None:
        if type(self.expected_revision) is not int or self.expected_revision < 0:
            raise ValueError("expected_revision must be an integer; use 0 only for a new identity")


@dataclass(frozen=True)
class KnowledgeVersion:
    value: KnowledgeObject
    revision: int
    recorded_at: str
    commit_id: str
    dependencies_changed: bool = False

    @property
    def interpretation_review(self) -> str:
        return self.value.interpretation_review

    def to_dict(self) -> dict[str, object]:
        return {**self.value.to_dict(), "revision": self.revision, "recorded_at": self.recorded_at,
                "commit_id": self.commit_id, "dependencies_changed": self.dependencies_changed,
                "interpretation_review": self.interpretation_review}
