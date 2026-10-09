"""Anonymous partner Zen connection, removable through one registration."""
from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import replace
from typing import Any

from codey.providers.api_provider import ApiProvider
from codey.providers.api_transport import GenerationRejectedError, GenerationUnknownError
from codey.providers.base import AssistantTurn, ProviderToolDefinition, ProviderToolResult, TurnFinish
from codey.providers.error_classification import OutputLengthError
from codey.providers.zen.catalog import ZenCatalog
from codey.providers.zen.declarations import TEXT_PROMPT_PREFIX, prepare_declarations, transport_identity
from codey.providers.zen.identity import CONNECTION_REVISION, ZEN_BASE_URL, ZenIdentity
from codey.providers.zen.usage import configure_usage, parser_for
from codey.runtime.core import cancellation
from codey.runtime.core.api_selection import ApiRunSelection
from codey.storage.local_store import DEFAULT_STATE_HOME

_CATALOG: ZenCatalog | None = None


class ZenProvider:
    """Removable partner envelope; it never executes a project or shell tool."""

    name = "OpenCode Zen"
    native_tools = True
    thread_safe_send = True

    def __init__(self, runtime: Any, *, on_plain_refused: Callable[[], None] | None = None,
                 on_plain_succeeded: Callable[[], None] | None = None) -> None:
        self.runtime = runtime
        self.on_plain_refused = on_plain_refused
        self.on_plain_succeeded = on_plain_succeeded
        self._send_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._generation = 0
        self._last_text_reply: tuple[str, str] | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self.runtime, name)

    @property
    def model_identity(self) -> str:
        return transport_identity(str(self.runtime.model_identity))

    def abandon_inflight(self) -> None:
        with self._state_lock:
            self._generation += 1
            self._last_text_reply = None
        self.runtime.abandon_inflight()

    def new_chat(self, timeout: float | None = None) -> None:
        self.abandon_inflight()

    def close(self) -> None:
        self.abandon_inflight()

    @contextlib.contextmanager
    def _sending(self) -> Iterator[int]:
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
            turn = self.runtime.acknowledge_tool_results(results, declared, timeout=remaining())
        raise AssertionError("unreachable text-only closure state")

    def normalize_reply(self, reply: str) -> object:
        with self._state_lock:
            previous = self._last_text_reply
        return AssistantTurn(text=reply, reasoning=previous[1]) if previous is not None and previous[0] == reply and previous[1] else reply

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
                           stream=True, tool_choice="auto", output_tokens=min(model.output, reserve), budget_source="model_catalog")


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
                           system_prompt="You are a coding assistant.",
                           usage_parser=parser_for(selection.protocol), configure_request=configure_usage,
                           budget_source=selection.budget_source)
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
