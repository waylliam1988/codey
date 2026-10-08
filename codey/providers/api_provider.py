"""Shared API generation runtime with protocol-owned wire history."""

from __future__ import annotations

import contextlib
import json
import threading
from collections.abc import Callable
from dataclasses import replace
from typing import Any, cast

from codey.providers.api_codec import ApiCodec, GenerationSettings
from codey.providers.base import AssistantTurn, ProviderToolDefinition, ProviderToolResult, TurnFinish

DEFAULT_TIMEOUT = 600.0
DEFAULT_TEMPERATURE = 0.3
# Socket-timeout rationale (stream=False: the timeout IS the full-generation
# budget, since Kobold replies only after the whole completion). Kobold's
# 1024-token window at the slowest observed local speed (~2 tok/s) needs
# ~540s; 600s covers it with margin. A dead localhost fails fast with
# connection-refused, so a large cap only binds accepted-but-silent servers.
# Residual: sub-1 tok/s hardware can still outrun any fixed cap; the durable
# answer is streaming (progress-observable) output, not a larger number.
# Memory bound only (not a total time guarantee): a runaway local service
# must not grow the UI process without limit.


class ApiProvider:
    name = "Local"
    # Single in-flight (fail-fast) + generation: sequential worker-thread
    # sends are safe and Stop-abandoned late replies skip history.
    thread_safe_send = True

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        api_key: str = "",
        timeout: float = DEFAULT_TIMEOUT,
        temperature: float = DEFAULT_TEMPERATURE,
        system_prompt: str = "",
        context_window_tokens: int = 32768,
        context_reserve_tokens: int = 8192,
        context_keep_recent_tokens: int = 12000,
        thinking_enabled: bool | None = None,
        reasoning_effort: str | None = None,
        api_protocol: str = "openai-completions",
        stream: bool = False,
        tool_choice: str = "required",
        output_tokens: int | None = None,
        request_headers: Any = None,
        native_tools: bool | None = None,
        text_decoder: Callable[[str], str | AssistantTurn] | None = None,
    ) -> None:
        """Runtime only: the target is already resolved (no env/discovery)."""
        if not base_url.strip():
            raise ValueError("base_url is required")
        if not model.strip():
            raise ValueError("model is required")
        self.base_url = base_url.strip().rstrip("/")
        self.model = model.strip()
        self.api_key = api_key
        self.timeout = timeout
        self.temperature = temperature
        self.system_prompt = system_prompt
        self.context_window_tokens = context_window_tokens
        self.context_reserve_tokens = context_reserve_tokens
        self.context_keep_recent_tokens = context_keep_recent_tokens
        self.thinking_enabled = thinking_enabled
        self.reasoning_effort = reasoning_effort
        if api_protocol not in {"openai-completions", "openai-responses"}:
            raise ValueError("unsupported API protocol")
        from codey.providers import api_chat, api_responses

        self._codec = cast(ApiCodec, {"openai-completions": api_chat, "openai-responses": api_responses}[api_protocol])
        self.api_protocol = api_protocol
        self.stream = stream
        self.tool_choice = tool_choice
        self.output_tokens = output_tokens
        self.request_headers = request_headers
        self.native_tools = native_tools
        self._text_decoder = text_decoder
        self._last_reasoned_reply: tuple[str, str] | None = None
        self._messages: list[dict[str, Any]] = []
        self._state_lock = threading.RLock()
        self._send_lock = threading.Lock()
        self._generation = 0
        self._active_response: Any = None
        self._inflight_generation = 0
        self._declared_tools: list[ProviderToolDefinition] = []

    @property
    def model_identity(self) -> str:
        import hashlib

        settings = {name: getattr(self, name) for name in (
            "base_url", "model", "api_protocol", "temperature", "system_prompt", "context_window_tokens",
            "context_reserve_tokens", "context_keep_recent_tokens", "reasoning_effort", "thinking_enabled",
            "stream", "tool_choice", "output_tokens",
        )}
        return hashlib.sha256(json.dumps(settings, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    @property
    def has_transport_history(self) -> bool:
        with self._state_lock:
            return bool(self._messages)

    @property
    def location(self) -> str:
        return f"{self.base_url} ({self.model})"

    def new_chat(self, timeout: float | None = None) -> None:
        self.abandon_inflight()

    def close(self) -> None:
        self.abandon_inflight()

    def normalize_reply(self, reply: str) -> object:
        """Normalize provider-specific text frames before kernel parsing."""
        normalized = self._text_decoder(reply) if self._text_decoder is not None else reply
        reasoning = self.reasoning_for_reply(reply)
        if not reasoning:
            return normalized
        from codey.providers.base import AssistantTurn

        return replace(normalized, reasoning=reasoning) if isinstance(normalized, AssistantTurn) else AssistantTurn(
            text=reply, reasoning=reasoning,
        )

    def reasoning_for_reply(self, reply: str) -> str:
        """Optional display data for the latest accepted plain exchange only."""
        with self._state_lock:
            previous = self._last_reasoned_reply
            return previous[1] if previous is not None and previous[0] == reply else ""

    def abandon_inflight(self) -> None:
        """Cancel the active socket and invalidate all late history commits."""
        from codey.providers.api_transport import abort_response

        with self._state_lock:
            self._generation += 1
            response, self._active_response = self._active_response, None
            self._messages = []
            self._declared_tools = []
            self._last_reasoned_reply = None
        abort_response(response)

    def _opened_response(self, response: Any) -> None:
        from codey.providers.api_transport import abort_response

        with self._state_lock:
            stale = self._inflight_generation != self._generation
            if not stale:
                self._active_response = response
        if stale:
            abort_response(response)

    def send(self, text: str, timeout: float | None = None) -> str:
        return self._exchange([{"role": "user", "content": text}], None, timeout=timeout, text_only=True).text

    def send_turn(self, prompt: str, tools: list[ProviderToolDefinition] | None = None,
                  timeout: float | None = None) -> AssistantTurn:
        return self._exchange([{"role": "user", "content": prompt}], tools, timeout=timeout)

    def send_tool_results(self, results: list[ProviderToolResult], tools: list[ProviderToolDefinition] | None = None,
                          timeout: float | None = None) -> AssistantTurn:
        return self._exchange(self._codec.encode_results(results), tools, timeout=timeout)

    def _exchange(self, pending: list[dict[str, Any]], tools: list[ProviderToolDefinition] | None,
                  *, timeout: float | None = None, text_only: bool = False,
                  terminal_tools: list[ProviderToolDefinition] | None = None) -> AssistantTurn:
        """One atomic lifecycle for both codecs, with no protocol-specific commit path."""
        from codey.providers import api_transport
        from codey.providers.error_classification import OutputLengthError

        if not self._send_lock.acquire(blocking=False):
            raise RuntimeError("API provider busy: concurrent sends are not supported")
        try:
            with self._state_lock:
                generation = self._inflight_generation = self._generation
                if terminal_tools is not None and any(tool not in self._declared_tools for tool in terminal_tools):
                    raise ValueError("terminal tools must belong to the previously declared set")
                candidate = self._codec.prepare(self._messages, pending, system=self.system_prompt,
                                                 tools=tools, budget=self._context_budget())
            body = self._generate(candidate, tools, timeout=timeout)
            turn, output = self._codec.decode_exchange(body, text_only=text_only,
                                                       text_decoder=self._text_decoder)
            with self._state_lock:
                if generation != self._generation:
                    raise api_transport.GenerationUnknownError("generation cancelled; late response discarded")
                if text_only and turn.finish is TurnFinish.OUTPUT_LIMIT:
                    raise OutputLengthError("text-only output truncated; refusing incomplete text execution")
                if text_only and turn.tool_calls:
                    raise RuntimeError("unexpected native tool calls in a text-only exchange")
                if output is None:
                    # Malformed native batches cannot be retained as legal history.
                    self._messages = [{"role": "system", "content": self.system_prompt}] if self.system_prompt else []
                    self._declared_tools = []
                    self._last_reasoned_reply = None
                else:
                    self._messages = [*candidate, *output]
                    if tools:
                        self._declared_tools = list(tools)
                    self._last_reasoned_reply = (turn.text, turn.reasoning)
            return turn
        finally:
            with self._state_lock:
                self._active_response = None
            self._send_lock.release()

    def _generate(self, messages: list[dict[str, Any]], tools: list[ProviderToolDefinition] | None = None,
                  *, timeout: float | None = None) -> dict[str, Any]:
        from codey.providers import api_transport

        settings = GenerationSettings(self.model, self.stream, self.temperature, self.thinking_enabled,
                                      self.reasoning_effort, self.tool_choice, self.output_tokens)
        return api_transport.generate(
            f"{self.base_url}/{self._codec.endpoint}", self._codec.build_payload(messages, tools, settings), self._headers(),
            timeout=timeout or self.timeout, observe=self._notify_http_attempt,
            cancelled=lambda: self._inflight_generation != self._generation, opened=self._opened_response,
        )

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if self.request_headers is not None:
            headers.update(self.request_headers(self.base_url))
        return headers

    def acknowledge_tool_results(self, results: list[ProviderToolResult],
                                 declared_tools: list[ProviderToolDefinition], timeout: float | None = None) -> object:
        """Deliver terminal results; the kernel never executes returned calls.

        Auto-only services retain the admitted declaration set. Other services
        withdraw declarations. This is a bounded generation, not a network ACK.
        """
        return self._exchange(self._codec.encode_results(results),
                              declared_tools if self.tool_choice == "auto" else [],
                              timeout=min(timeout or self.timeout, 30.0), terminal_tools=declared_tools)

    def _context_budget(self) -> tuple[int, int, int]:
        """Resolved generic limits; connection factories supply their own budgets."""
        return self.context_window_tokens, self.context_reserve_tokens, self.context_keep_recent_tokens

    def _notify_http_attempt(self, attempt: int, data: bytes, phase: str, response_bytes: int, seconds: float) -> None:
        # Diagnostic storage must not turn a successful request into a retry.
        with contextlib.suppress(Exception):
            self._observe_http_attempt(attempt=attempt, data=data, phase=phase,
                                       response_bytes=response_bytes, seconds=seconds)

    def _observe_http_attempt(self, *, attempt: int, data: bytes, phase: str,
                              response_bytes: int, seconds: float) -> None:
        """Optional transport observation; authorization headers are never supplied."""
