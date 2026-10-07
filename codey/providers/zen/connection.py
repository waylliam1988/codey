"""Anonymous partner Zen connection, removable through one registration."""
from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from codey.providers.api_provider import ApiProvider
from codey.providers.api_transport import GenerationRejectedError
from codey.providers.base import AssistantTurn, ProviderToolDefinition
from codey.providers.zen.catalog import ZenCatalog
from codey.providers.zen.identity import CONNECTION_REVISION, ZEN_BASE_URL, ZenIdentity
from codey.runtime.core.api_selection import ApiRunSelection
from codey.storage.local_store import DEFAULT_STATE_HOME

_CATALOG: ZenCatalog | None = None
_WIRE_NAMES = {"list_dir": "ls", "read_file": "read", "grep": "search", "find_references": "references"}


class ZenProvider:
    """Partner tool namespace only; execution and history remain in Codey."""

    name = "OpenCode Zen"
    native_tools = True
    thread_safe_send = True

    def __init__(self, runtime: Any, *, on_plain_refused: Callable[[], None] | None = None,
                 on_plain_succeeded: Callable[[], None] | None = None) -> None:
        self.runtime = runtime
        self.on_plain_refused = on_plain_refused
        self.on_plain_succeeded = on_plain_succeeded

    def __getattr__(self, name: str) -> Any:
        return getattr(self.runtime, name)

    def send(self, text: str, timeout: float | None = None) -> str:
        try:
            reply = str(self.runtime.send(text, timeout=timeout))
        except GenerationRejectedError as exc:
            if exc.status == 403 and exc.error_type == "FreeTierError" and self.on_plain_refused is not None:
                with contextlib.suppress(OSError, ValueError):
                    self.on_plain_refused()
            raise
        if self.on_plain_succeeded is not None:
            with contextlib.suppress(OSError, ValueError):
                self.on_plain_succeeded()
        return reply

    def _exchange(self, method: str, data: Any, tools: list[ProviderToolDefinition] | None, timeout: float | None) -> Any:
        declared = [replace(tool, name=_WIRE_NAMES.get(tool.name, tool.name)) for tool in tools] if tools is not None else None
        turn = getattr(self.runtime, method)(data, declared, timeout=timeout)
        if isinstance(turn, AssistantTurn):
            inverse = {wire: canonical for canonical, wire in _WIRE_NAMES.items()}
            return replace(turn, tool_calls=tuple(replace(call, name=inverse.get(call.name, call.name)) for call in turn.tool_calls))
        return turn

    def send_turn(self, prompt: str, tools: list[ProviderToolDefinition] | None = None, timeout: float | None = None) -> Any:
        return self._exchange("send_turn", prompt, tools, timeout)

    def send_tool_results(self, results: Any, tools: list[ProviderToolDefinition] | None = None, timeout: float | None = None) -> Any:
        return self._exchange("send_tool_results", results, tools, timeout)

    def acknowledge_tool_results(self, results: Any, declared_tools: list[ProviderToolDefinition], timeout: float | None = None) -> Any:
        return self._exchange("acknowledge_tool_results", results, declared_tools, timeout)


def catalog() -> ZenCatalog:
    global _CATALOG
    if _CATALOG is None or _CATALOG.path.parent.parent != DEFAULT_STATE_HOME:
        _CATALOG = ZenCatalog(DEFAULT_STATE_HOME)
    return _CATALOG


def capture_selection(selection: object = None) -> ApiRunSelection:
    directory = catalog()
    if selection is None:
        models = directory.refresh()
        if not models:
            raise ValueError("no supported free Zen models are currently available")
        selection = {"model": models[0].id}
    if not isinstance(selection, dict) or not isinstance(selection.get("model"), str):
        raise ValueError("Zen model selection must be an object with a model")
    model = directory.require(selection["model"])
    effort = selection.get("effort")
    if effort is not None and effort not in model.efforts:
        raise ValueError("selected Zen model does not advertise this reasoning effort")
    reserve = min(8192, model.context // 4)
    return ApiRunSelection("zen", CONNECTION_REVISION, model.id, model.protocol, True, model.context,
                           reserve, min(12000, model.context - reserve), reasoning_effort=effort,
                           stream=True, tool_choice="auto", output_tokens=min(model.output, 8192))


def validate_selection(selection: ApiRunSelection) -> None:
    if selection.connection_revision != CONNECTION_REVISION:
        raise ValueError("original Zen connection agreement is unavailable")
    catalog().require(selection.model_id, protocol=selection.protocol)


def open_selection(selection: ApiRunSelection) -> ZenProvider:
    validate_selection(selection)
    identity = ZenIdentity()

    def headers(base_url: str) -> dict[str, str]:
        validate_selection(selection)
        return identity.headers(base_url)

    provider = ApiProvider(ZEN_BASE_URL, selection.model_id, api_key="public", api_protocol=selection.protocol,
                           stream=selection.stream, tool_choice=selection.tool_choice, output_tokens=selection.output_tokens,
                           request_headers=headers, native_tools=selection.native_tools,
                           context_window_tokens=selection.context_window_tokens, context_reserve_tokens=selection.context_reserve_tokens,
                           context_keep_recent_tokens=selection.context_keep_recent_tokens, reasoning_effort=selection.reasoning_effort,
                           system_prompt="You are a coding assistant.")
    return ZenProvider(provider,
        on_plain_refused=lambda: catalog().access.record_plain_refusal(selection.model_id, selection.protocol),
        on_plain_succeeded=lambda: catalog().access.record_plain_success(selection.model_id, selection.protocol))


def model_payload() -> dict[str, Any]:
    directory = catalog()
    models = directory.refresh()
    return {"id": "zen", "label": "OpenCode Zen", "models": [
                {**model.to_payload(), "review_eligible": directory.access.plain_access(model.id, model.protocol)} for model in models],
            "stale": directory.stale, "error": directory.error, "source": "https://models.opencode.ai/api.json",
            "updated_at": directory.updated, "authentication": "public", "qualification": "checked by the service per request"}
