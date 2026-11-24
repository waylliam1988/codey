"""Small durable state surface for one CodeMode task."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Protocol

from codey.durable.model import TaskCheckpoint, thaw_json
from codey.types.json_value import unique_json_fields

if TYPE_CHECKING:
    from codey.durable.session import DurableSession


class CodeModeStateStore(Protocol):
    def store(self, key: str, value: object) -> None:
        """Persist one bounded JSON value."""

    def load(self, key: str) -> object:
        """Load one value, returning ``None`` for a missing key."""


class DurableCodeModeState:
    """Persist CodeMode state inside the task checkpoint's owned state."""

    def __init__(
        self,
        durable: DurableSession,
        task_id: str,
        *,
        max_keys: int = 64,
        key_bytes: int = 256,
        value_bytes: int = 16_384,
    ) -> None:
        self.durable = durable
        self.task_id = str(task_id or "")
        self.max_keys = max_keys
        self.key_bytes = key_bytes
        self.value_bytes = value_bytes
        if not self.task_id:
            raise ValueError("CodeMode state requires a task id")
        if any(type(value) is not int or value <= 0 for value in (max_keys, key_bytes, value_bytes)):
            raise ValueError("CodeMode state limits must be positive integers")

    def store(self, key: str, value: object) -> None:
        def update(current: TaskCheckpoint) -> dict[str, object]:
            return {"codemode_state": self.prepare_store(current, key, value)}

        self.durable.transform_task_state(
            self.task_id,
            update,
        )

    def prepare_store(self, current: TaskCheckpoint, key: str, value: object) -> dict[str, object]:
        """The single state mutation; replay adds only atomic revision references."""
        key = self._key(key)
        encoded = self._encode(value)
        state = self.values_at(current)
        if key not in state and len(state) >= self.max_keys:
            raise ValueError("CodeMode state key limit exceeded")
        decoded: object = json.loads(encoded, object_pairs_hook=unique_json_fields)
        state[key] = decoded
        return state

    def load(self, key: str) -> object:
        return self.values_at(self.durable.task(self.task_id)).get(self._key(key))

    @staticmethod
    def values_at(current: TaskCheckpoint) -> dict[str, object]:
        empty: dict[str, object] = {}
        raw = current.state.get("codemode_state", empty)
        if not isinstance(raw, Mapping):
            raise ValueError("invalid CodeMode program state")
        fields: Mapping[object, object] = raw
        return {str(key): thaw_json(item) for key, item in fields.items()}

    def _key(self, key: str) -> str:
        if not isinstance(key, str) or not key:
            raise ValueError("CodeMode state key must be a non-empty string")
        if len(key.encode("utf-8")) > self.key_bytes:
            raise ValueError("CodeMode state key exceeds size limit")
        return key

    def _encode(self, value: object) -> str:
        try:
            encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("CodeMode state value must be finite JSON") from exc
        if len(encoded.encode("utf-8")) > self.value_bytes:
            raise ValueError("CodeMode state value exceeds size limit")
        return encoded


__all__ = ["CodeModeStateStore", "DurableCodeModeState"]
