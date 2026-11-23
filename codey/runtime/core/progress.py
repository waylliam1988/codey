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
    warned_check_cycle: str = ""
    warning_batch_id: str = ""
    check_cycle_repeated: bool = False
    recovery_progress_turn: int = 0

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {"limit": self.limit, "seen": [list(key) for key in self.seen],
                "attempts": [asdict(record) for record in self.attempts],
                "idle": self.idle, "last_batch_id": self.last_batch_id}
        if self.schema_version in {2, 3, 4}:
            payload.update(schema_version=self.schema_version, repair_failure_key=self.repair_failure_key,
                           repair_rejections=self.repair_rejections)
        elif self.schema_version != 1 or self.repair_failure_key or self.repair_rejections:
            raise ValueError("repair observations require progress schema v2")
        if self.schema_version >= 3:
            payload.update(warned_check_cycle=self.warned_check_cycle, warning_batch_id=self.warning_batch_id,
                           check_cycle_repeated=self.check_cycle_repeated)
        elif self.warned_check_cycle or self.warning_batch_id or self.check_cycle_repeated:
            raise ValueError("project check observations require progress schema v3")
        if self.schema_version == 4:
            payload["recovery_progress_turn"] = self.recovery_progress_turn
        elif self.recovery_progress_turn:
            raise ValueError("recovery progress requires progress schema v4")
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
        if limit < 1 or idle < 0 or len(batch_id) > 240:
            raise ValueError("invalid progress counts or batch identity")
        failure_key = require_json_string(row.get("repair_failure_key", ""), "repair_failure_key")
        rejections = require_json_integer(row.get("repair_rejections", 0), "repair_rejections")
        if len(failure_key) > 4096 or not 0 <= rejections <= limit or bool(failure_key) != bool(rejections):
            raise ValueError("invalid repair rejection observation")
        check_cycle = require_json_string(row.get("warned_check_cycle", ""), "warned_check_cycle")
        warning_batch = require_json_string(row.get("warning_batch_id", ""), "warning_batch_id")
        repeated = require_json_boolean(row.get("check_cycle_repeated", False), "check_cycle_repeated")
        recovery_turn = require_json_integer(row.get("recovery_progress_turn", 0), "recovery progress turn")
        if (bool(check_cycle) != bool(warning_batch) or len(check_cycle) > 240 or len(warning_batch) > 240
                or recovery_turn < 0 or recovery_turn and not batch_id
                or repeated and (not warning_batch or warning_batch == batch_id)):
            raise ValueError("invalid project check cycle observation")
        raw_seen, raw_attempts = row["seen"], row["attempts"]
        if not isinstance(raw_seen, list) or len(raw_seen) > 256 or not isinstance(raw_attempts, list) or len(raw_attempts) > 16:
            raise ValueError("progress snapshot exceeds its bounded history")
        seen: list[tuple[str, str, str]] = []
        for key in raw_seen:
            parts = require_json_strings(key, "progress information key")
            if len(parts) != 3 or len(parts[2]) != 24 or any(char not in "0123456789abcdef" for char in parts[2]):
                raise ValueError("invalid progress information key")
            seen.append((parts[0], parts[1], parts[2]))
        if len(set(seen)) != len(seen):
            raise ValueError("duplicate progress information key")
        attempts: list[ToolAttemptRecord] = []
        for record in raw_attempts:
            if not isinstance(record, dict) or set(record) != {"tool", "call_fp", "result_fp", "ok", "changed", "turn", "edit_epoch"}:
                raise ValueError("invalid progress attempt")
            turn = require_json_integer(record["turn"], "turn")
            edit_epoch = require_json_integer(record["edit_epoch"], "edit_epoch")
            if turn < 0 or edit_epoch < 0:
                raise ValueError("invalid progress attempt")
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
