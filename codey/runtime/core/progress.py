"""Bounded progress data owned by the operation checkpoint."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import cast


@dataclass(frozen=True)
class ToolAttemptRecord:
    tool: str
    call_fp: str
    result_fp: str
    ok: bool
    changed: bool
    turn: int
    edit_epoch: int = 0


@dataclass(frozen=True)
class ProgressSnapshot:
    limit: int = 4
    seen: tuple[tuple[str, str, str], ...] = ()
    attempts: tuple[ToolAttemptRecord, ...] = ()
    idle: int = 0
    last_batch_id: str = ""
    schema_version: int = 1
    repair_failure_key: str = ""
    repair_rejections: int = 0

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {"limit": self.limit, "seen": [list(key) for key in self.seen],
                "attempts": [asdict(record) for record in self.attempts],
                "idle": self.idle, "last_batch_id": self.last_batch_id}
        if self.schema_version == 2:
            payload.update(schema_version=2, repair_failure_key=self.repair_failure_key,
                           repair_rejections=self.repair_rejections)
        elif self.schema_version != 1 or self.repair_failure_key or self.repair_rejections:
            raise ValueError("repair observations require progress schema v2")
        return payload

    @classmethod
    def from_payload(cls, value: object) -> ProgressSnapshot:
        if not isinstance(value, dict):
            raise ValueError("invalid progress snapshot shape")
        version = value.get("schema_version", 1)
        fields = {"limit", "seen", "attempts", "idle", "last_batch_id"}
        if type(version) is not int or version not in (1, 2):
            raise ValueError("invalid progress snapshot version")
        if version == 2:
            fields.update(("schema_version", "repair_failure_key", "repair_rejections"))
        if set(value) != fields:
            raise ValueError("invalid progress snapshot shape")
        if (type(value["limit"]) is not int or value["limit"] < 1
                or type(value["idle"]) is not int or value["idle"] < 0
                or not isinstance(value["last_batch_id"], str) or len(value["last_batch_id"]) > 240):
            raise ValueError("invalid progress counts or batch identity")
        failure_key = value.get("repair_failure_key", "")
        rejections = value.get("repair_rejections", 0)
        if (not isinstance(failure_key, str) or len(failure_key) > 4096
                or type(rejections) is not int or not 0 <= rejections <= value["limit"]
                or bool(failure_key) != bool(rejections)):
            raise ValueError("invalid repair rejection observation")
        raw_seen, raw_attempts = value["seen"], value["attempts"]
        if not isinstance(raw_seen, list) or len(raw_seen) > 256 or not isinstance(raw_attempts, list) or len(raw_attempts) > 16:
            raise ValueError("progress snapshot exceeds its bounded history")
        seen: list[tuple[str, str, str]] = []
        for key in raw_seen:
            if (not isinstance(key, list) or len(key) != 3
                    or any(not isinstance(part, str) for part in key)
                    or len(key[2]) != 24 or any(char not in "0123456789abcdef" for char in key[2])):
                raise ValueError("invalid progress information key")
            seen.append(cast(tuple[str, str, str], tuple(key)))
        if len(set(seen)) != len(seen):
            raise ValueError("duplicate progress information key")
        attempts: list[ToolAttemptRecord] = []
        for record in raw_attempts:
            if (not isinstance(record, dict)
                    or set(record) != {"tool", "call_fp", "result_fp", "ok", "changed", "turn", "edit_epoch"}
                    or any(not isinstance(record[key], str) for key in ("tool", "call_fp", "result_fp"))
                    or any(type(record[key]) is not bool for key in ("ok", "changed"))
                    or any(type(record[key]) is not int or record[key] < 0 for key in ("turn", "edit_epoch"))):
                raise ValueError("invalid progress attempt")
            attempts.append(ToolAttemptRecord(**record))
        return cls(value["limit"], tuple(seen), tuple(attempts), value["idle"], value["last_batch_id"],
                   version, failure_key, rejections)
