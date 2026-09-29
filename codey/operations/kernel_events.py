"""RunEvent projection for the shared task kernel (no task state).

Turn starts and tool results are projected to the existing RunEvent stream.
These helpers format and emit only; they never decide, send, or execute.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from codey.operations.task_session import TaskSession, turn_effect_id
from codey.runtime.core.models import ToolCall, ToolResult

__all__: list[str] = []


def _emit_turn_event(on_event: Callable[[Any], None] | None, turn: int, reply: Any) -> None:
    if on_event is None:
        return
    from codey.runtime.observe.events import RunEvent

    display = reply if isinstance(reply, str) else str(getattr(reply, "text", "") or "")
    on_event(RunEvent.turn_started(turn, str(display)))


def _event_call(session: TaskSession, call: ToolCall) -> ToolCall:
    name = str(call.name or "")
    project_name = {"list_dir": "ls", "read_file": "read", "grep": "search", "find_references": "references"}.get(name)
    if project_name is not None:
        return ToolCall(name=project_name, args=dict(call.args), call_id=call.call_id)
    key = {"open_result": "result_id", "reopen_source": "source_id", "open_hit": "hit_id"}.get(name)
    if key is None:
        return call
    reference = str(call.args.get(key) or "").strip().lower()
    url = (
        (session.hit_targets.get(reference, {}).get("url") if name == "open_hit" else None)
        or session.search_results.get(reference)
        or session.source_ids.get(reference)
    )
    if not url:
        return call
    return ToolCall(name="open_url", args={"url": url}, call_id=call.call_id)


def _emit_tool_starts(
    on_event: Callable[[Any], None] | None, session: TaskSession, turn: int, calls: list[ToolCall]
) -> None:
    if on_event is None:
        return
    from codey.runtime.observe.events import RunEvent
    from codey.toolchain.definition import render_tool_activity

    for index, call in enumerate(calls):
        display_call = _event_call(session, call)
        on_event(RunEvent.tool_started(turn, display_call, render_tool_activity(display_call), index))


def _emit_tool_results(
    on_event: Callable[[Any], None] | None,
    session: TaskSession,
    results: list[ToolResult],
    *,
    run_id: object,
    turn: int,
) -> None:
    if on_event is None:
        return
    from codey.runtime.observe.events import RunEvent
    from codey.toolchain.runtime import ToolOutcome

    for index, result in enumerate(results):
        identity = turn_effect_id(str(run_id or "adhoc"), turn, index)
        record = session.executed.get(identity, {})
        ok = bool(record.get("ok", False))
        exit_code = None
        if isinstance(result.audit, dict):
            exit_code = result.audit.get("exit_code")
        if result.call.name == "run" and session.verifications:
            exit_code = exit_code if exit_code is not None else session.verifications[-1].get("exit_code")
        display = str(result.model_text or "")
        if result.call.name in {"open_url", "open_result", "open_hit", "reopen_source"}:
            display = next(
                (line.removeprefix("Title: ") for line in display.splitlines() if line.startswith("Title: ")),
                display.splitlines()[0] if display else "",
            )
        outcome = ToolOutcome(
            model_text=result.model_text,
            ok=ok,
            changed=bool(result.audit.get("changed", ok and result.call.name == "edit")),
            exit_code=exit_code,
            truncated=result.truncated,
            canonical=result.canonical,
            audit=result.audit,
            presentation={"result": display[:500], **dict(result.presentation)},
        )
        on_event(RunEvent.tool_finished(turn, _event_call(session, result.call), outcome, index))
