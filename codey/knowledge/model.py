"""Long-term knowledge identities, independent of report/display budgets."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

from codey.knowledge.note import is_safe_id

KINDS = {"entity", "source", "evidence", "observation", "claim", "inference", "question", "answer"}
ORIGINS = {"source", "observed_result", "author_interpretation", "model_inference", "user_statement", "experiment"}
STATUSES = {"candidate", "active", "stale", "withdrawn", "superseded", "contradicted"}
SCOPES = {"global", "project", "session", "personal"}
CONDITION_STATES = {"reported", "not_reported", "not_extracted", "not_applicable"}


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True)
class KnowledgeReference:
    target: str
    revision: int
    relation: str

    def __post_init__(self) -> None:
        if not is_safe_id(self.target) or type(self.revision) is not int or self.revision < 1:
            raise ValueError("knowledge references require a stable target and positive revision")


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
    provenance: dict[str, Any] = field(default_factory=dict)
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
        for key, condition in self.conditions.items():
            if (not key or not isinstance(condition, dict) or condition.get("state") not in CONDITION_STATES
                    or set(condition) - {"state", "value"}
                    or any(not isinstance(value, str) for value in condition.values())):
                raise ValueError("conditions require state: reported/not_reported/not_extracted/not_applicable")
            if condition["state"] == "reported" and not condition.get("value", "").strip():
                raise ValueError("reported condition needs a value")
        canonical_json(self.to_dict())  # Reject non-JSON values and NaN at the boundary.

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["aliases"] = list(self.aliases)
        data["references"] = [asdict(ref) for ref in self.references]
        return data

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> KnowledgeObject:
        data = dict(raw)
        data["aliases"] = tuple(data.get("aliases", ()))
        data["references"] = tuple(KnowledgeReference(**ref) for ref in data.get("references", ()))
        return cls(**data)


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
    needs_review: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {**self.value.to_dict(), "revision": self.revision, "recorded_at": self.recorded_at,
                "commit_id": self.commit_id, "needs_review": self.needs_review}
