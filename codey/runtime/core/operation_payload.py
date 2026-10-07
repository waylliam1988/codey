"""Strict validation for opaque admission data and final delivery facts."""
from __future__ import annotations

import copy
import json


class RuntimeOperationTransitionError(Exception):
    """An operation state transition violated the closed leaf table."""


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
