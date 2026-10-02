"""Small structured event contract for one agent run."""

from __future__ import annotations

from dataclasses import dataclass, field

from codey.runtime.core.models import ToolCall
from codey.toolchain.runtime import ToolOutcome
from codey.utils.refs import coerce_int

MAX_EVENT_TEXT_CHARS = 1_000
MAX_EVENT_RESULT_CHARS = 200
TRUNCATED_TEXT_SUFFIX = "..."
MAX_DISPLAY_TOOL_CHARS = 160


def _safe_tool_index(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, str) and value.isascii() and value.isdigit():
        try:
            return max(0, int(value))
        except ValueError:
            return 0
    return 0


def _safe_byte_count(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        number = coerce_int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, number)


def _event_metadata(event: RunEvent) -> dict:
    metadata = getattr(event, "metadata", {})
    return metadata if isinstance(metadata, dict) else {}


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


def _call_args(call: ToolCall | None) -> dict:
    args = call.args if call is not None else {}
    return args if isinstance(args, dict) else {}


def render_run_event(event: RunEvent) -> str:
    if event.kind == "turn":
        suffix = f" {event.note}" if event.note else ""
        return f"\n--- turn {event.turn} reply{suffix} ---\n{event.reply}\n"
    if event.kind == "tool_start" and event.call is not None:
        path = str(_call_args(event.call).get("path") or "")
        label = path if path != "." else ""
        return f"  - {event.call.name} {label} -> {event.message}"
    if event.kind == "tool" and event.call is not None and event.outcome is not None:
        path = str(_call_args(event.call).get("path") or "")
        label = path if path != "." else ""
        first_line = event.outcome.presentation_result(80)
        return f"  - {event.call.name} {label} -> {first_line}"
    if event.kind == "info":
        names = str(_event_metadata(event).get("names") or "")
        suffix = f": {names}" if names else ""
        return f"[agent] {event.message}{suffix}"
    return event.message


def display_tool(name: str, args: dict, path: str = "") -> tuple[str, str]:
    research_names = {
        "web_search": ("search", str(args.get("query") or "")),
        "open_url": ("read", str(args.get("url") or "")),
        "knowledge_search": ("recall", str(args.get("query") or "")),
        "knowledge_read": ("note", str(args.get("id") or args.get("note_id") or "")),
        "knowledge_write": ("note", str(args.get("title") or args.get("type") or "")),
        "knowledge_link": ("link", str(args.get("src") or "")),
    }
    if name in research_names:
        kind, label = research_names[name]
        return kind, label[:MAX_DISPLAY_TOOL_CHARS]
    return name, "" if path == "." else path


def run_event_ui_payload(
    run_id: str,
    session_id: str,
    event: RunEvent,
) -> dict | None:
    """Render one RunEvent as the existing UI/SSE payload shape."""

    if event.kind == "turn":
        payload = {
            "type": "turn",
            "run_id": run_id,
            "session_id": session_id,
            "turn": event.turn,
        }
        if event.note:
            payload["note"] = event.note
        return payload
    if event.kind == "info":
        text = event.message
        names = str(_event_metadata(event).get("names") or "")
        if names:
            text = f"{text}: {names}"
        return {"type": "info", "run_id": run_id, "session_id": session_id, "text": text}
    if event.kind == "tool_start" and event.call is not None:
        start_args = _call_args(event.call)
        path = str(start_args.get("path") or "")
        display_kind, display_path = display_tool(event.call.name, start_args, path)
        meta = _event_metadata(event)
        tool_index = _safe_tool_index(meta.get("tool_index"))
        canonical_name = str(meta.get("tool_name") or event.call.name or "").strip().lower()
        payload = {
            "type": "tool_started",
            "run_id": run_id,
            "session_id": session_id,
            "turn": event.turn,
            "tool_id": f"{event.turn}:{tool_index}",
            "kind": display_kind,
            "tool_name": canonical_name,
            "path": display_path,
            "activity": event.message,
        }
        command = str(start_args.get("command") or "")
        if command:
            payload["command"] = command
        return payload
    if event.kind != "tool" or event.call is None or event.outcome is None:
        return None
    tool_args = _call_args(event.call)
    path = str(tool_args.get("path") or "")
    display_kind, display_path = display_tool(event.call.name, tool_args, path)
    result = event.outcome.presentation_result(MAX_EVENT_RESULT_CHARS)
    meta = _event_metadata(event)
    tool_index = _safe_tool_index(meta.get("tool_index"))
    canonical_name = str(meta.get("tool_name") or event.call.name or "").strip().lower()
    status = event.outcome.presentation_status()
    payload = {
        "type": "tool",
        "run_id": run_id,
        "session_id": session_id,
        "turn": event.turn,
        "tool_id": f"{event.turn}:{tool_index}",
        "kind": display_kind,
        "tool_name": canonical_name,
        "path": display_path,
        "result": result,
        "status": status,
        "error": status == "error",
        "ok": event.outcome.ok,
        "changed": event.outcome.changed,
        "truncated": event.outcome.truncated,
    }
    command = str(tool_args.get("command") or "")
    if command:
        payload["command"] = command
    # Strict exit: only real ints are projected; bool/str/float are omitted.
    if event.outcome.exit_code is not None and type(event.outcome.exit_code) is int:
        payload["exit_code"] = event.outcome.exit_code
    managed = event.outcome.managed_output()
    if managed:
        payload["output_handle"] = str(managed.get("handle") or "")
        payload["output_bytes"] = _safe_byte_count(managed.get("original_bytes"))
        payload["output_stored_bytes"] = _safe_byte_count(managed.get("stored_bytes"))
        payload["output_sha256"] = str(managed.get("sha256") or "")
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


def print_run_event(event: RunEvent) -> None:
    print(render_run_event(event))
