"""OpenAI-compatible local model provider."""

from __future__ import annotations

import contextlib
import http.client
import json
import threading
import urllib.error
import urllib.request

from codey.providers import local_config as _local_config
from codey.providers import local_discovery as _local_discovery

DEFAULT_TIMEOUT = 180
DEFAULT_TEMPERATURE = 0.3
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
        self._messages: list[dict] = []
        self._state_lock = threading.RLock()
        self._send_lock = threading.Lock()
        self._generation = 0

    @classmethod
    def connect(cls) -> LocalOpenAIProvider:
        """Connect to the single selected target (address + model + key).

        Offline is a hard error for preflight failover. An explicit
        address never falls back to another service, and probing uses the
        same key the provider sends with.
        """
        config = _local_config.load_local_config()
        selection = _local_config.select_local_target(config)
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
        )

    @property
    def location(self) -> str:
        return f"{self.base_url} ({self.model})"

    def new_chat(self, timeout: float | None = None) -> None:
        with self._state_lock:
            self._generation += 1
            self._messages = []

    def close(self) -> None:
        with self._state_lock:
            self._generation += 1
            self._messages = []

    def abandon_inflight(self) -> None:
        """Invalidate a still-running send so its late reply skips history."""
        with self._state_lock:
            self._generation += 1
            self._messages = []

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
            reply = self._complete(candidate, timeout=timeout)
            with self._state_lock:
                # Commit only on a usable reply: an HTTP failure leaves the
                # history untouched so the next send does not resend a stale
                # user message. A stale generation skips history entirely.
                if generation == self._generation:
                    self._messages = candidate
                    self._messages.append({"role": "assistant", "content": reply})
            return reply
        finally:
            self._send_lock.release()

    def _assistant_turn_or_fail_closed(
        self,
        candidate: list[dict],
        message: dict,
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

        parsed, dropped = _parse_tool_calls(message)
        if generation is not None and generation != self._generation:
            text = str(message.get("content") or "")
            return AssistantTurn(
                text=text,
                tool_calls=tuple(
                    ProviderToolCall(id=str(call["id"]), name=str(call["name"]), arguments=dict(call["arguments"]))
                    for call in parsed
                ) if not dropped else (),
                raw={"finish_reason": str(message.get("_finish_reason") or ""), "stale_generation": True},
            )
        if dropped:
            self._messages = (
                [{"role": "system", "content": self.system_prompt}] if self.system_prompt else []
            )
            text = str(message.get("content") or "")
            if not text:
                text = f"ERROR: local model returned {dropped} malformed tool call(s) without ids"
            return AssistantTurn(
                text=text,
                tool_calls=(),
                raw={"finish_reason": str(message.get("_finish_reason") or ""), "malformed_dropped": dropped},
            )
        self._messages = candidate
        self._messages.append(_store_assistant_message(message))
        return AssistantTurn(
            text=str(message.get("content") or ""),
            tool_calls=tuple(
                ProviderToolCall(id=str(call["id"]), name=str(call["name"]), arguments=dict(call["arguments"]))
                for call in parsed
            ),
            raw={"finish_reason": str(message.get("_finish_reason") or "")},
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
        pending: list[dict] = []
        for item in results:
            tool_call_id = str(item.get("tool_call_id") or "")
            if not tool_call_id:
                continue
            pending.append({
                "role": "tool",
                "tool_call_id": tool_call_id,
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
        pending_messages: list[dict],
        tools: list[dict[str, object]] | None = None,
    ) -> list[dict]:
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

        candidate: list[dict] = [dict(message) for message in self._messages]
        if not candidate and self.system_prompt:
            candidate.append({"role": "system", "content": self.system_prompt})
        for item in pending_messages:
            candidate.append(dict(item))
        window, reserve, keep = self._context_budget()
        from codey.agents import context_compaction as compaction

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

    def _post_chat(
        self,
        messages: list[dict],
        tools: list[dict[str, object]] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict:
        from codey.providers import error_classification as errors

        endpoint = f"{self.base_url}/chat/completions"
        payload: dict[str, object] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(endpoint, data=data, headers=headers, method="POST")
        last_error: Exception | None = None
        for _attempt in range(_RESPONSE_RETRIES + 1):
            try:
                with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                    raw = response.read(_CHAT_RESPONSE_MAX_BYTES + 1)
                if len(raw) > _CHAT_RESPONSE_MAX_BYTES:
                    raise RuntimeError(
                        f"local model at {endpoint} response exceeded "
                        f"{_CHAT_RESPONSE_MAX_BYTES} bytes"
                    )
                body = _load_response_json(raw, endpoint)
                return body
            except http.client.IncompleteRead as exc:
                last_error = _RetryableResponseError(
                    f"local model at {endpoint} returned a truncated response "
                    f"({len(exc.partial)} bytes read, {exc.expected} more expected)"
                )
            except _RetryableResponseError as exc:
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
        raise RuntimeError(str(last_error or f"local model at {endpoint} did not return a reply"))

    def _complete_message(
        self,
        messages: list[dict],
        tools: list[dict[str, object]] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict:
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
        if kind == errors.ProviderErrorKind.OUTPUT_LENGTH:
            raise errors.OutputLengthError()
        if kind == errors.ProviderErrorKind.FATAL:
            raise RuntimeError(
                "local model content filtered "
                f"(finish_reason={choice.get('finish_reason')})"
            )
        message = choice.get("message")
        if not isinstance(message, dict):
            raise RuntimeError("local model returned a choice without message content")
        out: dict[str, object] = {
            "content": message.get("content") or "",
            "_finish_reason": str(choice.get("finish_reason") or ""),
        }
        if "tool_calls" in message and not isinstance(message.get("tool_calls"), list):
            raise RuntimeError("local model returned malformed tool_calls")
        raw_calls = message.get("tool_calls")
        if isinstance(raw_calls, list):
            out["tool_calls"] = raw_calls
        return out

    def _complete(self, messages: list[dict], *, timeout: float | None = None) -> str:
        body = self._post_chat(messages, None, timeout=timeout)
        return _extract_reply(body)


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


def _parse_tool_calls(message: dict) -> tuple[list[dict[str, object]], int]:
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
    for item in raw_calls:
        if not isinstance(item, dict):
            dropped += 1
            continue
        call_id = str(item.get("id") or "")
        function = item.get("function")
        if not isinstance(function, dict):
            dropped += 1
            continue
        name = str(function.get("name") or "")
        if not call_id or not name:
            dropped += 1
            continue
        raw_args = function.get("arguments")
        if isinstance(raw_args, dict):
            arguments = dict(raw_args)
        elif isinstance(raw_args, str):
            if not raw_args.strip():
                arguments = {}
            else:
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


def _store_assistant_message(message: dict) -> dict:
    stored: dict[str, object] = {"role": "assistant", "content": str(message.get("content") or "")}
    raw_calls = message.get("tool_calls")
    if isinstance(raw_calls, list) and raw_calls:
        stored["tool_calls"] = raw_calls
    return stored


def _extract_reply(body: dict) -> str:
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


def _load_response_json(raw: bytes, endpoint: str) -> dict:
    text = raw.decode("utf-8", errors="replace")
    if not text.strip():
        raise _RetryableResponseError(
            f"local model at {endpoint} returned an empty response; expected OpenAI-compatible JSON"
        )
    try:
        body = json.loads(text)
    except json.JSONDecodeError as exc:
        preview = " ".join(text.split())[:_RESPONSE_PREVIEW_LIMIT]
        raise RuntimeError(
            f"local model at {endpoint} returned non-JSON response: {preview}"
        ) from exc
    if not isinstance(body, dict):
        raise RuntimeError(
            f"local model at {endpoint} returned a non-object JSON response"
        )
    return body
