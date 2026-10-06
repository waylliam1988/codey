"""OpenAI-compatible local model provider."""

from __future__ import annotations

import contextlib
import http.client
import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import replace
from typing import Any, cast

from codey.providers import local_config as _local_config
from codey.providers import local_discovery as _local_discovery

DEFAULT_TIMEOUT = 600.0
DEFAULT_TEMPERATURE = 0.3
# Socket-timeout rationale (stream=False: the timeout IS the full-generation
# budget, since Kobold replies only after the whole completion). Kobold's
# 1024-token window at the slowest observed local speed (~2 tok/s) needs
# ~540s; 600s covers it with margin. A dead localhost fails fast with
# connection-refused, so a large cap only binds accepted-but-silent servers.
# Residual: sub-1 tok/s hardware can still outrun any fixed cap; the durable
# answer is streaming (progress-observable) output, not a larger number.
_RESPONSE_PREVIEW_LIMIT = 400
_RESPONSE_RETRIES = 1
# Memory bound only (not a total time guarantee): a runaway local service
# must not grow the UI process without limit.
_CHAT_RESPONSE_MAX_BYTES = 16 * 1024 * 1024
_ERROR_BODY_MAX_BYTES = 2000


class _RetryableResponseError(RuntimeError):
    pass


class LocalOpenAIProvider:
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
        self._response_reasoning = ""
        self._last_reasoned_reply: tuple[str, str] | None = None
        self._messages: list[dict[str, Any]] = []
        self._state_lock = threading.RLock()
        self._send_lock = threading.Lock()
        self._generation = 0

    @classmethod
    def connect(cls, *, config: _local_config.LocalProviderConfig | None = None,
                verify_thinking: bool = False) -> LocalOpenAIProvider:
        """Connect to the single selected target (address + model + key).

        Offline is a hard error for preflight failover. An explicit
        address never falls back to another service, and probing uses the
        same key the provider sends with.
        """
        captured = config is not None
        config = config if captured else _local_config.load_local_config()
        assert config is not None
        selection = (_local_config.LocalTargetSelection(config.base_url, config.model, config.api_key, "config")
                     if captured else _local_config.select_local_target(config))
        endpoint = _local_discovery.resolve_local_endpoint(
            base_url=selection.base_url,
            model=selection.model,
            api_key=selection.api_key,
        )
        if endpoint is None:
            if selection.base_url:
                configured = selection.base_url
            elif selection.model:
                configured = f"auto-discovery (no configured address; model {selection.model!r} not found)"
            else:
                configured = "auto-discovery (no configured address)"
            raise RuntimeError(
                f"could not reach local model at {configured}: "
                "no OpenAI-compatible /models endpoint"
            )
        effective = _local_config.resolve_effective_local_config(config, selection, endpoint)
        if verify_thinking and config.thinking_enabled is not None:
            from codey.providers.local_selection import model_metadata

            metadata = model_metadata(effective.base_url, effective.model, api_key=effective.api_key)
            options = metadata.get("thinking_options")
            if not isinstance(options, list) or not options:
                raise ValueError("This model does not advertise a supported thinking control.")
            if config.reasoning_effort is not None and config.reasoning_effort not in options:
                raise ValueError("This model does not support the selected thinking effort.")
        if not effective.model:
            raise RuntimeError(
                f"local model at {effective.base_url} returned no usable model"
            )
        return cls(
            effective.base_url,
            effective.model,
            api_key=effective.api_key,
            context_window_tokens=effective.context.context_window_tokens,
            context_reserve_tokens=effective.context.context_reserve_tokens,
            context_keep_recent_tokens=effective.context.context_keep_recent_tokens,
            thinking_enabled=config.thinking_enabled if selection.base_url == config.base_url else None,
            reasoning_effort=config.reasoning_effort if selection.base_url == config.base_url else None,
        )

    @property
    def location(self) -> str:
        return f"{self.base_url} ({self.model})"

    def new_chat(self, timeout: float | None = None) -> None:
        with self._state_lock:
            self._generation += 1
            self._messages = []
            self._last_reasoned_reply = None

    def close(self) -> None:
        with self._state_lock:
            self._generation += 1
            self._messages = []
            self._last_reasoned_reply = None

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
        """Invalidate a still-running send so its late reply skips history."""
        with self._state_lock:
            self._generation += 1
            self._messages = []
            self._last_reasoned_reply = None

    def _acquire_send(self) -> None:
        if not self._send_lock.acquire(blocking=False):
            raise RuntimeError("local provider busy: concurrent sends are not supported")

    def send(self, text: str, timeout: float | None = None) -> str:
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
        from codey.providers.base import AssistantTurn, ProviderToolCall
        from codey.providers.local_response_codec import normalize_local_reply

        parsed, dropped = _parse_tool_calls(message)
        text = str(message.get("content") or "")
        metadata_turn = normalize_local_reply(text)
        provider_metadata = {}
        if isinstance(metadata_turn, AssistantTurn):
            provider_metadata = dict(metadata_turn.raw)
            text = metadata_turn.text
            if not parsed and not dropped and not message.get("_continuable_length") and metadata_turn.tool_calls:
                parsed = [
                    {"id": call.id, "name": call.name, "arguments": dict(call.arguments)}
                    for call in metadata_turn.tool_calls
                ]
                message = {**message, "content": text, "tool_calls": [
                    {"id": call["id"], "type": "function", "function": {
                        "name": call["name"], "arguments": json.dumps(call["arguments"], ensure_ascii=False),
                    }} for call in parsed
                ]}
        if generation is not None and generation != self._generation:
            return AssistantTurn(
                text=text,
                tool_calls=tuple(
                    ProviderToolCall(id=str(call["id"]), name=str(call["name"]), arguments=cast(dict[str, Any], call.get("arguments")) if isinstance(call.get("arguments"), dict) else {})
                    for call in parsed
                ) if not dropped else (),
                raw={"finish_reason": str(message.get("_finish_reason") or ""), "stale_generation": True,
                     "continuable_length": bool(message.get("_continuable_length")), **provider_metadata},
            )
        if dropped:
            self._messages = (
                [{"role": "system", "content": self.system_prompt}] if self.system_prompt else []
            )
            # Content cannot rescue a malformed native batch as a JSON call.
            text = f"ERROR: local model returned {dropped} malformed tool call(s)"
            return AssistantTurn(
                text=text,
                tool_calls=(),
                raw={"finish_reason": str(message.get("_finish_reason") or ""), "malformed_dropped": dropped,
                     **provider_metadata},
            )
        self._messages = candidate
        self._messages.append(_store_assistant_message(message))
        return AssistantTurn(
            text=text,
            reasoning=_reasoning_text(message),
            tool_calls=tuple(
                ProviderToolCall(id=str(call["id"]), name=str(call["name"]), arguments=cast(dict[str, Any], call.get("arguments")) if isinstance(call.get("arguments"), dict) else {})
                for call in parsed
            ),
            raw={"finish_reason": str(message.get("_finish_reason") or ""),
                 "continuable_length": bool(message.get("_continuable_length")), **provider_metadata},
        )

    def send_turn(
        self,
        prompt: str,
        tools: list[dict[str, object]] | None = None,
        timeout: float | None = None,
    ) -> object:
        self._acquire_send()
        try:
            with self._state_lock:
                generation = self._generation
                candidate = self._prepare_request(
                    [{"role": "user", "content": prompt}], tools=tools,
                )
            message = self._complete_message(candidate, tools=tools, timeout=timeout)
            with self._state_lock:
                return self._assistant_turn_or_fail_closed(candidate, message, generation=generation)
        finally:
            self._send_lock.release()

    def send_tool_results(
        self,
        results: list[dict[str, object]],
        tools: list[dict[str, object]] | None = None,
        timeout: float | None = None,
    ) -> object:
        pending: list[dict[str, Any]] = []
        for item in results:
            pending.append({
                "role": "tool",
                "tool_call_id": item.get("tool_call_id"),
                "content": str(item.get("content") or ""),
            })
        self._acquire_send()
        try:
            with self._state_lock:
                generation = self._generation
                candidate = self._prepare_request(pending, tools=tools)
            message = self._complete_message(candidate, tools=tools, timeout=timeout)
            with self._state_lock:
                return self._assistant_turn_or_fail_closed(candidate, message, generation=generation)
        finally:
            self._send_lock.release()

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
        tools: list[dict[str, object]] | None = None,
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
        from codey.providers import error_classification as errors

        candidate: list[dict[str, object]] = [dict(message) for message in self._messages]
        if not candidate and self.system_prompt:
            candidate.append({"role": "system", "content": self.system_prompt})
        for item in pending_messages:
            candidate.append(dict(item))
        window, reserve, keep = self._context_budget()
        from codey.agents import context_compaction as compaction

        groups = compaction.group_messages_for_compaction(candidate)
        if any(not compaction.is_tool_group_complete(candidate, group) for group in groups):
            raise errors.RequestPrepError("local tool history has incomplete or invalid ID pairing")
        try:
            compaction.compact_openai_messages_in_place(
                candidate,
                tools=tools,
                context_window_tokens=window,
                reserve_tokens=reserve,
                keep_recent_tokens=keep,
            )
        except Exception as exc:
            raise errors.RequestPrepError(f"local context compaction failed: {exc}") from exc
        try:
            estimated = (
                compaction.estimate_messages_tokens(candidate)
                + compaction.estimate_tools_tokens(tools)
                + int(reserve)
            )
        except Exception as exc:
            raise errors.RequestPrepError(f"local context estimation failed: {exc}") from exc
        if estimated > int(window):
            raise errors.ContextOverflowError(
                f"local model context overflow before send: estimated {estimated} "
                f"tokens exceeds window {int(window)} "
                f"(reserve {int(reserve)} for reply)"
            )
        return candidate

    def _request_payload(self, messages: list[dict[str, Any]], tools: list[dict[str, object]] | None) -> dict[str, Any]:
        """One wire contract, also used by the diagnostic request recorder."""
        payload: dict[str, object] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "stream": False,
        }
        if self.thinking_enabled is not None:
            payload["chat_template_kwargs"] = {"enable_thinking": self.thinking_enabled}
        if self.reasoning_effort is not None:
            payload["reasoning_effort"] = "none" if self.reasoning_effort == "off" else self.reasoning_effort
        if tools:
            payload["tools"] = tools
            # Kernel turns require a call (including done). Ordinary answers
            # belong to send(), or terminal receipts with tools withdrawn.
            payload["tool_choice"] = "required"
            payload["parallel_tool_calls"] = False
        elif tools is not None and messages and messages[-1].get("role") == "tool":
            # Terminal receipt: the answer and completion proof already exist.
            # Await transport acknowledgement, not another generated answer.
            payload["max_tokens"] = 1
        return payload

    def _post_chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, object]] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        from codey.providers import error_classification as errors

        endpoint = f"{self.base_url}/chat/completions"
        payload = self._request_payload(messages, tools)
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(endpoint, data=data, headers=headers, method="POST")
        last_error: Exception | None = None
        for attempt in range(1, _RESPONSE_RETRIES + 2):
            started = time.perf_counter()
            phase = "error"
            response_bytes = 0
            self._notify_http_attempt(attempt, data, "request", 0, 0.0)
            try:
                with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                    raw = response.read(_CHAT_RESPONSE_MAX_BYTES + 1)
                    response_bytes = len(raw)
                if len(raw) > _CHAT_RESPONSE_MAX_BYTES:
                    raise RuntimeError(
                        f"local model at {endpoint} response exceeded "
                        f"{_CHAT_RESPONSE_MAX_BYTES} bytes"
                    )
                body = _load_response_json(raw, endpoint)
                phase = "response"
                return body
            except http.client.IncompleteRead as exc:
                phase = "retryable_error"
                response_bytes = len(exc.partial)
                last_error = _RetryableResponseError(
                    f"local model at {endpoint} returned a truncated response "
                    f"({len(exc.partial)} bytes read, {exc.expected} more expected)"
                )
            except _RetryableResponseError as exc:
                phase = "retryable_error"
                last_error = exc
            except urllib.error.HTTPError as exc:
                try:
                    try:
                        detail = exc.read(_ERROR_BODY_MAX_BYTES + 1).decode("utf-8", "replace")[:_ERROR_BODY_MAX_BYTES]
                    except Exception:
                        detail = ""
                finally:
                    with contextlib.suppress(Exception):
                        exc.close()
                kind = errors.classify_http_error(int(getattr(exc, "code", 0) or 0), detail)
                if kind == errors.ProviderErrorKind.CONTEXT_OVERFLOW:
                    raise errors.ContextOverflowError(f"local model context overflow: {detail[:400]}") from exc
                if kind == errors.ProviderErrorKind.AUTH:
                    raise RuntimeError(f"local model HTTP {exc.code}: {detail[:400]}") from exc
                message = f"local model HTTP {exc.code}: {detail[:400]}"
                if tools and _looks_like_unsupported_tools_error(detail):
                    message += (
                        " (local endpoint rejected native tools; disable them with "
                        "NATIVE_TOOLS=0 or Local model > Native tools: Off "
                        "(local-openai.json {\"native_tools_mode\":\"off\"}))"
                    )
                raise RuntimeError(message) from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                raise RuntimeError(f"could not reach local model at {self.base_url}: {exc}") from exc
            finally:
                self._notify_http_attempt(attempt, data, phase, response_bytes, time.perf_counter() - started)
        raise RuntimeError(str(last_error or f"local model at {endpoint} did not return a reply"))

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
        tools: list[dict[str, object]] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        from codey.providers import error_classification as errors

        body = self._post_chat(messages, tools, timeout=timeout)
        try:
            choice = body["choices"][0]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("local model returned no choices") from exc
        if not isinstance(choice, dict):
            raise RuntimeError("local model returned a malformed choice")
        kind = errors.classify_openai_choice(choice)
        if kind == errors.ProviderErrorKind.CONTEXT_OVERFLOW:
            raise errors.ContextOverflowError("local model context overflow (finish_reason=length)")
        if kind == errors.ProviderErrorKind.FATAL:
            raise RuntimeError(
                "local model content filtered "
                f"(finish_reason={choice.get('finish_reason')})"
            )
        message = choice.get("message")
        if not isinstance(message, dict):
            raise RuntimeError("local model returned a choice without message content")
        raw_calls = message.get("tool_calls")
        if kind == errors.ProviderErrorKind.OUTPUT_LENGTH and raw_calls:
            raise errors.OutputLengthError(
                "model output truncated while emitting tool calls; refusing partial tool execution"
            )
        out: dict[str, object] = {
            "content": message.get("content") or "",
            "_finish_reason": str(choice.get("finish_reason") or ""),
        }
        reasoning = _reasoning_text(message)
        if reasoning:
            out["reasoning_content"] = reasoning
        if kind == errors.ProviderErrorKind.OUTPUT_LENGTH:
            # Preserve a text-only length stop as a continuable assistant turn.
            # Tool calls are parsed below and malformed/truncated arguments
            # remain fail-closed; the kernel may spend one normal turn asking
            # the local provider to continue or call done.
            out["_continuable_length"] = True
        if "tool_calls" in message and not isinstance(message.get("tool_calls"), list):
            raise RuntimeError("local model returned malformed tool_calls")
        if isinstance(raw_calls, list):
            out["tool_calls"] = raw_calls
        return out

    def _complete(self, messages: list[dict[str, Any]], *, timeout: float | None = None) -> str:
        body = self._post_chat(messages, None, timeout=timeout)
        reply = _extract_reply(body)
        choices = body.get("choices")
        choice = choices[0] if isinstance(choices, list) and choices else None
        message = choice.get("message") if isinstance(choice, dict) else None
        self._response_reasoning = _reasoning_text(message)
        return reply


