"""Anonymous partner Zen connection, removable through one registration."""
from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable, Generator
from dataclasses import replace
from typing import Any

from codey.providers.api_provider import ApiProvider
from codey.providers.api_transport import GenerationRejectedError, GenerationUnknownError
from codey.providers.base import AssistantTurn, ProviderToolDefinition, ProviderToolResult, TurnFinish
from codey.providers.error_classification import OutputLengthError
from codey.providers.token_accounting import RequestContextCount
from codey.providers.zen.catalog import ZenCatalog
from codey.providers.zen.declarations import TEXT_PROMPT_PREFIX, prepare_declarations, transport_identity
from codey.providers.zen.identity import CONNECTION_REVISION, ZenIdentity, revision_for_endpoint
from codey.providers.zen.node_http import open_request as open_zen_request
from codey.providers.zen.reliability import ZenRequestPolicy, ZenResourcesRegistry, make_control_scope
from codey.providers.zen.usage import configure_usage, parser_for
from codey.runtime.core import cancellation
from codey.runtime.core.api_selection import ApiRunSelection
from codey.runtime.core.reasoning import compile_selection
from codey.storage.local_store import DEFAULT_STATE_HOME

_CATALOG: ZenCatalog | None = None


def status_key() -> str:
    """Return the connection/catalog identity used by the status cache."""
    return CONNECTION_REVISION


def probe_status(_request: object) -> dict[str, object]:
    payload = model_payload()
    models = payload.get("models")
    usable = isinstance(models, list) and bool(models)
    return {
        "state": "ready" if usable else "unavailable",
        "source": "api_catalog",
        "stale": payload.get("stale") is True,
        "error": str(payload.get("error") or ""),
        "models": models if isinstance(models, list) else [],
        "payload": payload,
    }


