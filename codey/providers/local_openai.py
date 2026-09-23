"""OpenAI-compatible local model provider."""

from __future__ import annotations

import http.client
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from codey.env_names import (
    LOCAL_OPENAI_API_KEY_ENV as LOCAL_API_KEY_ENV,
)
from codey.env_names import (
    LOCAL_OPENAI_BASE_URL_ENV as LOCAL_BASE_URL_ENV,
)
from codey.env_names import (
    LOCAL_OPENAI_CONTEXT_KEEP_ENV,
    LOCAL_OPENAI_CONTEXT_RESERVE_ENV,
    LOCAL_OPENAI_CONTEXT_WINDOW_ENV,
    NATIVE_TOOLS_ENV,
)
from codey.env_names import (
    LOCAL_OPENAI_MODEL_ENV as LOCAL_MODEL_ENV,
)
from codey.storage.local_store import (
    DEFAULT_STATE_HOME,
    StoreCorruption,
    backup_corrupt_file,
    read_json_strict,
    write_json_atomic,
)

DEFAULT_BASE_URL = "http://127.0.0.1:1234/v1"
LOCAL_BASE_URL_CANDIDATES = (
    "http://127.0.0.1:1234/v1",
    "http://127.0.0.1:11434/v1",
    "http://127.0.0.1:8080/v1",
)
DEFAULT_TIMEOUT = 180
DEFAULT_TEMPERATURE = 0.3
_RESPONSE_PREVIEW_LIMIT = 400
_RESPONSE_RETRIES = 1
_CONFIG_FILE = "local-openai.json"


@dataclass(frozen=True)
class LocalEndpoint:
    base_url: str
    models: tuple[str, ...] = ()

    @property
    def default_model(self) -> str:
        return self.models[0] if self.models else ""


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
        endpoint = resolve_local_endpoint()
        if endpoint is None:
            return cls()
        config = load_local_config()
        model = str(config.get("model") or endpoint.default_model or "")
        api_key = str(config.get("api_key") or "")
        return cls(endpoint.base_url, model, api_key=api_key, **resolve_local_context_budgets())

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

    def _maybe_compact_messages(self, tools: list[dict[str, object]] | None = None) -> None:
        try:
            from codey.agents import context_compaction as compaction

            budgets = resolve_local_context_budgets()
        except Exception:
            return
        try:
            summary = compaction.compact_openai_messages_in_place(
                self._messages,
                tools=tools,
                context_window_tokens=(
                    self.context_window_tokens if self.context_window_tokens else budgets["context_window_tokens"]
                ),
                reserve_tokens=(
                    self.context_reserve_tokens if self.context_reserve_tokens else budgets["context_reserve_tokens"]
                ),
                keep_recent_tokens=(
                    self.context_keep_recent_tokens
                    if self.context_keep_recent_tokens
                    else budgets["context_keep_recent_tokens"]
                ),
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
    """Return an endpoint only for a valid OpenAI-compatible /models reply.

    Anything else (unreachable/auth/invalid payload) is None: callers treat
    "we got bytes but not /models" the same as "nothing there". Use
    probe_local_endpoint_detail when the reason matters for an error message.
    """
    endpoint, reason = probe_local_endpoint_detail(
        base_url, api_key=api_key, timeout=timeout
    )
    return endpoint if reason == "ok" else None


def probe_local_endpoint_detail(
    base_url: str,
    *,
    api_key: str = "",
    timeout: float = 1.5,
) -> tuple[LocalEndpoint | None, str]:
    """Probe /models and report why it failed without raising.

    Returns (endpoint, reason) where reason is one of
    ok/unreachable/auth/invalid_json. The thin probe_local_endpoint
    wrapper above keeps the historical Optional return for callers.
    """
    url = (base_url or DEFAULT_BASE_URL).rstrip("/")
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(f"{url}/models", headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return None, "auth"
        return None, "unreachable"
    except Exception:
        return None, "unreachable"
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, "invalid_json"
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list):
        return None, "invalid_json"
    models = tuple(
        str(item.get("id"))
        for item in data
        if isinstance(item, dict) and item.get("id")
    )
    return LocalEndpoint(url, models), "ok"


def detect_local_endpoints(*, api_key: str = "") -> list[LocalEndpoint]:
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
    path = _config_path()
    try:
        return read_json_strict(path) or {}
    except StoreCorruption:
        backup_corrupt_file(path)
        return {}


def _parse_bool_flag(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "y", "on"}:
            return True
        if text in {"0", "false", "no", "n", "off"}:
            return False
    return None


def local_native_tools_enabled() -> bool:
    """Whether the local provider should use native function calls.

    Explicit opt-out only, no auto-fallback: ``NATIVE_TOOLS=0`` or
    ``local-openai.json`` ``{"native_tools": false}`` disables it.
    Unset means the capability default (on for cold start).
    """
    env_flag = _parse_bool_flag(os.environ.get(NATIVE_TOOLS_ENV, "").strip())
    if env_flag is not None:
        return env_flag
    try:
        config_flag = _parse_bool_flag(load_local_config().get("native_tools"))
    except Exception:
        config_flag = None
    if config_flag is not None:
        return config_flag
    try:
        from codey.providers.capabilities import capability_for

        return bool(capability_for("local").native_tools_default)
    except Exception:
        return True


def _parse_positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, float) and value.is_integer() and value > 0:
        return int(value)
    if isinstance(value, str):
        text = value.strip().replace("_", "").replace(",", "")
        if text.isdigit() and int(text) > 0:
            return int(text)
    return None