def _looks_like_unsupported_tools_error(detail: object) -> bool:
    text = str(detail or "").lower()
    return any(
        marker in text
        for marker in (
            "tools",
            "tool_choice",
            "function",
            "unsupported",
            "unknown parameter",
            "unrecognized",
        )
    )


def _parse_tool_calls(message: dict[str, Any]) -> tuple[list[dict[str, object]], int]:
    """Parse raw tool_calls, returning the usable calls plus a drop count.

    A call without an id, without a name, or without a JSON-object
    ``arguments`` payload can never be answered legally, so callers must
    treat any drop as a malformed turn (fail closed) rather than storing
    the raw block. Illegal arguments are never coerced to ``{}``: a
    ``done`` with ``"{bad"`` must not become a valid completion.
    A present-but-non-list ``tool_calls`` is a protocol error, never an
    empty turn: it would silently downgrade a tool turn to plain text.
    """
    if "tool_calls" not in message:
        return [], 0
    raw_calls = message.get("tool_calls")
    if raw_calls is None:
        return [], 0
    if not isinstance(raw_calls, list):
        raise RuntimeError("local model returned malformed tool_calls")
    parsed: list[dict[str, object]] = []
    dropped = 0
    seen_ids: set[str] = set()
    for item in raw_calls:
        if not isinstance(item, dict):
            dropped += 1
            continue
        call_id = item.get("id")
        function = item.get("function")
        if not isinstance(function, dict):
            dropped += 1
            continue
        name = str(function.get("name") or "")
        if type(call_id) is not str or not call_id.strip() or call_id in seen_ids or not name:
            dropped += 1
            continue
        seen_ids.add(call_id)
        raw_args = function.get("arguments")
        if isinstance(raw_args, dict):
            arguments = dict(raw_args)
        elif isinstance(raw_args, str):
            if not raw_args.strip():
                dropped += 1
                continue
            try:
                decoded = json.loads(raw_args)
            except json.JSONDecodeError:
                dropped += 1
                continue
            if not isinstance(decoded, dict):
                dropped += 1
                continue
            arguments = dict(decoded)
        else:
            dropped += 1
            continue
        parsed.append({"id": call_id, "name": name, "arguments": arguments})
    return parsed, dropped


