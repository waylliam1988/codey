from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from codey.knowledge.model import canonical_json
from codey.research.answer_fields import AnswerField
from codey.research.answer_values import AnswerScopePayload
from codey.research.task_mode import ResearchPolicyView
from codey.runtime.core.execution_record import ExecutionRecord
from codey.runtime.core.models import ToolResult
from codey.types.json_value import JsonObject, decode_json_object, require_json_boolean, require_json_string

ANSWER_SCOPE_VERSION = 11


class AnswerSessionView(ResearchPolicyView, Protocol):
    @property
    def task_text(self) -> str: ...

    @property
    def _memory_results(self) -> Mapping[str, ToolResult]: ...

    @property
    def executed(self) -> Mapping[str, ExecutionRecord]: ...


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


@dataclass(frozen=True)
class AnswerScope:
    task_digest: str
    aliases: tuple[tuple[str, str], ...]
    attempt_refs: tuple[str, ...]
    fields: tuple[AnswerField, ...] = ()
    declared_fields_ready: bool = True
    execution_summary: str = ""
    execution_refs: tuple[str, ...] = ()
    calculations: tuple[JsonObject, ...] = ()
    primary_passages: tuple[str, ...] | None = None

    @property
    def schema_version(self) -> int:
        return ANSWER_SCOPE_VERSION

    @property
    def passage_refs(self) -> tuple[str, ...]:
        return tuple(ref for _, ref in self.aliases)

    def to_payload(self) -> AnswerScopePayload:
        return AnswerScopePayload(
            task_digest=self.task_digest,
            aliases=[list(row) for row in self.aliases],
            attempt_refs=list(self.attempt_refs),
            schema_version=self.schema_version,
            fields=[field.to_payload() for field in self.fields],
            declared_fields_ready=self.declared_fields_ready,
            execution_summary=self.execution_summary,
            execution_refs=list(self.execution_refs),
            calculations=list(self.calculations),
            primary_passages=list(self.primary_passages) if self.primary_passages is not None else None,
        )

    @classmethod
    def from_payload(cls, raw: object) -> AnswerScope:
        raw = decode_json_object(raw)
        version = raw.get("schema_version")
        if type(version) is not int or version != ANSWER_SCOPE_VERSION:
            raise ValueError("invalid whole-task research scope")
        if set(raw) != AnswerScopePayload.__required_keys__:
            raise ValueError("invalid whole-task research scope")
        task_digest = require_json_string(raw.get("task_digest"), "task_digest")
        if re.fullmatch(r"[0-9a-f]{64}", task_digest) is None:
            raise ValueError("invalid task digest")
        aliases = raw.get("aliases")
        attempts = raw.get("attempt_refs")
        if not isinstance(aliases, list) or not isinstance(attempts, list):
            raise ValueError("invalid frozen research identities")
        bindings: list[tuple[str, str]] = []
        for row in aliases:
            if not isinstance(row, list) or len(row) != 2:
                raise ValueError("invalid frozen evidence alias")
            label = require_json_string(row[0], "label")
            ref = require_json_string(row[1], "passage_ref")
            if re.fullmatch(r"e[1-9][0-9]*", label) is None or re.fullmatch(r"p[0-9a-f]{12}", ref) is None:
                raise ValueError("invalid frozen evidence alias")
            bindings.append((label, ref))
        attempt_refs = tuple(require_json_string(ref, "attempt_ref") for ref in attempts)
        if (len({row[0] for row in bindings}) != len(bindings)
                or len({row[1] for row in bindings}) != len(bindings)
                or any(not ref for ref in attempt_refs) or len(set(attempt_refs)) != len(attempt_refs)):
            raise ValueError("duplicate or invalid frozen research identity")
        raw_fields = raw["fields"]
        if not isinstance(raw_fields, list):
            raise ValueError("invalid frozen answer fields")
        fields = tuple(AnswerField.from_payload(row) for row in raw_fields)
        if len({field.pointer for field in fields}) != len(fields) or any(set(field.labels) - {label for label, _ in bindings} for field in fields):
            raise ValueError("duplicate or unread frozen answer field")
        execution_refs = raw["execution_refs"]
        if not isinstance(execution_refs, list):
            raise ValueError("invalid frozen browser execution references")
        summary = require_json_string(raw["execution_summary"], "execution_summary")
        refs = tuple(require_json_string(ref, "execution ref") for ref in execution_refs)
        if len(set(refs)) != len(refs) or bool(refs) != bool(summary):
            raise ValueError("invalid frozen browser execution summary")
        calculations = raw["calculations"]
        if not isinstance(calculations, list) or any(not isinstance(row, dict) for row in calculations):
            raise ValueError("invalid frozen calculations")
        primary = _primary_refs(raw["primary_passages"], bindings) if raw["primary_passages"] is not None else None
        return cls(task_digest, tuple(bindings), attempt_refs, fields,
                   require_json_boolean(raw["declared_fields_ready"], "declared_fields_ready"), summary, refs,
                   tuple(decode_json_object(row) for row in calculations), primary)


def _primary_refs(raw: object, bindings: list[tuple[str, str]]) -> tuple[str, ...]:
    if not isinstance(raw, list):
        raise ValueError("primary_passages must identify original task reads")
    primary = tuple(require_json_string(ref, "primary passage") for ref in raw)
    if len(set(primary)) != len(primary) or set(primary) - {ref for _, ref in bindings}:
        raise ValueError("primary_passages must identify original task reads")
    return primary
