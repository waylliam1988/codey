"""Test adapters for the typed kernel recovery APIs."""

from __future__ import annotations

from typing import Any


def trusted_workspace_pair(result: Any) -> tuple[int, str]:
    from codey.operations.kernel_provenance import _kernel_workspace_identity_of

    identity = _kernel_workspace_identity_of(result)
    if identity is None:
        return 0, ""
    return int(identity.revision), str(identity.fingerprint)


def replay_settled_result(*args: Any, **kwargs: Any) -> Any:
    from codey.operations.kernel_recovery import replay_slot_typed

    slot = replay_slot_typed(*args, **kwargs)
    return None if slot.disposition == "NO_MATCH" else slot.result


def delivered_slot_result(*args: Any, **kwargs: Any) -> Any:
    from codey.operations.kernel_recovery import delivered_slot_typed

    slot = delivered_slot_typed(*args, **kwargs)
    return None if slot.disposition == "NO_MATCH" else slot.result
