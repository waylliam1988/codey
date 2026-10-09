"""Resolve a Local connection once before entering the shared API runtime."""
from __future__ import annotations

from codey.providers import local_config as _local_config
from codey.providers import local_discovery as _local_discovery
from codey.providers.api_provider import ApiProvider
from codey.providers.local_response_codec import normalize_local_reply
from codey.providers.local_usage import configure_usage, parser_for
from codey.runtime.core.api_selection import ApiRunSelection


def connect_local( *, config: _local_config.LocalProviderConfig | None = None,
            verify_thinking: bool = False) -> ApiProvider:
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
    from codey.providers.local_tokens import admitted_counter, counter_for

    budget = effective.context
    counter, window = admitted_counter(effective.base_url, effective.api_key, config.api_protocol, budget.context_window_tokens)
    if window <= budget.context_reserve_tokens:
        raise ValueError("Local running context cannot accommodate the configured output reserve")
    return ApiProvider(
        effective.base_url,
        effective.model,
        api_key=effective.api_key,
        context_window_tokens=window,
        context_reserve_tokens=effective.context.context_reserve_tokens,
        context_keep_recent_tokens=min(budget.context_keep_recent_tokens, window - budget.context_reserve_tokens),
        output_tokens=budget.context_reserve_tokens,
        thinking_enabled=config.thinking_enabled if selection.base_url == config.base_url else None,
        reasoning_effort=config.reasoning_effort if selection.base_url == config.base_url else None,
        api_protocol=config.api_protocol,
        native_tools=effective.native_tools,
        text_decoder=normalize_local_reply,
        usage_parser=parser_for(config.api_protocol), configure_request=configure_usage,
        request_counter=counter_for(counter, effective.base_url, effective.model, effective.api_key, window),
        budget_source="server_and_configuration" if counter == "koboldcpp" else "configuration",
    )


def capture_selection(selection: object = None) -> ApiRunSelection:
    from dataclasses import replace
    from uuid import uuid4

    from codey.providers.local_selection import capture_local_run_config

    saved = _local_config.load_local_config()
    if not saved.connection_revision:
        saved = replace(saved, connection_revision=uuid4().hex)
        _local_config.save_local_config(saved)
    if selection is None:
        target = _local_config.select_local_target(saved)
        if not target.base_url:
            endpoint = _local_discovery.resolve_local_endpoint(model=target.model, api_key=target.api_key)
            if endpoint is None:
                raise ValueError("Local connection is unavailable")
            target = _local_config.LocalTargetSelection(endpoint.base_url, endpoint.default_model, target.api_key)
        selection = {"base_url": target.base_url, "model": target.model}
    config = capture_local_run_config(selection)
    budget = _local_config.resolve_local_context_budget(config)
    from codey.providers.local_tokens import admitted_counter

    counter, window = admitted_counter(config.base_url, config.api_key, config.api_protocol, budget.context_window_tokens)
    if window <= budget.context_reserve_tokens:
        raise ValueError("Local running context cannot accommodate the configured output reserve")
    return ApiRunSelection("local", _target_revision(config), config.model, config.api_protocol,
                           _local_config.resolve_local_native_tools(config), window,
                           budget.context_reserve_tokens, min(budget.context_keep_recent_tokens, window - budget.context_reserve_tokens),
                           config.thinking_enabled, config.reasoning_effort,
                           output_tokens=budget.context_reserve_tokens, token_counter=counter,
                           budget_source="server_and_configuration" if counter == "koboldcpp" else "configuration")


def _target_revision(config: _local_config.LocalProviderConfig) -> str:
    import hashlib

    # Scope environment targets as well as saved targets without retaining keys.
    return hashlib.sha256((config.connection_revision + "\0" + config.base_url + "\0" + config.api_key).encode()).hexdigest()


def config_for_selection(selection: ApiRunSelection) -> _local_config.LocalProviderConfig:
    from dataclasses import replace

    saved = _local_config.load_local_config()
    target = _local_config.select_local_target(saved)
    current = replace(saved, base_url=target.base_url, api_key=target.api_key)
    if not target.base_url:
        endpoint = _local_discovery.resolve_local_endpoint(model=selection.model_id, api_key=target.api_key)
        if endpoint is not None:
            current = replace(current, base_url=endpoint.base_url)
    if _target_revision(current) != selection.connection_revision:
        raise ValueError("original Local connection or credential is unavailable; restore the connection before resuming")
    return replace(current, model=selection.model_id, api_protocol=selection.protocol,
                   native_tools_mode="on" if selection.native_tools else "off",
                   context=_local_config.LocalContextBudget(selection.context_window_tokens, selection.context_reserve_tokens,
                                                          selection.context_keep_recent_tokens),
                   thinking_enabled=selection.thinking_enabled, reasoning_effort=selection.reasoning_effort)


def open_selection(selection: ApiRunSelection) -> ApiProvider:
    from codey.providers.local_tokens import counter_for

    config = config_for_selection(selection)
    # The admitted budgets/mode cannot be overwritten by environment changes.
    provider = ApiProvider(config.base_url, selection.model_id, api_key=config.api_key, api_protocol=selection.protocol,
                           native_tools=selection.native_tools, stream=selection.stream, tool_choice=selection.tool_choice,
                           output_tokens=selection.output_tokens, context_window_tokens=selection.context_window_tokens,
                           context_reserve_tokens=selection.context_reserve_tokens, context_keep_recent_tokens=selection.context_keep_recent_tokens,
                           thinking_enabled=selection.thinking_enabled, reasoning_effort=selection.reasoning_effort,
                           text_decoder=normalize_local_reply,
                           usage_parser=parser_for(selection.protocol), configure_request=configure_usage,
                           budget_source=selection.budget_source,
                           request_counter=counter_for(selection.token_counter, config.base_url, selection.model_id,
                                                       config.api_key, selection.context_window_tokens))
    return provider


def validate_selection(selection: ApiRunSelection) -> None:
    config_for_selection(selection)


def model_payload() -> dict[str, object]:
    from codey.providers.local_selection import model_metadata

    local = _local_config.local_bootstrap_payload()
    saved = _local_config.load_local_config()
    target = _local_config.select_local_target(saved)
    model = str(local.get("model") or "")
    base = str(local.get("base_url") or "")
    metadata = model_metadata(base if local.get("connected") else "", model, api_key=target.api_key,
                              display_name=saved.display_name if model == saved.model else "")
    models = list(dict.fromkeys([*(local.get("models") or []), *([model] if model else [])]))
    return {"id": "local", "label": "Local", "base_url": base, "connected": local.get("connected", False),
            "models": [{"id": name, "name": metadata["display_name"] if name == model else name, "protocol": saved.api_protocol,
                        "efforts": metadata["thinking_options"] if name == model else []} for name in models],
            "error": local.get("error", ""), "default_model": model}
