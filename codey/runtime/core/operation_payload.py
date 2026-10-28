"""Strict validation for opaque admission data and final delivery facts."""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field

from codey.runtime.core.progress import ProgressSnapshot


class RuntimeOperationTransitionError(Exception):
    """An operation state transition violated the closed leaf table."""


@dataclass(frozen=True)
class RuntimeExecutionCounters:
    """Bounded execution history; it carries no independent execution phase."""

    invalid_turns: int = 0
    length_continuation_used: bool = False
    closing_round: int = 0
    progress: ProgressSnapshot = field(default_factory=ProgressSnapshot)

    def to_payload(self) -> dict[str, object]:
        return {"invalid_turns": self.invalid_turns,
                "length_continuation_used": self.length_continuation_used,
                "closing_round": self.closing_round, "progress": self.progress.to_payload()}

    @classmethod
    def from_payload(cls, value: object) -> RuntimeExecutionCounters:
        if (not isinstance(value, dict)
                or set(value) != {"invalid_turns", "length_continuation_used", "closing_round", "progress"}
                or type(value["invalid_turns"]) is not int or value["invalid_turns"] < 0
                or type(value["length_continuation_used"]) is not bool
                or type(value["closing_round"]) is not int or not 0 <= value["closing_round"] <= 4):
            raise RuntimeOperationTransitionError("invalid operation execution counters")
        try:
            progress = ProgressSnapshot.from_payload(value["progress"])
        except ValueError as exc:
            raise RuntimeOperationTransitionError(str(exc)) from exc
        return cls(value["invalid_turns"], value["length_continuation_used"], value["closing_round"], progress)


def _parse_final_delivery(value: object) -> str:
    if not isinstance(value, str) or value not in {"not_required", "success", "failed", "unknown"}:
        raise RuntimeOperationTransitionError("invalid final model result delivery")
    return value


def _parse_model_selection(value: object) -> dict[str, object]:
    if value is None or value == {}:
        return {}
    from codey.runtime.core.api_selection import ApiRunSelection

    try:
        return ApiRunSelection.from_payload(value).to_payload()
    except ValueError as exc:
        raise RuntimeOperationTransitionError("invalid admitted API selection") from exc


def _parse_task_policy(value: object) -> dict[str, object]:
    """Preserve opaque authorization data; TaskPolicy owns its schema.

    Runtime storage must neither drop newly added fields nor turn invalid
    values into valid ones. Semantic validation belongs to the policy owner.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise RuntimeOperationTransitionError("task_policy must be an object")
    try:
        json.dumps(value, allow_nan=False)
        return copy.deepcopy(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeOperationTransitionError("task_policy must be JSON data") from exc
