"""OpenAI-compatible local model provider."""

from __future__ import annotations

import http.client
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from codey.env_names import (
    LOCAL_OPENAI_API_KEY_ENV as LOCAL_API_KEY_ENV,
)
from codey.env_names import (
    LOCAL_OPENAI_BASE_URL_ENV as LOCAL_BASE_URL_ENV,
)
from codey.env_names import (
    LOCAL_OPENAI_MODEL_ENV as LOCAL_MODEL_ENV,
)
from codey.providers.local_config import CONFIG_FILE as _CONFIG_FILE
from codey.providers.local_config import (
    LocalProviderConfig,
    config_from_dict,
    resolve_effective_local_config,
    resolve_local_context_budget,
    resolve_local_native_tools,
)
from codey.providers.local_discovery import (
    DEFAULT_BASE_URL,
    LOCAL_BASE_URL_CANDIDATES,
    LocalEndpoint,
)
from codey.storage.local_store import (
    DEFAULT_STATE_HOME,
    StoreCorruption,
    backup_corrupt_file,
    read_json_strict,
    write_json_atomic,
)

DEFAULT_TIMEOUT = 180
DEFAULT_TEMPERATURE = 0.3
_RESPONSE_PREVIEW_LIMIT = 400
_RESPONSE_RETRIES = 1


class _RetryableResponseError(RuntimeError):
    pass


