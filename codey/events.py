"""Small structured event contract for one agent run."""

from __future__ import annotations

from dataclasses import dataclass, field

from codey.models import ToolCall
from codey.tool_runtime import ToolOutcome


MAX_EVENT_TEXT_CHARS = 1_000
MAX_EVENT_RESULT_CHARS = 200
TRUNCATED_TEXT_SUFFIX = "..."


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
    def tool_started(
        cls,
        turn: int,
        call: ToolCall,
        activity: str,
        index: int = 0,
    ) -> RunEvent:
        return cls(
            "tool_start",
            turn=turn,
            call=call,
            message=activity,
            metadata={"tool_index": index},
        )

    @classmethod
    def tool_finished(
        cls,
        turn: int,
        call: ToolCall,
        outcome: ToolOutcome,
        index: int = 0,
    ) -> RunEvent:
        return cls(
            "tool",
            turn=turn,
            call=call,
            outcome=outcome,
            metadata={"tool_index": index},
        )

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
    if event.kind == "tool_start" and event.call is not None:
        path = str(event.call.args.get("path") or "")
        label = path if path != "." else ""
        return f"  · {event.call.name} {label} -> {event.message}"
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


def run_event_payload(
    event: RunEvent,
    *,
    run_id: str = "",
    session_id: str = "",
) -> dict | None:
    """Render one run event as a bounded machine-readable payload."""

    common = _event_common(run_id, session_id)
    if event.kind == "turn":
        payload = {
            "type": "turn",
            **common,
            "turn": event.turn,
        }
        if event.note:
            payload["note"] = clip_event_text(event.note)
        return payload
    if event.kind in {"info", "status"}:
        text = event.message
        if event.kind == "info":
            names = str(event.metadata.get("names") or "")
            if names:
                text = f"{text}: {names}"
        return {"type": "info", **common, "text": clip_event_text(text)}
    if event.kind == "tool_start" and event.call is not None:
        path = str(event.call.args.get("path") or "")
        tool_index = int(event.metadata.get("tool_index") or 0)
        payload = {
            "type": "tool_started",
            **common,
            "turn": event.turn,
            "tool_id": f"{event.turn}:{tool_index}",
            "kind": event.call.name,
            "path": "" if path == "." else clip_event_text(path),
            "activity": clip_event_text(event.message),
        }
        command = clip_event_text(event.call.args.get("command") or "")
        if command:
            payload["command"] = command
        return payload
    if event.kind != "tool" or event.call is None or event.outcome is None:
        return None
    path = str(event.call.args.get("path") or "")
    tool_index = int(event.metadata.get("tool_index") or 0)
    payload = {
        "type": "tool",
        **common,
        "turn": event.turn,
        "tool_id": f"{event.turn}:{tool_index}",
        "kind": event.call.name,
        "path": "" if path == "." else clip_event_text(path),
        "ok": event.outcome.ok,
        "changed": event.outcome.changed,
        "truncated": event.outcome.truncated,
        "result": clip_event_text(
            event.outcome.first_line(MAX_EVENT_RESULT_CHARS),
            MAX_EVENT_RESULT_CHARS,
        ),
    }
    command = clip_event_text(event.call.args.get("command") or "")
    if command:
        payload["command"] = command
    if event.outcome.exit_code is not None:
        payload["exit_code"] = event.outcome.exit_code
    return payload


def clip_event_text(
    value: object,
    limit: int = MAX_EVENT_TEXT_CHARS,
) -> str:
    text = str(value or "")
    if limit <= len(TRUNCATED_TEXT_SUFFIX):
        return text[:limit]
    if len(text) <= limit:
        return text
    return text[: limit - len(TRUNCATED_TEXT_SUFFIX)].rstrip() + TRUNCATED_TEXT_SUFFIX


def _event_common(run_id: str, session_id: str) -> dict[str, str]:
    payload: dict[str, str] = {}
    if run_id:
        payload["run_id"] = run_id
    if session_id:
        payload["session_id"] = session_id
    return payload


def print_run_event(event: RunEvent) -> None:
    print(render_run_event(event))