def _store_assistant_message(message: dict[str, Any]) -> dict[str, Any]:
    stored: dict[str, object] = {"role": "assistant", "content": str(message.get("content") or "")}
    reasoning = _reasoning_text(message)
    if reasoning:
        stored["reasoning_content"] = reasoning
    raw_calls = message.get("tool_calls")
    if isinstance(raw_calls, list) and raw_calls:
        stored["tool_calls"] = raw_calls
    return stored


def _reasoning_text(message: object) -> str:
    if not isinstance(message, dict):
        return ""
    for key in ("reasoning_content", "reasoning", "thinking"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _extract_reply(body: dict[str, Any]) -> str:
    try:
        choice = body["choices"][0]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("local model returned no choices") from exc
    if not isinstance(choice, dict):
        raise RuntimeError("local model returned a malformed choice")
    from codey.providers import error_classification as errors

    kind = errors.classify_openai_choice(choice)
    if kind == errors.ProviderErrorKind.CONTEXT_OVERFLOW:
        raise errors.ContextOverflowError("local model context overflow (finish_reason=length)")
    if kind == errors.ProviderErrorKind.OUTPUT_LENGTH:
        raise errors.OutputLengthError()
    if kind == errors.ProviderErrorKind.FATAL:
        raise RuntimeError(
            "local model content filtered "
            f"(finish_reason={choice.get('finish_reason')})"
        )
    message = choice.get("message")
    if isinstance(message, dict):
        return str(message.get("content") or "")
    if isinstance(choice.get("text"), str):
        return str(choice.get("text") or "")
    raise RuntimeError("local model returned a choice without message content")


def _load_response_json(raw: bytes, endpoint: str) -> dict[str, Any]:
    text = raw.decode("utf-8", errors="replace")
    if not text.strip():
        raise _RetryableResponseError(
            f"local model at {endpoint} returned an empty response; expected OpenAI-compatible JSON"
        )
    try:
        body = json.loads(text)
    except json.JSONDecodeError as exc:
        preview = " ".join(text.split())[:_RESPONSE_PREVIEW_LIMIT]
        raise _RetryableResponseError(
            f"local model at {endpoint} returned non-JSON response: {preview}"
        ) from exc
    if not isinstance(body, dict):
        raise RuntimeError(
            f"local model at {endpoint} returned a non-object JSON response"
        )
    return body
