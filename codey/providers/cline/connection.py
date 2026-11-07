"""Removable Cline factories; all generation uses Codey's normal ApiProvider."""
from __future__ import annotations

from functools import partial
from typing import Any

from codey.providers.api_provider import ApiProvider
from codey.providers.api_request_policy import RequestContext
from codey.providers.cline.capabilities import Capability
from codey.providers.cline.codec import CODEC_REVISION, ChatCodec
from codey.providers.cline.identity import CONNECTION_REVISION, INFERENCE_URL, client_headers, inference_api_key
from codey.providers.cline.reasoning import configure
from codey.providers.cline.request_policy import ClineRequestPolicy, safe_error_detail
from codey.providers.cline.runtime import runtime, shutdown
from codey.providers.cline.stream import ClineAccumulator
from codey.providers.cline.usage import parse_usage
from codey.providers.connection_controls import ConnectionActionError
from codey.runtime.core.api_selection import ApiRunSelection
from codey.runtime.core.reasoning import compile_selection


def authentication_payload() -> dict[str, Any]:
    return runtime().auth.snapshot()


def connection_action(action: str, attempt_id: str | None = None) -> dict[str, Any]:
    return runtime().action(action, attempt_id)


def shutdown_controls() -> None:
    shutdown()


def status_key() -> str:
    auth = authentication_payload()
    record = runtime().auth.store.load() if auth["state"] == "ready" else None
    return f"{CONNECTION_REVISION}:{record.account_epoch if record else ''}:{auth['state']}:{runtime().blocked}"


def cached_model_payload() -> dict[str, Any]:
    return runtime().payload()


def refresh_models(*, force: bool = True) -> dict[str, Any]:
    runtime().catalog.refresh(force=force)
    return cached_model_payload()


def model_payload() -> dict[str, Any]:
    return refresh_models(force=False)


def probe_status(request: object) -> dict[str, Any]:
    payload = cached_model_payload()
    return {"state": payload["state"], "stale": payload["stale"], "models": payload["models"],
            "source": "account_catalog", "payload": payload, "error": payload["error"]}


def _revision(epoch: str, capability: Capability) -> str:
    return f"{CONNECTION_REVISION}:{epoch}:{capability.revision}"


def capture_selection(selection: object = None) -> ApiRunSelection:
    if not isinstance(selection, dict) or not isinstance(selection.get("model"), str) or not selection["model"].strip():
        raise ValueError("Cline selection requires a model.")
    if set(selection) - {"model", "connection_id", "base_url", "effort", "thinking"} or selection.get("base_url"):
        raise ValueError("Cline endpoint and authentication cannot be overridden.")
    resource = runtime()
    record = resource.auth.store.load()
    if record is None:
        raise ConnectionActionError("reauth_required", "Sign in to Cline first.", 401)
    capability = resource.catalog.require(selection["model"])
    resource.auth._check_record(record, record.account_epoch)
    effort, thinking = compile_selection(efforts=capability.efforts, toggle=capability.toggle,
                                        effort=selection.get("effort"), thinking=selection.get("thinking"))
    reserve = min(8192, capability.context // 4)
    return ApiRunSelection("cline", _revision(record.account_epoch, capability), selection["model"],
        "openai-completions", capability.tools, capability.context, reserve, min(12000, capability.context - reserve),
        reasoning_effort=effort, thinking_enabled=thinking, stream=True, output_tokens=min(capability.output, reserve),
        budget_source="model_catalog", input_limit_tokens=capability.input_limit, supports_images=capability.images)


def validate_selection(selection: ApiRunSelection) -> None:
    from dataclasses import replace

    resource = runtime()
    record = resource.auth.store.load()
    if record is None:
        raise ConnectionActionError("reauth_required", "Sign in to Cline first.", 401)
    capability = resource.catalog.require(selection.model_id)
    if (selection.connection_id != "cline" or selection.protocol != "openai-completions"
            or selection.supports_images and not capability.images
            or selection.connection_revision not in {
                _revision(record.account_epoch, capability),
                _revision(record.account_epoch, replace(capability, images=False)) if not selection.supports_images else "",
            }):
        raise ValueError("Cline account or model contract changed; select the model again.")
    resource.auth._check_record(record, record.account_epoch)
    compile_selection(efforts=capability.efforts, toggle=capability.toggle,
                      effort=selection.reasoning_effort, thinking=selection.thinking_enabled)


def open_selection(selection: ApiRunSelection) -> ApiProvider:
    validate_selection(selection)
    resource = runtime()
    epoch = selection.connection_revision.split(":")[1]
    capability = resource.catalog.require(selection.model_id)
    policy = ClineRequestPolicy(resource, epoch, selection.model_id, capability.revision)

    def headers(context: RequestContext) -> dict[str, str]:
        if context.base_url != INFERENCE_URL:
            raise ValueError("Cline credentials cannot be sent to another endpoint.")
        record = resource.auth.credentials(context.deadline, context.cancelled, epoch)
        policy.remember_version(context.logical_id, record.token_version)
        return {**client_headers(context.logical_id), "Authorization": "Bearer " + inference_api_key(record.access_token)}

    provider = ApiProvider(INFERENCE_URL, selection.model_id, supports_images=selection.supports_images, api_key="", api_protocol=selection.protocol,
        context_window_tokens=selection.context_window_tokens, context_reserve_tokens=selection.context_reserve_tokens,
        context_keep_recent_tokens=selection.context_keep_recent_tokens, native_tools=selection.native_tools,
        reasoning_effort=selection.reasoning_effort, thinking_enabled=selection.thinking_enabled,
        stream=selection.stream, tool_choice=selection.tool_choice, output_tokens=selection.output_tokens,
        budget_source=selection.budget_source, input_limit_tokens=selection.input_limit_tokens,
        request_context_headers=headers, request_policy=policy, configure_request=configure,
        codec=ChatCodec(), codec_revision=CODEC_REVISION, connection_revision=selection.connection_revision,
        usage_parser=partial(parse_usage, resource), transport_options={"chat_accumulator_factory": ClineAccumulator,
            "body_idle_timeout": 60.0, "error_detail_filter": safe_error_detail})
    provider.name = "Cline"
    return provider
