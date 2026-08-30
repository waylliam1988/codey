"""Runtime lane queues for current work, steering, follow-ups, and next tasks."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Literal

LaneName = Literal["current", "steer", "follow_up", "next"]


@dataclass(frozen=True)
class LaneCommand:
    lane: LaneName | str
    operation_id: str
    command: Literal["schedule", "resume", "cancel", "abort"] = "schedule"


@dataclass
class LaneQueues:
    _queues: dict[str, deque[LaneCommand]] = field(default_factory=dict)

    def push(self, command: LaneCommand) -> None:
        self._queues.setdefault(command.lane, deque()).append(command)

    def pop(self, lane: LaneName | str = "current") -> LaneCommand | None:
        queue = self._queues.get(lane)
        if not queue:
            return None
        return queue.popleft()

    def pending(self, lane: LaneName | str = "current") -> tuple[LaneCommand, ...]:
        return tuple(self._queues.get(lane, ()))
