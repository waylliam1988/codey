"""Small structured event contract for one agent run."""

from __future__ import annotations

from dataclasses import dataclass, field

from codey.models import ToolCall
from codey.tool_runtime import ToolOutcome


@dataclass(frozen=True)
class RunEvent:
    kind: str
    turn: int = 0
    call: ToolCall | None = None
    outcome: ToolOutcome | None = None
    message: str = ""
    reply: str = ""
    note: str = ""
    metadata: dict[str, object] = field(default_factory=dict)

    @classmethod
    def turn_started(cls, turn: int, reply: str, note: str = "") -> RunEvent:
        return cls("turn", turn=turn, reply=reply, note=note)

    @classmethod
    def tool_finished(
        cls,
        turn: int,
        call: ToolCall,
        outcome: ToolOutcome,
    ) -> RunEvent:
        return cls("tool", turn=turn, call=call, outcome=outcome)

    @classmethod
    def info(cls, message: str, **metadata: object) -> RunEvent:
        return cls("info", message=message, metadata=metadata)

    @classmethod
    def status(cls, message: str) -> RunEvent:
        return cls("status", message=message)


def render_run_event(event: RunEvent) -> str:
    if event.kind == "turn":
        suffix = f" {event.note}" if event.note else ""
        return f"\n--- turn {event.turn} reply{suffix} ---\n{event.reply}\n"
    if event.kind == "tool" and event.call is not None and event.outcome is not None:
        path = str(event.call.args.get("path") or "")
        label = path if path != "." else ""
        first_line = event.outcome.first_line(80)
        return f"  · {event.call.name} {label} -> {first_line}"
    if event.kind == "info":
        names = str(event.metadata.get("names") or "")
        suffix = f": {names}" if names else ""
        return f"[agent] {event.message}{suffix}"
    return event.message


def print_run_event(event: RunEvent) -> None:
    print(render_run_event(event))
