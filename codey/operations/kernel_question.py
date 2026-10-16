"""Suspend a turn for a user decision after closing its protocol call."""
from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any

from codey.providers.base import ProviderToolResult
from codey.runtime.observe.events import RunEvent
from codey.task.questions import question_args


def pause_for_question(args: dict[str, Any], provider: Any, reply: Any, native: bool, native_tools: Any,
                       *, on_event: Callable[[RunEvent], None] | None, identity: str, turn: int) -> str:
    question = question_args(args)
    if on_event is None:
        raise ValueError("user questions require an event surface")
    if native:
        followup = provider.acknowledge_tool_results(
            [ProviderToolResult(call.id, "Question recorded; waiting for the user. No execution authorized.")
             for call in reply.tool_calls], native_tools,
        )
        if followup is not None and followup.tool_calls:
            from codey.operations.kernel_transport import close_native_reply
            close_native_reply(provider, followup, "waiting for the user's answer; call not executed", declared_tools=native_tools)
    identity = "question_" + hashlib.sha256(f"{identity}:{turn}".encode()).hexdigest()[:24]
    on_event(RunEvent("user_question", turn=turn, message=question["question"], metadata={"id": identity, **question}))
    return str(question["question"])
