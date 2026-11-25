"""Test adapter for callers that still provide raw text fixtures.

Production callers must decode text before entering the kernel.  Keeping this
adapter in the test support package lets legacy fixture tests exercise the
same decoder without reintroducing a raw-string kernel entry point.
"""

from __future__ import annotations

from collections.abc import Collection

from codey.operations.kernel.kernel_protocol import TurnSnapshot
from codey.operations.kernel.kernel_protocol import normalize_turn as _normalize_turn
from codey.policies.task_policy import TaskPolicy
from codey.protocols.text_turn import DecodedTextTurn, decode_text_turn
from codey.providers.base import AssistantTurn
from codey.runtime.core.models import ToolPlan


def normalize_turn(
    reply: str | DecodedTextTurn | AssistantTurn,
    *,
    policy: TaskPolicy | None = None,
    controller_allowed: Collection[str] | None = None,
    snapshot_names: tuple[str, ...] | None = None,
    snapshot: TurnSnapshot | None = None,
) -> ToolPlan:
    decoded = decode_text_turn(reply) if isinstance(reply, str) else reply
    return _normalize_turn(decoded, policy=policy, controller_allowed=controller_allowed,
                           snapshot_names=snapshot_names, snapshot=snapshot)