class ZenProvider:
    """Removable partner envelope; it never executes a project or shell tool."""

    name = "OpenCode Zen"
    native_tools = True
    thread_safe_send = True

    def __init__(self, runtime: ApiProvider, *, on_plain_refused: Callable[[], None] | None = None,
                 on_plain_succeeded: Callable[[], None] | None = None,
                 on_close: Callable[[], None] | None = None) -> None:
        self.runtime = runtime
        self.on_plain_refused = on_plain_refused
        self.on_plain_succeeded = on_plain_succeeded
        self.on_close = on_close
        self._closed = False
        self._send_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._generation = 0
        self._last_text_reply: tuple[str, str] | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self.runtime, name)

    @property
    def model_identity(self) -> str:
        return transport_identity(self.runtime.model_identity)

    def abandon_inflight(self) -> None:
        with self._state_lock:
            self._generation += 1
            self._last_text_reply = None
        self.runtime.abandon_inflight()

    def new_chat(self, timeout: float | None = None) -> None:
        self.reset_window()

    def reset_window(self, expected_ref: Any = None) -> Any:
        self.abandon_inflight()
        return self.runtime.reset_window(expected_ref)

    def load_durable_context(self, messages: list[dict[str, Any]], *,
                             source_entry_ids: tuple[str, ...] = ()) -> None:
        # Window planning discovers this capability on the concrete type.
        # __getattr__ forwarding alone cannot declare a cold replay window.
        self.runtime.load_durable_context(messages, source_entry_ids=source_entry_ids)

    def bind_context(self, session_id: str) -> None:
        self.runtime.bind_context(session_id)

    def close(self) -> None:
        with self._state_lock:
            self._generation += 1
            self._last_text_reply = None
        self.runtime.close()
        if not self._closed and self.on_close is not None:
            self._closed = True
            self.on_close()

    @contextlib.contextmanager
    def _sending(self) -> Generator[int, None, None]:
        if not self._send_lock.acquire(blocking=False):
            raise RuntimeError("Zen provider busy: concurrent sends are not supported")
        try:
            with self._state_lock:
                generation = self._generation
            yield generation
        finally:
            self._send_lock.release()

    def _check_generation(self, generation: int) -> None:
        cancellation.check()
        with self._state_lock:
            if generation != self._generation:
                raise GenerationUnknownError("Zen generation cancelled; late reply discarded")

    def count_text(self, text: str, *, deadline: float) -> RequestContextCount:
        declared, _ = prepare_declarations(None)
        return self.runtime.count_text(TEXT_PROMPT_PREFIX + text, tools=declared, deadline=deadline)

    def send(self, text: str, timeout: float | None = None) -> str:
        try:
            with self._sending() as generation:
                reply = self._send_text(text, timeout, generation)
        except GenerationRejectedError as exc:
            if exc.status == 403 and exc.error_type == "FreeTierError" and self.on_plain_refused is not None:
                with contextlib.suppress(OSError, ValueError):
                    self.on_plain_refused()
            raise
        if self.on_plain_succeeded is not None:
            with contextlib.suppress(OSError, ValueError):
                self.on_plain_succeeded()
        return reply

    def _send_text(self, text: str, timeout: float | None, generation: int) -> str:
        declared, _ = prepare_declarations(None)
        deadline = time.monotonic() + (timeout if timeout is not None else self.runtime.timeout)

        def remaining() -> float:
            self._check_generation(generation)
            budget = deadline - time.monotonic()
            if budget <= 0:
                raise cancellation.DeadlineExceeded("Zen text-only exchange deadline exceeded")
            return budget

        prompt = TEXT_PROMPT_PREFIX + text
        turn = self.runtime.send_turn(prompt, declared, timeout=remaining())
        # Only protocol rejection receipts, never tool execution or HTTP retry.
        # Two closure rounds share the original total timeout.
        for closure in range(3):
            self._check_generation(generation)
            if not isinstance(turn, AssistantTurn):
                raise RuntimeError("Zen text-only exchange returned an invalid turn")
            if turn.finish is TurnFinish.PROTOCOL_ERROR:
                self.abandon_inflight()
                raise RuntimeError("Zen text-only exchange returned malformed tool calls")
            if turn.finish is TurnFinish.OUTPUT_LIMIT:
                self.abandon_inflight()
                raise OutputLengthError("Zen text-only reply was truncated")
            if not turn.tool_calls:
                with self._state_lock:
                    self._check_generation(generation)
                    self._last_text_reply = (turn.text, turn.reasoning)
                return turn.text
            if closure == 2:
                self.abandon_inflight()
                raise RuntimeError("Zen text-only tool refusals exceeded their closure budget")
            results = [ProviderToolResult(call.id,
                "Not executed: tools are unavailable for this text-only request. Reply with text only.") for call in turn.tool_calls]
            turn = self.runtime.send_tool_results(results, declared, timeout=remaining())
        raise AssertionError("unreachable text-only closure state")

    def normalize_reply(self, reply: str) -> object:
        with self._state_lock:
            previous = self._last_text_reply
        return AssistantTurn(text=reply, reasoning=previous[1], raw={"text_protocol": True}) if previous is not None and previous[0] == reply and previous[1] else reply

    def _exchange(self, method: str, data: Any, tools: list[ProviderToolDefinition] | None, timeout: float | None) -> Any:
        declared, inverse = prepare_declarations(tools)
        with self._sending() as generation:
            self._check_generation(generation)
            turn = getattr(self.runtime, method)(data, declared, timeout=timeout)
            self._check_generation(generation)
        if isinstance(turn, AssistantTurn):
            return replace(turn, tool_calls=tuple(replace(call, name=inverse.get(call.name, call.name)) for call in turn.tool_calls))
        return turn

    def send_turn(self, prompt: str, tools: list[ProviderToolDefinition] | None = None, timeout: float | None = None) -> Any:
        return self._exchange("send_turn", prompt, tools, timeout)

    def send_tool_results(self, results: Any, tools: list[ProviderToolDefinition] | None = None, timeout: float | None = None) -> Any:
        return self._exchange("send_tool_results", results, tools, timeout)

    def send_tool_results_bound(self, results: Any, window_ref: Any,
                                tools: list[ProviderToolDefinition] | None = None,
                                timeout: float | None = None,
                                delivery_commit: dict[str, Any] | None = None) -> Any:
        declared, inverse = prepare_declarations(tools)
        with self._sending() as generation:
            self._check_generation(generation)
            turn: Any = self.runtime.send_tool_results_bound(
                results, window_ref, declared, timeout=timeout, delivery_commit=delivery_commit,
            )
            self._check_generation(generation)
        if isinstance(turn, AssistantTurn):
            return replace(turn, tool_calls=tuple(replace(call, name=inverse.get(call.name, call.name)) for call in turn.tool_calls))
        return turn

    def acknowledge_tool_results(self, results: Any, declared_tools: list[ProviderToolDefinition], timeout: float | None = None) -> Any:
        return self._exchange("acknowledge_tool_results", results, declared_tools, timeout)

    def acknowledge_tool_results_bound(self, results: Any, window_ref: Any,
                                       declared_tools: list[ProviderToolDefinition], timeout: float | None = None) -> Any:
        declared, _ = prepare_declarations(declared_tools)
        with self._sending() as generation:
            self._check_generation(generation)
            turn = self.runtime.acknowledge_tool_results_bound(
                results, window_ref, declared, timeout=timeout,
            )
            self._check_generation(generation)
        return turn


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
    # OpenCode's effort axis is authoritative when a model advertises both a
    # toggle and effort values. This compiles to one wire control, matching
    # its Default/Low/High/Max variants and avoiding enable_thinking=true plus
    # reasoning_effort=X being interpreted differently by provider adapters.
    effort, thinking = compile_selection(
        efforts=model.efforts,
        toggle=model.thinking_toggle,
        effort=selection.get("effort"),
        thinking=selection.get("thinking"),
    )
    reserve = min(8192, model.context // 4)
    return ApiRunSelection("zen", revision_for_endpoint(model.api), model.id, model.protocol, True, model.context,
                           reserve, min(12000, model.context - reserve), reasoning_effort=effort,
                           thinking_enabled=thinking,
                           stream=True, tool_choice="auto", output_tokens=min(model.output, reserve), budget_source="model_catalog",
                           input_limit_tokens=model.input_limit_tokens)


def validate_selection(selection: ApiRunSelection) -> None:
    model = catalog().require(selection.model_id, protocol=selection.protocol)
    if selection.connection_revision != revision_for_endpoint(model.api):
        raise ValueError("Zen connection endpoint changed; start a new explicitly admitted request")


def endpoint_for_selection(selection: ApiRunSelection) -> str:
    """Read the admitted endpoint for observers without creating a runtime."""
    endpoint = catalog().require_cached(selection.model_id, protocol=selection.protocol).api
    if selection.connection_revision != revision_for_endpoint(endpoint):
        raise ValueError("Zen connection endpoint changed while opening the selection")
    return endpoint


def open_selection(selection: ApiRunSelection) -> ZenProvider:
    validate_selection(selection)
    endpoint = endpoint_for_selection(selection)
    identity = ZenIdentity()
    scope = make_control_scope(endpoint, "public", selection.connection_revision)
    coordinator = ZenResourcesRegistry.get_or_create(scope)
    http_session = ZenResourcesRegistry.get_session(scope)

    def headers(base_url: str) -> dict[str, str]:
        return identity.headers(base_url)

    provider = ApiProvider(endpoint, selection.model_id, api_key="public", api_protocol=selection.protocol,
                           stream=selection.stream, tool_choice=selection.tool_choice, output_tokens=selection.output_tokens,
                           request_headers=headers, native_tools=selection.native_tools,
                           context_window_tokens=selection.context_window_tokens, context_reserve_tokens=selection.context_reserve_tokens,
                           context_keep_recent_tokens=selection.context_keep_recent_tokens, reasoning_effort=selection.reasoning_effort,
                           thinking_enabled=selection.thinking_enabled,
                           system_prompt="You are a coding assistant.",
                           usage_parser=parser_for(selection.protocol), configure_request=configure_usage,
                           budget_source=selection.budget_source, input_limit_tokens=selection.input_limit_tokens,
                           request_policy=ZenRequestPolicy(coordinator, scope),
                           connection_revision=selection.connection_revision,
                           http_session=http_session,
                           transport_options={"body_idle_timeout": 30.0, "opener": open_zen_request,
                                              "request_id_headers": ("X-OpenCode-Log-Id",)})
    provider.auxiliary_connection = ZenProvider
    wrapped = ZenProvider(provider,
        on_plain_refused=lambda: catalog().access.record_plain_refusal(selection.model_id, selection.protocol),
        on_plain_succeeded=lambda: catalog().access.record_plain_success(selection.model_id, selection.protocol),
        on_close=lambda: ZenResourcesRegistry.release(scope))
    return wrapped


def model_payload() -> dict[str, Any]:
    return refresh_models(force=False)


def refresh_models(*, force: bool = True) -> dict[str, Any]:
    catalog().refresh(force=force)
    return cached_model_payload()


def cached_model_payload() -> dict[str, Any]:
    directory = catalog()
    payload = directory.cached_payload()
    for model in payload["models"]:
        model["review_eligible"] = directory.access.plain_access(model["id"], model["protocol"])
    return {**payload, "id": "zen", "label": "OpenCode Zen", "source": "https://models.opencode.ai/api.json",
            "authentication": "public", "qualification": "checked by the service per request"}