def resolve_local_context_budgets() -> dict[str, int]:
    """Resolve local context budgets from env, then config, then capability.

    Env wins (``LOCAL_OPENAI_CONTEXT_WINDOW/RESERVE/KEEP``), then
    ``local-openai.json`` ``context_*`` fields, then the ``local``
    capability defaults. Invalid values fail open to the capability
    defaults; ``window > reserve > 0`` and ``keep > 0`` are enforced.
    """
    try:
        from codey.providers.capabilities import capability_for

        capability = capability_for("local")
        defaults = {
            "context_window_tokens": int(capability.context_window_tokens),
            "context_reserve_tokens": int(capability.context_reserve_tokens),
            "context_keep_recent_tokens": int(capability.context_keep_recent_tokens),
        }
    except Exception:
        defaults = {
            "context_window_tokens": 32_768,
            "context_reserve_tokens": 8_192,
            "context_keep_recent_tokens": 12_000,
        }
    try:
        config = load_local_config()
    except Exception:
        config = {}
    resolved: dict[str, int] = {}
    sources = (
        ("context_window_tokens", LOCAL_OPENAI_CONTEXT_WINDOW_ENV, "context_window_tokens"),
        ("context_reserve_tokens", LOCAL_OPENAI_CONTEXT_RESERVE_ENV, "context_reserve_tokens"),
        ("context_keep_recent_tokens", LOCAL_OPENAI_CONTEXT_KEEP_ENV, "context_keep_recent_tokens"),
    )
    for key, env_name, config_key in sources:
        env_value = _parse_positive_int(os.environ.get(env_name, "").strip())
        if env_value is not None:
            resolved[key] = env_value
            continue
        config_value = _parse_positive_int(config.get(config_key))
        if config_value is not None:
            resolved[key] = config_value
            continue
        resolved[key] = int(defaults[key])
    window = int(resolved["context_window_tokens"])
    reserve = int(resolved["context_reserve_tokens"])
    keep = int(resolved["context_keep_recent_tokens"])
    if not (window > reserve > 0 and keep > 0):
        return {key: int(defaults[key]) for key in resolved}
    if keep > window:
        resolved["context_keep_recent_tokens"] = int(defaults["context_keep_recent_tokens"])
    return resolved


def save_local_config(
    base_url: str,
    model: str = "",
    api_key: str | None = None,
    *,
    native_tools: bool | None = None,
    context_window_tokens: int | None = None,
    context_reserve_tokens: int | None = None,
    context_keep_recent_tokens: int | None = None,
) -> None:
    previous = load_local_config()
    stored_key = str(previous.get("api_key") or "").strip() if api_key is None else str(api_key or "").strip()
    payload: dict[str, object] = {
        "base_url": (base_url or "").strip().rstrip("/"),
        "model": (model or "").strip(),
        "api_key": stored_key,
    }
    # Preserve explicit local tuning across base_url/model saves; an explicit
    # kwarg overrides, otherwise the previous value survives.
    if native_tools is None:
        if "native_tools" in previous:
            payload["native_tools"] = previous["native_tools"]
    elif isinstance(native_tools, bool):
        payload["native_tools"] = native_tools
    for key, value in (
        ("context_window_tokens", context_window_tokens),
        ("context_reserve_tokens", context_reserve_tokens),
        ("context_keep_recent_tokens", context_keep_recent_tokens),
    ):
        if value is None:
            if key in previous:
                payload[key] = previous[key]
        else:
            payload[key] = int(value)
    write_json_atomic(_config_path(), payload, mode=0o600)


def local_config_payload() -> dict:
    config = load_local_config()
    endpoint = resolve_local_endpoint()
    api_key = str(config.get("api_key") or "")
    candidates = detect_local_endpoints(api_key=api_key) if endpoint is None else []
    model = ""
    if endpoint is not None:
        model = endpoint.default_model
    model = str(config.get("model") or model or "")
    try:
        budgets = resolve_local_context_budgets()
    except Exception:
        budgets = {}
    return {
        "connected": endpoint is not None,
        "base_url": (endpoint.base_url if endpoint is not None else str(config.get("base_url") or "")),
        "model": model,
        "models": list(endpoint.models if endpoint is not None else ()),
        "candidates": [item.base_url for item in candidates],
        "has_api_key": bool(config.get("api_key")),
        "native_tools": local_native_tools_enabled(),
        "context_window_tokens": budgets.get("context_window_tokens"),
        "context_reserve_tokens": budgets.get("context_reserve_tokens"),
        "context_keep_recent_tokens": budgets.get("context_keep_recent_tokens"),
    }


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
