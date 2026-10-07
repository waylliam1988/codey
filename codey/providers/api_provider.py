"""Shared API generation runtime with protocol-owned wire history."""

from __future__ import annotations

import contextlib
import json
import threading
from dataclasses import replace
from typing import Any

from codey.providers.api_chat import _extract_reply, _reasoning_text, decode_turn
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
        context_window_tokens: int | None = None,
        context_reserve_tokens: int | None = None,
        context_keep_recent_tokens: int | None = None,
        thinking_enabled: bool | None = None,
        reasoning_effort: str | None = None,
        api_protocol: str = "openai-completions",
        stream: bool = False,
        tool_choice: str = "required",
        output_tokens: int | None = None,
        request_headers: Any = None,
        native_tools: bool | None = None,
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
        self.api_protocol = api_protocol
        self.stream = stream
        self.tool_choice = tool_choice
        self.output_tokens = output_tokens
        self.request_headers = request_headers
        self.native_tools = native_tools
        self._response_reasoning = ""
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
        from codey.providers.local_response_codec import normalize_local_reply

        normalized = normalize_local_reply(reply)
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

    def _acquire_send(self) -> None:
        if not self._send_lock.acquire(blocking=False):
            raise RuntimeError("API provider busy: concurrent sends are not supported")
        self._inflight_generation = self._generation

    def send(self, text: str, timeout: float | None = None) -> str:
        if self.api_protocol == "openai-responses":
            return self._responses_exchange(
                [{"role": "user", "content": text}], None, timeout, allow_limited_text=False,
            ).text
        self._acquire_send()
        try:
            with self._state_lock:
                generation = self._generation
                candidate = self._prepare_request(
                    [{"role": "user", "content": text}], tools=None,
                )
                self._response_reasoning = ""
            reply = self._complete(candidate, timeout=timeout)
            with self._state_lock:
                # Commit only on a usable reply: an HTTP failure leaves the
                # history untouched so the next send does not resend a stale
                # user message. A stale generation skips history entirely.
                if generation == self._generation:
                    self._messages = candidate
                    self._messages.append({"role": "assistant", "content": reply})
                    self._last_reasoned_reply = (reply, self._response_reasoning)
                    if self._response_reasoning:
                        self._messages[-1]["reasoning_content"] = self._response_reasoning
                else:
                    from codey.providers.api_transport import GenerationUnknownError

                    raise GenerationUnknownError("generation cancelled; late response discarded")
            return reply
        finally:
            self._send_lock.release()

    def _assistant_turn_or_fail_closed(
        self,
        candidate: list[dict[str, Any]],
        message: dict[str, Any],
        *,
        generation: int | None = None,
    ) -> object:
        """Commit one candidate history plus its assistant turn, or fail closed.

        The candidate was built before the HTTP call but never committed:
        only a usable reply installs it, atomically with the assistant
        message, under the state lock (callers hold it). Unanswerable
        tool_calls still reset to a fresh chat; a stale generation (after
        close/new_chat/abandon) never mutates history.
        """
        if generation is not None and generation != self._generation:
            from codey.providers.api_transport import GenerationUnknownError

            raise GenerationUnknownError("generation cancelled; late response discarded")
        turn, stored = decode_turn(message)
        if stored is None:
            self._messages = (
                [{"role": "system", "content": self.system_prompt}] if self.system_prompt else []
            )
        else:
            self._messages = [*candidate, stored]
        return turn

    def send_turn(
        self,
        prompt: str,
        tools: list[ProviderToolDefinition] | None = None,
        timeout: float | None = None,
    ) -> object:
        if self.api_protocol == "openai-responses":
            return self._responses_exchange([{"role": "user", "content": prompt}], tools, timeout)
        self._acquire_send()
        try:
            with self._state_lock:
                generation = self._generation
                candidate = self._prepare_request(
                    [{"role": "user", "content": prompt}], tools=tools,
                )
            message = self._complete_message(candidate, tools=tools, timeout=timeout)
            with self._state_lock:
                turn = self._assistant_turn_or_fail_closed(candidate, message, generation=generation)
                self._declared_tools = list(tools) if tools else self._declared_tools
                return turn
        finally:
            self._send_lock.release()

    def send_tool_results(
        self,
        results: list[ProviderToolResult],
        tools: list[ProviderToolDefinition] | None = None,
        timeout: float | None = None,
    ) -> object:
        if self.api_protocol == "openai-responses":
            from codey.providers.api_responses import encode_results

            return self._responses_exchange(encode_results(results), tools, timeout)
        from codey.providers.api_chat import encode_results

        pending = encode_results(results)
        self._acquire_send()
        try:
            with self._state_lock:
                generation = self._generation
                candidate = self._prepare_request(pending, tools=tools)
            message = self._complete_message(candidate, tools=tools, timeout=timeout)
            with self._state_lock:
                turn = self._assistant_turn_or_fail_closed(candidate, message, generation=generation)
                self._declared_tools = list(tools) if tools else self._declared_tools
                return turn
        finally:
            self._send_lock.release()

    def _responses_exchange(self, pending: list[dict[str, Any]], tools: list[ProviderToolDefinition] | None,
                            timeout: float | None, *, allow_limited_text: bool = True) -> AssistantTurn:
        from codey.providers import api_responses, api_transport

        self._acquire_send()
        try:
            with self._state_lock:
                generation = self._generation
                candidate = api_responses.prepare(self._messages, pending, system=self.system_prompt,
                                                  tools=tools, budget=self._context_budget())
            body = api_transport.generate(
                f"{self.base_url}/responses",
                api_responses.payload(candidate, tools, model=self.model, stream=self.stream,
                                      effort=self.reasoning_effort, choice=self.tool_choice, output_tokens=self.output_tokens),
                self._headers(), timeout=timeout or self.timeout, observe=self._notify_transport_attempt,
                cancelled=lambda: generation != self._generation, opened=self._opened_response,
            )
            turn, output = api_responses.decode(body)
            with self._state_lock:
                if generation != self._generation:
                    raise api_transport.GenerationUnknownError("generation cancelled; late response discarded")
                if not allow_limited_text and turn.finish is TurnFinish.OUTPUT_LIMIT:
                    from codey.providers.error_classification import OutputLengthError

                    raise OutputLengthError("Responses text output truncated; refusing incomplete text execution")
                self._messages = [*candidate, *output]
                self._declared_tools = list(tools) if tools else self._declared_tools
                self._last_reasoned_reply = (turn.text, turn.reasoning)
            return turn
        finally:
            self._send_lock.release()

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
        with self._state_lock:
            if any(tool not in self._declared_tools for tool in declared_tools):
                raise ValueError("terminal tools must belong to the previously declared set")
        return self.send_tool_results(results, declared_tools if self.tool_choice == "auto" else [],
                                      timeout=min(timeout or self.timeout, 30.0))

    def _context_budget(self) -> tuple[int, int, int]:
        """Instance budgets; capability is the single source of defaults."""
        from codey.providers.capabilities import capability_for

        capability = capability_for("local")
        defaults = (
            int(capability.context_window_tokens),
            int(capability.context_reserve_tokens),
            int(capability.context_keep_recent_tokens),
        )
        return (
            self.context_window_tokens or defaults[0],
            self.context_reserve_tokens or defaults[1],
            self.context_keep_recent_tokens or defaults[2],
        )

    def _prepare_request(
        self,
        pending_messages: list[dict[str, Any]],
        tools: list[ProviderToolDefinition] | None = None,
    ) -> list[dict[str, Any]]:
        """Build, compact, and budget-check the next request without mutation.

        Copies history, appends the full pending batch at once (never
        splitting assistant tool_calls from their tool results), compacts
        the candidate in place, then verifies
        ``messages + tools + reserve <= window``. The candidate is returned
        uncommitted: callers install it under the state lock only after a
        usable reply, so HTTP failures and stale generations leave
        ``self._messages`` untouched. Overflow and compaction failures
        raise explicitly; nothing is sent. Callers hold the state lock.
        """
        from codey.providers.api_chat import prepare

        return prepare(self._messages, pending_messages, system=self.system_prompt, tools=tools, budget=self._context_budget())

    def _request_payload(self, messages: list[dict[str, Any]], tools: list[ProviderToolDefinition] | None) -> dict[str, Any]:
        from codey.providers.api_chat import payload

        return payload(messages, tools, model=self.model, stream=self.stream, temperature=self.temperature,
                       thinking=self.thinking_enabled, effort=self.reasoning_effort, choice=self.tool_choice,
                       output_tokens=self.output_tokens)

    def _post_chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[ProviderToolDefinition] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        from codey.providers import api_transport

        return api_transport.generate(
            f"{self.base_url}/chat/completions", self._request_payload(messages, tools), self._headers(),
            timeout=timeout or self.timeout, observe=self._notify_transport_attempt,
            cancelled=lambda: self._inflight_generation != self._generation, opened=self._opened_response,
        )

    def _notify_transport_attempt(self, **kwargs: Any) -> None:
        self._notify_http_attempt(**kwargs)

    def _notify_http_attempt(self, attempt: int, data: bytes, phase: str, response_bytes: int, seconds: float) -> None:
        # Diagnostic storage must not turn a successful request into a retry.
        with contextlib.suppress(Exception):
            self._observe_http_attempt(attempt=attempt, data=data, phase=phase,
                                       response_bytes=response_bytes, seconds=seconds)

    def _observe_http_attempt(self, *, attempt: int, data: bytes, phase: str,
                              response_bytes: int, seconds: float) -> None:
        """Optional transport observation; authorization headers are never supplied."""

    def _complete_message(
        self,
        messages: list[dict[str, Any]],
        tools: list[ProviderToolDefinition] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        from codey.providers.api_chat import decode_message

        return decode_message(self._post_chat(messages, tools, timeout=timeout))

    def _complete(self, messages: list[dict[str, Any]], *, timeout: float | None = None) -> str:
        body = self._post_chat(messages, None, timeout=timeout)
        reply = _extract_reply(body)
        choices = body.get("choices")
        choice = choices[0] if isinstance(choices, list) and choices else None
        message = choice.get("message") if isinstance(choice, dict) else None
        self._response_reasoning = _reasoning_text(message)
        return reply
