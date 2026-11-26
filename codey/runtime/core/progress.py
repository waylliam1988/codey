"""Bounded progress data owned by the operation checkpoint."""
from __future__ import annotations

from dataclasses import asdict, dataclass

from codey.types.json_value import (
    decode_json_object,
    require_json_boolean,
    require_json_integer,
    require_json_string,
    require_json_strings,
)

MAX_INFORMATION_KEYS = 256
MAX_PROGRESS_ATTEMPTS = 16


@dataclass(frozen=True)
class ToolAttemptRecord:
    tool: str
    call_fp: str
    result_fp: str
    ok: bool
    changed: bool
    turn: int
    edit_epoch: int = 0

    def __post_init__(self) -> None:
        for value, name in ((self.tool, "tool"), (self.call_fp, "call_fp"), (self.result_fp, "result_fp")):
            require_json_string(value, name)
        require_json_boolean(self.ok, "ok")
        require_json_boolean(self.changed, "changed")
        if (require_json_integer(self.turn, "turn") < 0
                or require_json_integer(self.edit_epoch, "edit_epoch") < 0):
            raise ValueError("invalid progress attempt")


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
    warned_check_cycle: str = ""
    warning_batch_id: str = ""
    check_cycle_repeated: bool = False
    recovery_progress_turn: int = 0

    def __post_init__(self) -> None:
        limit = require_json_integer(self.limit, "progress limit")
        idle = require_json_integer(self.idle, "progress idle")
        batch_id = require_json_string(self.last_batch_id, "last_batch_id")
        version = require_json_integer(self.schema_version, "progress snapshot version")
        if version not in (1, 2, 3, 4):
            raise ValueError("invalid progress snapshot version")
        if limit < 1 or idle < 0 or len(batch_id) > 240:
            raise ValueError("invalid progress counts or batch identity")
        failure_key = require_json_string(self.repair_failure_key, "repair_failure_key")
        rejections = require_json_integer(self.repair_rejections, "repair_rejections")
        if len(failure_key) > 4096 or not 0 <= rejections <= limit or bool(failure_key) != bool(rejections):
            raise ValueError("invalid repair rejection observation")
        check_cycle = require_json_string(self.warned_check_cycle, "warned_check_cycle")
        warning_batch = require_json_string(self.warning_batch_id, "warning_batch_id")
        repeated = require_json_boolean(self.check_cycle_repeated, "check_cycle_repeated")
        recovery_turn = require_json_integer(self.recovery_progress_turn, "recovery progress turn")
        if (bool(check_cycle) != bool(warning_batch) or len(check_cycle) > 240 or len(warning_batch) > 240
                or recovery_turn < 0 or recovery_turn and not batch_id
                or repeated and (not warning_batch or warning_batch == batch_id)):
            raise ValueError("invalid project check cycle observation")
        if version < 2 and (failure_key or rejections):
            raise ValueError("repair observations require progress schema v2")
        if version < 3 and (check_cycle or warning_batch or repeated):
            raise ValueError("project check observations require progress schema v3")
        if version < 4 and recovery_turn:
            raise ValueError("recovery progress requires progress schema v4")
        if (not isinstance(self.seen, tuple) or len(self.seen) > MAX_INFORMATION_KEYS
                or not isinstance(self.attempts, tuple) or len(self.attempts) > MAX_PROGRESS_ATTEMPTS):
            raise ValueError("progress snapshot exceeds its bounded history")
        for key in self.seen:
            if (not isinstance(key, tuple) or len(key) != 3
                    or any(not isinstance(part, str) for part in key)
                    or len(key[2]) != 24 or any(char not in "0123456789abcdef" for char in key[2])):
                raise ValueError("invalid progress information key")
        if len(set(self.seen)) != len(self.seen):
            raise ValueError("duplicate progress information key")
        if any(not isinstance(record, ToolAttemptRecord) for record in self.attempts):
            raise ValueError("invalid progress attempt")

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {"limit": self.limit, "seen": [list(key) for key in self.seen],
                "attempts": [asdict(record) for record in self.attempts],
                "idle": self.idle, "last_batch_id": self.last_batch_id}
        if self.schema_version >= 2:
            payload.update(schema_version=self.schema_version, repair_failure_key=self.repair_failure_key,
                           repair_rejections=self.repair_rejections)
        if self.schema_version >= 3:
            payload.update(warned_check_cycle=self.warned_check_cycle, warning_batch_id=self.warning_batch_id,
                           check_cycle_repeated=self.check_cycle_repeated)
        if self.schema_version == 4:
            payload["recovery_progress_turn"] = self.recovery_progress_turn
        return payload

    @classmethod
    def from_payload(cls, value: object) -> ProgressSnapshot:
        row = decode_json_object(value)
        version = require_json_integer(row.get("schema_version", 1), "progress snapshot version")
        fields = {"limit", "seen", "attempts", "idle", "last_batch_id"}
        if version not in (1, 2, 3, 4):
            raise ValueError("invalid progress snapshot version")
        if version >= 2:
            fields.update(("schema_version", "repair_failure_key", "repair_rejections"))
        if version >= 3:
            fields.update(("warned_check_cycle", "warning_batch_id", "check_cycle_repeated"))
        if version == 4:
            fields.add("recovery_progress_turn")
        if set(row) != fields:
            raise ValueError("invalid progress snapshot shape")
        limit = require_json_integer(row["limit"], "progress limit")
        idle = require_json_integer(row["idle"], "progress idle")
        batch_id = require_json_string(row["last_batch_id"], "last_batch_id")
        failure_key = require_json_string(row.get("repair_failure_key", ""), "repair_failure_key")
        rejections = require_json_integer(row.get("repair_rejections", 0), "repair_rejections")
        check_cycle = require_json_string(row.get("warned_check_cycle", ""), "warned_check_cycle")
        warning_batch = require_json_string(row.get("warning_batch_id", ""), "warning_batch_id")
        repeated = require_json_boolean(row.get("check_cycle_repeated", False), "check_cycle_repeated")
        recovery_turn = require_json_integer(row.get("recovery_progress_turn", 0), "recovery progress turn")
        raw_seen, raw_attempts = row["seen"], row["attempts"]
        if (not isinstance(raw_seen, list) or len(raw_seen) > MAX_INFORMATION_KEYS
                or not isinstance(raw_attempts, list) or len(raw_attempts) > MAX_PROGRESS_ATTEMPTS):
            raise ValueError("progress snapshot exceeds its bounded history")
        seen: list[tuple[str, str, str]] = []
        for key in raw_seen:
            parts = require_json_strings(key, "progress information key")
            if len(parts) != 3:
                raise ValueError("invalid progress information key")
            seen.append((parts[0], parts[1], parts[2]))
        attempts: list[ToolAttemptRecord] = []
        for record in raw_attempts:
            if not isinstance(record, dict) or set(record) != {"tool", "call_fp", "result_fp", "ok", "changed", "turn", "edit_epoch"}:
                raise ValueError("invalid progress attempt")
            turn = require_json_integer(record["turn"], "turn")
            edit_epoch = require_json_integer(record["edit_epoch"], "edit_epoch")
            attempts.append(ToolAttemptRecord(
                require_json_string(record["tool"], "tool"),
                require_json_string(record["call_fp"], "call_fp"),
                require_json_string(record["result_fp"], "result_fp"),
                require_json_boolean(record["ok"], "ok"),
                require_json_boolean(record["changed"], "changed"),
                turn, edit_epoch,
            ))
        return cls(limit, tuple(seen), tuple(attempts), idle, batch_id, version, failure_key, rejections,
                   check_cycle, warning_batch, repeated, recovery_turn)
