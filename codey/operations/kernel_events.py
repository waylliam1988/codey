"""RunEvent projection for the shared task kernel (no task state).

Turn starts and tool results are projected to the existing RunEvent stream.
These helpers format and emit only; they never decide, send, or execute.
"""

from __future__ import annotations

import contextlib
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
        canonical = str(getattr(call, "name", "") or "").strip().lower()
        display_call = _event_call(session, call)
        event = RunEvent.tool_started(turn, display_call, render_tool_activity(display_call), index)
        try:
            if isinstance(getattr(event, "metadata", None), dict) and canonical:
                event.metadata["tool_name"] = canonical
        except Exception:
            pass
        on_event(event)


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
        # Strict exit projection: only real ints pass. A present-but-invalid
        # audit exit (bool/str/float) is omitted, never coerced to 0. The
        # verification fallback is strict as well.
        exit_code = None
        try:
            from codey.utils.refs import strict_exit_code as _strict_exit
        except Exception:
            _strict_exit = None  # type: ignore[assignment]
        if isinstance(result.audit, dict) and result.audit.get("exit_code") is not None:
            try:
                raw_exit = result.audit.get("exit_code")
                exit_code = _strict_exit(raw_exit) if _strict_exit is not None else None
            except Exception:
                exit_code = None
        if result.call.name == "run" and session.verifications:
            try:
                fallback_raw = session.verifications[-1].get("exit_code")
                fallback = _strict_exit(fallback_raw) if _strict_exit is not None else None
            except Exception:
                fallback = None
            exit_code = exit_code if exit_code is not None else fallback
        display = str(result.model_text or "")
        if result.call.name in {"open_url", "open_result", "open_hit", "reopen_source"}:
            display = next(
                (line.removeprefix("Title: ") for line in display.splitlines() if line.startswith("Title: ")),
                display.splitlines()[0] if display else "",
            )
        changed = bool(result.audit.get("changed", ok and result.call.name == "edit"))
        outcome = ToolOutcome(
            model_text=result.model_text,
            ok=ok,
            changed=changed,
            exit_code=exit_code,
            truncated=result.truncated,
            canonical=result.canonical,
            audit=result.audit,
            presentation={"result": display[:500], **dict(result.presentation)},
        )
        canonical_name = str(getattr(result.call, "name", "") or "").strip().lower()
        display_call = _event_call(session, result.call)
        event = RunEvent.tool_finished(turn, display_call, outcome, index)
        try:
            if isinstance(getattr(event, "metadata", None), dict):
                if canonical_name:
                    event.metadata["tool_name"] = canonical_name
                # Only the kernel side-channel attached by
                # _with_trusted_workspace_state may be carried. Raw audit
                # workspace keys are executor-forgeable and never trusted;
                # the no-store path never sets the side-channel so it never
                # emits trusted state and hooks must bump. Metadata keys are
                # display/logging only; the authoritative proof travels in
                # the event side-channel read by hooks via ``event_proof``.
                if canonical_name == "edit" and ok and changed:
                    try:
                        from codey.operations.kernel_provenance import (
                            TrustedWorkspaceProof,
                            _kernel_workspace_identity_of,
                            attach_proof_to_event,
                        )

                        kernel_identity = _kernel_workspace_identity_of(result)
                        trusted = kernel_identity is not None
                    except Exception:
                        kernel_identity = None
                        trusted = False
                    if trusted:
                        with contextlib.suppress(Exception):
                            event.metadata["workspace_revision"] = int(kernel_identity.revision)
                            event.metadata["workspace_fingerprint"] = str(kernel_identity.fingerprint)
                        with contextlib.suppress(Exception):
                            attach_proof_to_event(
                                event,
                                TrustedWorkspaceProof(
                                    identity=kernel_identity, source="event_side_channel"
                                ),
                            )
        except Exception:
            pass
        on_event(event)