class LocalOpenAIProvider:
    name = "Local"
    thread_safe_send = True

    def __init__(
        self,
        base_url: str = "",
        model: str = "",
        *,
        api_key: str = "",
        timeout: float = DEFAULT_TIMEOUT,
        temperature: float = DEFAULT_TEMPERATURE,
        system_prompt: str = "",
        context_window_tokens: int | None = None,
        context_reserve_tokens: int | None = None,
        context_keep_recent_tokens: int | None = None,
    ) -> None:
        self.base_url = (base_url or os.environ.get(LOCAL_BASE_URL_ENV) or default_local_base_url()).rstrip("/")
        self.model = model or os.environ.get(LOCAL_MODEL_ENV) or "local-model"
        self.api_key = api_key or os.environ.get(LOCAL_API_KEY_ENV, "")
        self.timeout = timeout
        self.temperature = temperature
        self.system_prompt = system_prompt
        self.context_window_tokens = context_window_tokens
        self.context_reserve_tokens = context_reserve_tokens
        self.context_keep_recent_tokens = context_keep_recent_tokens
        self._messages: list[dict] = []

    @classmethod
    def connect(cls, **_kwargs) -> LocalOpenAIProvider:
        try:
            canonical = config_from_dict(load_local_config())
            endpoint = resolve_local_endpoint()
            if endpoint is None:
                return cls()
            effective = resolve_effective_local_config(canonical, endpoint=endpoint)
        except Exception:
            return cls()
        return cls(
            effective.base_url,
            effective.model or "local-model",
            api_key=effective.api_key,
            context_window_tokens=effective.context.context_window_tokens,
            context_reserve_tokens=effective.context.context_reserve_tokens,
            context_keep_recent_tokens=effective.context.context_keep_recent_tokens,
        )

    @property
    def location(self) -> str:
        return f"{self.base_url} ({self.model})"

    def new_chat(self, timeout: float | None = None) -> None:
        self._messages = []

    def send(self, text: str, timeout: float | None = None) -> str:
        if not self._messages and self.system_prompt:
            self._messages.append({"role": "system", "content": self.system_prompt})
        self._maybe_compact_messages()
        self._messages.append({"role": "user", "content": text})
        reply = self._complete(self._messages, timeout=timeout)
        self._messages.append({"role": "assistant", "content": reply})
        return reply

    def _assistant_turn_or_fail_closed(self, message: dict) -> object:
        """Build the AssistantTurn, failing closed on unanswerable tool_calls.

        Storing a raw block with missing ids would poison the local history:
        a later plain prompt would break the provider chain. Instead reset to
        a fresh chat and surface text the JSON fallback can route to repair.
        """
        from codey.providers.base import AssistantTurn, ProviderToolCall

        parsed, dropped = _parse_tool_calls(message)
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
        if not self._messages and self.system_prompt:
            self._messages.append({"role": "system", "content": self.system_prompt})
        self._maybe_compact_messages(tools=tools)
        self._messages.append({"role": "user", "content": prompt})
        message = self._complete_message(self._messages, tools=tools, timeout=timeout)
        return self._assistant_turn_or_fail_closed(message)

    def send_tool_results(
        self,
        results: list[dict[str, object]],
        tools: list[dict[str, object]] | None = None,
        timeout: float | None = None,
    ) -> object:
        for item in results:
            tool_call_id = str(item.get("tool_call_id") or "")
            if not tool_call_id:
                continue
            self._messages.append({
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": str(item.get("content") or ""),
            })
        self._maybe_compact_messages(tools=tools)
        message = self._complete_message(self._messages, tools=tools, timeout=timeout)
        return self._assistant_turn_or_fail_closed(message)

    def _context_budget(self) -> tuple[int, int, int]:
        """Instance budgets with capability fallback; never reads disk per send."""
        try:
            from codey.providers.capabilities import capability_for

            capability = capability_for("local")
            defaults = (
                int(capability.context_window_tokens),
                int(capability.context_reserve_tokens),
                int(capability.context_keep_recent_tokens),
            )
        except Exception:
            defaults = (32_768, 8_192, 12_000)
        return (
            self.context_window_tokens or defaults[0],
            self.context_reserve_tokens or defaults[1],
            self.context_keep_recent_tokens or defaults[2],
        )

    def _maybe_compact_messages(self, tools: list[dict[str, object]] | None = None) -> None:
        try:
            from codey.agents import context_compaction as compaction
        except Exception:
            return
        window, reserve, keep = self._context_budget()
        try:
            summary = compaction.compact_openai_messages_in_place(
                self._messages,
                tools=tools,
                context_window_tokens=window,
                reserve_tokens=reserve,
                keep_recent_tokens=keep,
            )
            _ = summary
        except Exception:
            return

    def close(self) -> None:
        self._messages = []

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
                    raw = response.read()
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
                    detail = exc.read().decode("utf-8", "replace")[:2000]
                except Exception:
                    detail = ""
                kind = errors.classify_http_error(int(getattr(exc, "code", 0) or 0), detail)
                if kind == errors.ProviderErrorKind.CONTEXT_OVERFLOW:
                    raise errors.ContextOverflowError(f"local model context overflow: {detail[:400]}") from exc
                if kind == errors.ProviderErrorKind.AUTH:
                    raise RuntimeError(f"local model HTTP {exc.code}: {detail[:400]}") from exc
                message = f"local model HTTP {exc.code}: {detail[:400]}"
                if tools and _looks_like_unsupported_tools_error(detail):
                    message += (
                        " (local endpoint rejected native tools; disable them with "
                        "NATIVE_TOOLS=0 or local-openai.json {\"native_tools\": false})"
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
        message = choice.get("message")
        if not isinstance(message, dict):
            raise RuntimeError("local model returned a choice without message content")
        out: dict[str, object] = {
            "content": message.get("content") or "",
            "_finish_reason": str(choice.get("finish_reason") or ""),
        }
        raw_calls = message.get("tool_calls")
        if isinstance(raw_calls, list):
            out["tool_calls"] = raw_calls
        return out

    def _complete(self, messages: list[dict], *, timeout: float | None = None) -> str:
        body = self._post_chat(messages, None, timeout=timeout)
        return _extract_reply(body)


def default_local_base_url() -> str:
    """First reachable candidate (short-circuit); env/remembered first."""
    configured = os.environ.get(LOCAL_BASE_URL_ENV, "").strip()
    if configured:
        return configured
    remembered = load_local_config().get("base_url")
    if remembered:
        return str(remembered)
    for candidate in LOCAL_BASE_URL_CANDIDATES:
        if local_endpoint_available(candidate):
            return candidate
    return DEFAULT_BASE_URL


def local_endpoint_available(base_url: str = "") -> bool:
    config = load_local_config()
    remembered_key = str(config.get("api_key") or "")
    if base_url:
        return probe_local_endpoint(base_url, api_key=remembered_key) is not None
    configured = os.environ.get(LOCAL_BASE_URL_ENV, "").strip()
    if configured:
        return probe_local_endpoint(configured, api_key=os.environ.get(LOCAL_API_KEY_ENV, "")) is not None
    endpoint = resolve_local_endpoint()
    return endpoint is not None


def probe_local_endpoint(
    base_url: str,
    *,
    api_key: str = "",
    timeout: float = 1.5,
) -> LocalEndpoint | None:
    """Facade over discovery (keeps the historical Optional return)."""
    from codey.providers import local_discovery as discovery

    return discovery.probe_local_endpoint(base_url, api_key=api_key, timeout=timeout)


def probe_local_endpoint_detail(
    base_url: str,
    *,
    api_key: str = "",
    timeout: float = 1.5,
) -> tuple[LocalEndpoint | None, str]:
    """Facade over discovery (keeps the historical detail return)."""
    from codey.providers import local_discovery as discovery

    return discovery.probe_local_endpoint_detail(base_url, api_key=api_key, timeout=timeout)


def detect_local_endpoints(*, api_key: str = "") -> list[LocalEndpoint]:
    """First-hit short-circuit scan (mock-friendly); full parallel scan lives in discovery."""
    found: list[LocalEndpoint] = []
    seen: set[str] = set()
    for candidate in LOCAL_BASE_URL_CANDIDATES:
        normalized = candidate.rstrip("/")
        if normalized in seen:
            continue
        seen.add(normalized)
        endpoint = probe_local_endpoint(normalized, api_key=api_key)
        if endpoint is not None:
            found.append(endpoint)
    return found


def resolve_local_endpoint() -> LocalEndpoint | None:
    """Remembered endpoint first, else the first reachable candidate."""
    config = load_local_config()
    remembered = str(config.get("base_url") or "").strip()
    api_key = str(config.get("api_key") or "")
    if remembered:
        endpoint = probe_local_endpoint(remembered, api_key=api_key)
        if endpoint is not None:
            model = str(config.get("model") or endpoint.default_model or "")
            models = ((model,) if model else ()) + tuple(m for m in endpoint.models if m != model)
            return LocalEndpoint(endpoint.base_url, models)
    detected = detect_local_endpoints(api_key=os.environ.get(LOCAL_API_KEY_ENV, ""))
    return detected[0] if detected else None


def load_local_config() -> dict:
    """Legacy dict view over the canonical config (callers migrate to local_config)."""
    from codey.providers import local_config as canonical

    try:
        raw = read_json_strict(_config_path()) or {}
    except StoreCorruption:
        backup_corrupt_file(_config_path())
        raw = {}
    except Exception:
        return {}
    try:
        config = canonical.config_from_dict(raw)
    except Exception:
        return {}
    view: dict[str, object] = {
        "base_url": config.base_url,
        "model": config.model,
        "api_key": config.api_key,
    }
    if config.native_tools_mode == canonical.NATIVE_TOOLS_ON:
        view["native_tools"] = True
    elif config.native_tools_mode == canonical.NATIVE_TOOLS_OFF:
        view["native_tools"] = False
    if config.context is not None:
        view["context_window_tokens"] = config.context.context_window_tokens
        view["context_reserve_tokens"] = config.context.context_reserve_tokens
        view["context_keep_recent_tokens"] = config.context.context_keep_recent_tokens
    return view


def local_native_tools_enabled() -> bool:
    """Facade over the canonical resolver (env > stored mode > default)."""
    try:
        return resolve_local_native_tools(config_from_dict(load_local_config()))
    except Exception:
        return True


def resolve_local_context_budgets() -> dict[str, int]:
    """Legacy dict view over the canonical budget resolver."""
    try:
        budget = resolve_local_context_budget(config_from_dict(load_local_config()))
    except Exception:
        return {
            "context_window_tokens": 32_768,
            "context_reserve_tokens": 8_192,
            "context_keep_recent_tokens": 12_000,
        }
    return {
        "context_window_tokens": budget.context_window_tokens,
        "context_reserve_tokens": budget.context_reserve_tokens,
        "context_keep_recent_tokens": budget.context_keep_recent_tokens,
    }


def save_local_config(
    base_url: str,
    model: str = "",
    api_key: str | None = None,
    *,
    native_tools: bool | None = None,
    native_tools_mode: str | None = None,
    context_window_tokens: int | None = None,
    context_reserve_tokens: int | None = None,
    context_keep_recent_tokens: int | None = None,
) -> None:
    """Legacy writer; persists the canonical schema-2 shape at the facade path."""
    from codey.providers import local_config as canonical

    try:
        previous = canonical.config_from_dict(load_local_config())
    except Exception:
        previous = LocalProviderConfig()
    stored_key = previous.api_key if api_key is None else str(api_key or "").strip()
    mode = previous.native_tools_mode
    if native_tools_mode is not None:
        parsed_mode = canonical.parse_native_tools_mode(native_tools_mode)
        if parsed_mode is not None:
            mode = parsed_mode
    elif native_tools is not None:
        mode = canonical.NATIVE_TOOLS_ON if native_tools else canonical.NATIVE_TOOLS_OFF
    context = previous.context
    if any(v is not None for v in (context_window_tokens, context_reserve_tokens, context_keep_recent_tokens)):
        window = context_window_tokens if context_window_tokens is not None else (
            context.context_window_tokens if context else 0
        )
        try:
            window_int = int(window)
        except (TypeError, ValueError):
            window_int = 0
        if window_int > 0:
            preset = canonical.context_budget_for_window(window_int)
            context = canonical.LocalContextBudget(
                window_int,
                int(context_reserve_tokens) if context_reserve_tokens is not None else preset.context_reserve_tokens,
                int(context_keep_recent_tokens) if context_keep_recent_tokens is not None else preset.context_keep_recent_tokens,
                source="config",
            )
            if canonical.validate_context_budget(context):
                context = previous.context
    # Single write through the facade path (tests patch this module's path).
    write_json_atomic(_config_path(), canonical.config_to_payload(
        canonical.LocalProviderConfig(
            base_url=(base_url or "").strip(),
            model=(model or "").strip(),
            api_key=stored_key,
            native_tools_mode=mode,
            context=context,
        )
    ), mode=0o600)


def local_config_payload() -> dict:
    """Facade over the canonical bootstrap payload."""
    from codey.providers import local_config as canonical

    try:
        return canonical.local_bootstrap_payload()
    except Exception:
        return {"connected": False}


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


def _config_path() -> Path:
    return DEFAULT_STATE_HOME / _CONFIG_FILE


def _parse_tool_calls(message: dict) -> tuple[list[dict[str, object]], int]:
    """Parse raw tool_calls, returning the usable calls plus a drop count.

    A call without an id or a name can never be answered legally, so callers
    must treat any drop as a malformed turn (fail closed) rather than storing
    the raw block. Bad argument payloads are NOT dropped here: they keep
    their id and flow into validation, which answers them with an error.
    """
    raw_calls = message.get("tool_calls")
    if not isinstance(raw_calls, list):
        return [], 0
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
        raw_args = function.get("arguments")
        if isinstance(raw_args, dict):
            arguments = dict(raw_args)
        elif isinstance(raw_args, str):
            try:
                decoded = json.loads(raw_args) if raw_args.strip() else {}
            except json.JSONDecodeError:
                decoded = {}
            arguments = dict(decoded) if isinstance(decoded, dict) else {}
        else:
            arguments = {}
        if not call_id or not name:
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
