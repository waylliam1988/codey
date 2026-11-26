from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from codey.operations.kernel.turn_contract import TurnSnapshot
from codey.providers.base import AssistantTurn
from codey.types.protocol_window import ProtocolWindowRef

if TYPE_CHECKING:
    from codey.operations.task_session import TaskSession


@dataclass(frozen=True)
class ReceivedProviderTurn:
    """A received response with its original request's declarations and turn."""

    reply: AssistantTurn | str
    snapshot: TurnSnapshot
    prompt: str
    owner_session: TaskSession
    logical_turn: int = 1
    window_ref: ProtocolWindowRef | None = None
    effect_scope: str = ""
