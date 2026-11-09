"""Committed execution index: bounded facts, distinct from a full receipt."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, NotRequired, TypedDict


class AppliedEdit(TypedDict):
    mutation_applied: Literal[True]
    identity_status: Literal["unconfirmed"]
    identity_reason: str


class ExecutionRecord(TypedDict):
    name: str
    ok: bool
    call_id: str
    excerpt: str
    args_digest: str
    argument_digests: dict[str, str]
    child_effect_ids: NotRequired[list[str]]
    applied_edit: NotRequired[AppliedEdit]
    exit_code: NotRequired[int]
    execution_disposition: NotRequired[Literal["denied_before_execution"]]
    workspace_revision: NotRequired[int]
    workspace_fingerprint: NotRequired[str]


class HitTarget(TypedDict):
    url: str
    text_hash: str
    offset: int
    pages: str


@dataclass(frozen=True)
class ExecutionFact:
    """Immutable completion projection; contains no raw output authority."""

    identity: str
    name: str
    ok: bool
    call_id: str
    excerpt: str
    args_digest: str
    argument_digests: tuple[tuple[str, str], ...]
    child_effect_ids: tuple[str, ...]

    @classmethod
    def capture(cls, identity: str, row: ExecutionRecord) -> ExecutionFact:
        return cls(identity, row.get("name", ""), row.get("ok") is True,
                   row.get("call_id", ""), row.get("excerpt", ""), row.get("args_digest", ""),
                   tuple(row.get("argument_digests", {}).items()), tuple(row.get("child_effect_ids", ())))

    def to_record(self) -> ExecutionRecord:
        return ExecutionRecord(name=self.name, ok=self.ok, call_id=self.call_id,
                               excerpt=self.excerpt, args_digest=self.args_digest,
                               argument_digests=dict(self.argument_digests),
                               child_effect_ids=list(self.child_effect_ids))
