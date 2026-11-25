"""Deliver owned command completions through the existing frozen provider requests."""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace

from codey.durable.model import TASK_TERMINAL_STATUSES
from codey.durable.session import DurableSession
from codey.durable.tool_task import ToolTaskInput
from codey.operations.provider_receipts import (
    REQUEST_KIND,
    RESPONSE_KIND,
    read_provider_artifact,
    request_artifact_for_effect,
)
from codey.operations.provider_request_context import ProviderRequestContext
from codey.operations.tool_tasks.reads import terminal_tool_task_observations, tool_task_snapshot
from codey.providers.base import ProviderToolResult
from codey.providers.response_record import decode_reply
from codey.runtime.core.models import ToolResult
from codey.runtime.effects.effect_records import RuntimeEffectStore
from codey.storage.managed_outputs import ManagedOutputStore

NOTIFICATION_MAX_CHARS = 12_000


def active_tool_task_controls(durable: DurableSession, owner_task_id: str) -> frozenset[str]:
    names: set[str] = set()
    for child in durable.owned_tasks(owner_task_id):
        if child.execution_kind != "tool" or child.status in TASK_TERMINAL_STATUSES:
            continue
        names.add("task_output")
        if child.state.get("cancel_requested"):
            continue
        names.add("task_cancel")
        request = ToolTaskInput.from_payload(child.state["tool_input"])
        if request.call_name == "run":
            tty = request.args.get("tty") is True
            if request.args.get("stdin", tty) is True:
                names.add("task_input")
            if tty:
                names.update(("task_signal", "task_resize"))
    return frozenset(names)


@dataclass(frozen=True)
class ToolTaskNotifications:
    refs: tuple[str, ...] = ()
    text: str = ""


def append_tool_task_notifications(durable: DurableSession, *, owner_task_id: str, store: ManagedOutputStore,
                                   results: Mapping[str, ToolResult], enabled: bool, native: bool,
                                   prompt: str, messages: list[ProviderToolResult] | None,
                                   ) -> tuple[str, list[ProviderToolResult] | None, tuple[str, ...]]:
    notifications = pending_tool_task_notifications(durable, owner_task_id=owner_task_id, store=store,
        results=results) if enabled else ToolTaskNotifications()
    if notifications.text:
        text = "Automatic command results from committed receipts:\n" + notifications.text
        if native and messages:
            messages = [*messages[:-1], replace(messages[-1], user_content="\n\n".join(
                part for part in (messages[-1].user_content, text) if part))]
        else:
            prompt += "\n\n" + text
    return prompt, messages, notifications.refs


def notified_tool_tasks(durable: DurableSession, *, owner_task_id: str, store: ManagedOutputStore) -> set[str]:
    """Successful and uncertain sends consume their notifications; unsent work does not."""
    owner = durable.task(owner_task_id)
    observed: set[str] = set()
    for effect in RuntimeEffectStore(durable=durable).load_effects(owner.conversation_id, owner_task_id):
        if effect.intent.effect_category != "provider_send":
            continue
        if effect.settlement is None:
            # A received response is durable before the next generation boundary
            # settles the send. Preparing that next request must consume it too.
            responses = [item for item in durable.artifacts_for_effect(effect.intent.effect_id)
                         if item.metadata.get("kind") == RESPONSE_KIND]
            if not responses:
                continue
            if len(responses) != 1:
                raise ValueError("provider generation has conflicting responses")
            decode_reply(read_provider_artifact(store, responses[0], kind=RESPONSE_KIND,
                session_id=owner.conversation_id, run_id=owner_task_id, effect_id=effect.intent.effect_id))
        elif effect.settlement.sent_state == "not_sent":
            continue
        artifact = request_artifact_for_effect(durable, effect)
        payload = read_provider_artifact(store, artifact, kind=REQUEST_KIND,
            session_id=owner.conversation_id, run_id=owner_task_id, effect_id=effect.intent.effect_id)
        observed.update(ProviderRequestContext.from_payload(payload).tool_task_notifications)
    return observed


def pending_tool_task_notifications(durable: DurableSession, *, owner_task_id: str, store: ManagedOutputStore,
                                    results: Mapping[str, ToolResult]) -> ToolTaskNotifications:
    observed = notified_tool_tasks(durable, owner_task_id=owner_task_id, store=store)
    observed.update(terminal_tool_task_observations(results))
    refs: list[str] = []
    texts: list[str] = []
    for child in durable.owned_tasks(owner_task_id):
        if child.execution_kind != "tool" or child.status not in TASK_TERMINAL_STATUSES or child.task_id in observed:
            continue
        request = ToolTaskInput.from_payload(child.state["tool_input"])
        origin = durable.effect(request.origin_effect_id)
        # A short command's result is already in its original result delivery.
        if origin.settlement is None:
            continue
        from codey.operations.tool_tasks.reads import owned_tool_task

        owned_tool_task(durable, child.task_id, owner_task_id=owner_task_id)
        value = tool_task_snapshot(durable, child, store=store, args={"limit": 1600}, include_output=False)
        value["read_result"] = {"tool": "read_tool_result", "args": {"result_ref": child.task_id}}
        text = json.dumps(value, ensure_ascii=False)
        if sum(map(len, texts)) + len(texts) + len(text) > NOTIFICATION_MAX_CHARS:
            break
        refs.append(child.task_id)
        texts.append(text)
    return ToolTaskNotifications(tuple(refs), "\n".join(texts))
