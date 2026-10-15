"""Durable approval facts and atomic admission of shell execution."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from codey.storage.atomic_io import write_bytes_atomic
from codey.storage.file_lock import with_file_lock

FACT_KINDS = frozenset({"requested", "approved", "denied", "execution_claimed", "execution_settled", "abandoned", "continuation_claimed", "continuation_started", "continuation_failed", "continuation_cancelled"})


def json_digest(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class ShellApprovalFact:
    approval_id: str
    session_id: str
    source_run_id: str
    kind: str
    invocation_origin: str = ""
    command_ref: str = ""
    command_digest: str = ""
    cwd: str = ""
    result_ref: str = ""
    result_digest: str = ""
    delivery_batch_id: str = ""
    continuation_run_id: str = ""

    def validate(self) -> None:
        if not self.approval_id or not self.session_id or not self.source_run_id:
            raise ValueError("approval facts require approval, session, and source run ids")
        if self.kind not in FACT_KINDS:
            raise ValueError(f"unsupported approval fact kind: {self.kind!r}")

    def to_payload(self) -> dict[str, Any]:
        self.validate()
        return {"schema_version": 1, "effect_kind": "shell_approval", **self.__dict__}

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> ShellApprovalFact:
        expected = {"schema_version", "effect_kind", *cls.__dataclass_fields__}
        if set(payload) != expected or payload.get("schema_version") != 1 or payload.get("effect_kind") != "shell_approval":
            raise ValueError("invalid shell approval fact envelope")
        value = cls(**{name: payload[name] for name in cls.__dataclass_fields__})
        value.validate()
        return value


class ShellApprovalRecordStore:
    def __init__(self, root: str | Path) -> None:
        self.path = Path(root) / "runtime" / "shell_approvals.jsonl"
        self._complete_continuation_identity()

    def _complete_continuation_identity(self) -> None:
        """Complete the one known pre-continuation shape, without changing version 1.

        Validate the entire ledger before writing anything. Existing execution
        claims and settlements retain their identities and ordering verbatim.
        """
        if not self.path.exists():
            return
        with with_file_lock(self.path):
            original = self.path.read_bytes()
            payloads: list[dict[str, Any]] = []
            changed = False
            fields = {"schema_version", "effect_kind", *ShellApprovalFact.__dataclass_fields__}
            previous_fields = fields - {"continuation_run_id"}
            previous_kinds = {"requested", "approved", "denied", "execution_claimed", "execution_settled", "abandoned"}
            for number, line in enumerate(original.splitlines(), 1):
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                    if isinstance(payload, dict) and set(payload) == previous_fields and payload.get("kind") in previous_kinds:
                        payload["continuation_run_id"] = ""
                        changed = True
                    ShellApprovalFact.from_payload(payload)
                except (TypeError, ValueError) as error:
                    raise ValueError(f"invalid shell approval record at {self.path}:{number}: {error}") from error
                payloads.append(payload)
            if not changed:
                return
            digest = hashlib.sha256(original).hexdigest()
            backup = self.path.with_name(f"shell_approvals.before-continuation-{digest}.jsonl")
            if not backup.exists():
                write_bytes_atomic(backup, original)
            elif backup.read_bytes() != original:
                raise ValueError(f"shell approval backup differs from original: {backup}")
            completed = "".join(
                json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
                for payload in payloads
            ).encode("utf-8")
            write_bytes_atomic(self.path, completed)

    def append(self, fact: ShellApprovalFact) -> None:
        payload = fact.to_payload()
        line = (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with with_file_lock(self.path):
            existing = self.path.read_bytes() if self.path.exists() else b""
            write_bytes_atomic(self.path, existing + line)

    def claim_execution(self, fact: ShellApprovalFact) -> bool:
        """Atomically reserve one requested or accepted command for execution."""
        if fact.kind != "execution_claimed":
            raise ValueError("execution claim must use execution_claimed kind")
        return self._claim_after(fact, {"requested", "approved"})

    def claim_decision(self, fact: ShellApprovalFact) -> bool:
        """Atomically reserve the verdict for an untouched request."""
        if fact.kind not in {"approved", "denied", "abandoned"}:
            raise ValueError("approval decision must be approved, denied, or abandoned")
        return self._claim_after(fact, {"requested"})

    def _claim_after(self, fact: ShellApprovalFact, expected_kinds: set[str]) -> bool:
        payload = fact.to_payload()
        line = (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with with_file_lock(self.path):
            existing = self.path.read_bytes() if self.path.exists() else b""
            latest: ShellApprovalFact | None = None
            execution_claimed = False
            for raw_line in existing.splitlines():
                if raw_line.strip():
                    candidate = ShellApprovalFact.from_payload(json.loads(raw_line))
                    if candidate.approval_id == fact.approval_id:
                        latest = candidate
                        execution_claimed |= candidate.kind == "execution_claimed"
            if fact.kind == "execution_claimed" and execution_claimed:
                return False
            if latest is None or latest.kind not in expected_kinds:
                return False
            write_bytes_atomic(self.path, existing + line)
            return True

    def read(self, approval_id: str) -> tuple[ShellApprovalFact, ...]:
        if not self.path.exists():
            return ()
        with with_file_lock(self.path):
            rows = []
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    fact = ShellApprovalFact.from_payload(json.loads(line))
                    if fact.approval_id == approval_id:
                        rows.append(fact)
            return tuple(rows)

    def latest(self, approval_id: str) -> ShellApprovalFact | None:
        facts = self.read(approval_id)
        return facts[-1] if facts else None

    def histories(self) -> dict[str, tuple[ShellApprovalFact, ...]]:
        if not self.path.exists():
            return {}
        with with_file_lock(self.path):
            rows: dict[str, list[ShellApprovalFact]] = {}
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    fact = ShellApprovalFact.from_payload(json.loads(line))
                    rows.setdefault(fact.approval_id, []).append(fact)
        return {key: tuple(value) for key, value in rows.items()}

    def pending_requested(self) -> tuple[ShellApprovalFact, ...]:
        """Untouched requests for rebuilding the pending UI projection."""
        return tuple(facts[-1] for facts in self.histories().values() if facts[-1].kind == "requested")


__all__ = ["FACT_KINDS", "ShellApprovalFact", "ShellApprovalRecordStore", "json_digest"]
