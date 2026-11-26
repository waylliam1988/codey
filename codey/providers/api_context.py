"""Context maintenance over one provider history, ledger and compaction coordinator."""
from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, TypeGuard

from codey.providers import api_context_state
from codey.providers.base import ProviderToolDefinition
from codey.providers.compaction import ContextSnapshot
from codey.providers.context_checkpoint import (
    ContextRange,
    restore_result_bodies,
)
from codey.providers.context_ledger import digest
from codey.providers.token_accounting import RequestContextCount
from codey.types.protocol_window import ProtocolWindowRef

if TYPE_CHECKING:
    from codey.providers.api_provider import ApiProvider


def _is_api_provider(value: object) -> TypeGuard[ApiProvider]:
    from codey.providers.api_provider import ApiProvider

    provider_type: type[object] = ApiProvider
    return isinstance(value, provider_type)


def restore_declared_tools(provider: object, ref: ProtocolWindowRef,
                           tools: list[ProviderToolDefinition]) -> None:
    """Restore a saved response's transport contract over a cold durable view."""
    from codey.providers.introspection import unwrap_provider

    client = unwrap_provider(provider)
    if not _is_api_provider(client) or not client.native_tools:
        return
    if not client._send_lock.acquire(blocking=False):
        raise RuntimeError("API provider busy: cannot restore declared tools")
    try:
        with client._state_lock:
            if client.window_ref != ref:
                raise ValueError("restored tool contract window mismatch")
            if client._declared_tools:
                if client._declared_tools != tools:
                    raise ValueError("cannot replace a live declared tool contract")
                return
            messages: list[dict[str, object]] = client._messages
            ids = client._durable_source_entry_ids
            prefix = 1 if client.system_prompt and messages else 0
            response: object = client._active_response
            control: object = client._active_control
            if (response is not None or control is not None or len(ids) != len(messages)
                    or any(identity is None for identity in ids[prefix:])):
                raise ValueError("cannot restore declared tools over live protocol history")
            client._declared_tools = list(tools)
    finally:
        client._send_lock.release()


def bind_result_recovery(provider: object, refs: tuple[str, ...]) -> None:
    """Bind the final request's reread capability before compaction or sending."""
    from codey.providers.error_classification import RequestPrepError
    from codey.providers.introspection import optional_callable, unwrap_provider

    client = unwrap_provider(provider)
    if not _is_api_provider(client):
        binder: Callable[[tuple[str, ...]], object] | None = optional_callable(client, "set_result_refs")
        if binder is not None:
            binder(refs)
        return
    with client._state_lock:
        client.set_result_refs(refs)
        # Both work-state summaries and deterministic views already archive
        # their source. Expand that lineage; never reread a changing webpage.
        messages: list[dict[str, object]] = client._messages
        checkpoints: list[dict[str, object]] = client.context_ledger.checkpoints()
        try:
            restored = restore_result_bodies(messages, checkpoints, frozenset(refs))
        except ValueError as exc:
            raise RequestPrepError(f"cannot restore retained receipt bodies: {exc}") from exc
        if restored != messages:
            if len(restored) != len(messages):
                entry_ids = {digest(item): identity for item, identity in
                             zip(messages, client._durable_source_entry_ids, strict=False)}
                client._durable_source_entry_ids = tuple(entry_ids.get(digest(item)) for item in restored)
            client._messages = restored
            client._history_revision += 1


def fork_auxiliary(self: ApiProvider) -> ApiProvider:
    return self._context_fork_auxiliary()


def summarize_context(self: ApiProvider, source: list[dict[str, object]], deadline: float) -> str:
    return api_context_state.summarize_context(self, source, deadline)


def context_snapshot(self: ApiProvider) -> ContextSnapshot:
    return api_context_state.context_snapshot(self)


def count_context(self: ApiProvider, items: list[dict[str, object]], tools: list[ProviderToolDefinition] | None, deadline: float) -> RequestContextCount:
    return api_context_state.count_context(self, items, tools, deadline)


def commit_context(self: ApiProvider, selected: ContextRange, replacement: list[dict[str, object]], checkpoint: dict[str, object], snapshot: ContextSnapshot, tools: list[ProviderToolDefinition] | None, deadline: float) -> bool:
    return api_context_state.commit_context(self, selected, replacement, checkpoint, snapshot, tools, deadline)


def schedule_maintenance(self: ApiProvider) -> None:
    return api_context_state.schedule_maintenance(self)
