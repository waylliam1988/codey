"""Local Model Bootstrap: canonical config, discovery, review policy."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest import mock


def test_context_budget_presets() -> None:
    from codey.providers.local_config import context_budget_for_window

    small = context_budget_for_window(32_768)
    assert (small.context_window_tokens, small.context_reserve_tokens, small.context_keep_recent_tokens) == (
        32_768, 8_192, 12_000,
    )
    mid = context_budget_for_window(131_072)
    assert (mid.context_window_tokens, mid.context_reserve_tokens, mid.context_keep_recent_tokens) == (
        131_072, 16_384, 16_384,
    )
    big = context_budget_for_window(262_144)
    assert (big.context_window_tokens, big.context_reserve_tokens, big.context_keep_recent_tokens) == (
        262_144, 32_768, 32_000,
    )


def test_context_budget_validation() -> None:
    from codey.providers.local_config import LocalContextBudget, validate_context_budget

    assert validate_context_budget(LocalContextBudget(8192, 8192, 1000)) != ""
    assert validate_context_budget(LocalContextBudget(8192, 2048, 9000)) != ""
    assert validate_context_budget(LocalContextBudget(32768, 8192, 12000)) == ""
    # keep must fit inside window minus reserve, not just window.
    assert validate_context_budget(LocalContextBudget(8192, 2048, 7000)) != ""
    assert validate_context_budget(LocalContextBudget(8192, 2048, 6144)) == ""


def test_parse_update_derives_preset_and_mode() -> None:
    from codey.providers.local_config import LocalProviderConfig, parse_local_config_update

    previous = LocalProviderConfig(base_url="http://127.0.0.1:1234/v1", model="m", api_key="k")
    parsed, error = parse_local_config_update(
        {"base_url": "http://127.0.0.1:11434/v1", "model": "qwen",
         "context_window_tokens": "262144", "native_tools_mode": "off"},
        previous,
    )
    assert error == ""
    assert parsed is not None
    assert parsed.native_tools_mode == "off"
    assert parsed.context is not None
    assert (parsed.context.context_window_tokens, parsed.context.context_reserve_tokens,
            parsed.context.context_keep_recent_tokens) == (262144, 32768, 32000)
    # api_key omitted means "not provided" (the API layer decides reuse).
    assert parsed.api_key == ""

    parsed2, error2 = parse_local_config_update({"base_url": "", "model": "m"}, previous)
    assert parsed2 is None and error2 != ""


def test_canonical_roundtrip_and_legacy_migration(tmp_path: Path) -> None:
    from codey.providers import local_config as canonical

    path = tmp_path / "local-openai.json"
    with mock.patch.object(canonical, "_config_path", return_value=path):
        canonical.save_local_config(canonical.LocalProviderConfig(
            base_url="http://127.0.0.1:11434/v1", model="qwen", api_key="",
            native_tools_mode="auto", context=canonical.context_budget_for_window(262144),
        ))
        loaded = canonical.load_local_config()
        assert loaded.base_url == "http://127.0.0.1:11434/v1"
        assert loaded.context is not None and loaded.context.context_window_tokens == 262144
        import json

        assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == 2

        # Legacy flat shape migrates on read.
        path.write_text(
            json.dumps({"base_url": "http://127.0.0.1:5001/v1", "model": "g",
                        "native_tools": False, "context_window_tokens": 8192}),
            encoding="utf-8",
        )
        migrated = canonical.load_local_config()
        assert migrated.native_tools_mode == "off"
        assert migrated.context is not None and migrated.context.context_window_tokens == 8192


def test_effective_resolution(monkeypatch) -> None:
    from codey.providers.local_config import (
        LocalProviderConfig,
        context_budget_for_window,
        resolve_effective_local_config,
        resolve_local_native_tools,
    )

    config = LocalProviderConfig(base_url="http://127.0.0.1:11434/v1", model="qwen")
    monkeypatch.delenv("NATIVE_TOOLS", raising=False)
    assert resolve_local_native_tools(config) is True
    assert resolve_effective_local_config(
        config, endpoint=SimpleNamespace(base_url="http://127.0.0.1:11434/v1", models=("qwen",)),
    ).native_tools is True

    off = LocalProviderConfig(base_url="http://x/v1", native_tools_mode="off")
    assert resolve_local_native_tools(off) is False
    monkeypatch.setenv("NATIVE_TOOLS", "0")
    assert resolve_local_native_tools(config) is False
    monkeypatch.delenv("NATIVE_TOOLS", raising=False)

    monkeypatch.setenv("LOCAL_OPENAI_CONTEXT_WINDOW", "131072")
    effective = resolve_effective_local_config(
        LocalProviderConfig(base_url="http://x/v1", context=context_budget_for_window(32768)),
        endpoint=SimpleNamespace(base_url="http://x/v1", models=()),
    )
    assert effective.context.context_window_tokens == 131072
    assert effective.context.source == "env"
    monkeypatch.delenv("LOCAL_OPENAI_CONTEXT_WINDOW", raising=False)


def test_discovery_candidates_include_koboldcpp() -> None:
    from codey.providers.local_discovery import LOCAL_BASE_URL_CANDIDATES, LOCAL_ENDPOINT_CANDIDATES

    urls = [c.base_url for c in LOCAL_ENDPOINT_CANDIDATES]
    assert urls == [
        "http://127.0.0.1:1234/v1",
        "http://127.0.0.1:11434/v1",
        "http://127.0.0.1:5001/v1",
        "http://127.0.0.1:8080/v1",
    ]
    assert tuple(urls) == LOCAL_BASE_URL_CANDIDATES
    kinds = [c.kind for c in LOCAL_ENDPOINT_CANDIDATES]
    assert "koboldcpp" in kinds


def test_recommended_default_provider() -> None:
    from codey.app.provider_services import recommended_default_provider

    assert recommended_default_provider({"deepseek": True, "local": True}) == "deepseek"
    assert recommended_default_provider({"deepseek": False, "local": True}) == "local"
    assert recommended_default_provider({"deepseek": False, "local": False}) == "deepseek"
    assert recommended_default_provider({}) == "deepseek"


def test_review_policy_gating(monkeypatch) -> None:
    from codey.reviews.review_policy import (
        allow_self_review,
        load_review_policy,
    )

    monkeypatch.delenv("REVIEW_POLICY", raising=False)
    assert load_review_policy() == "web_if_available"
    assert allow_self_review("web_if_available") is True
    monkeypatch.setenv("REVIEW_POLICY", "require_web")
    assert load_review_policy() == "require_web"
    assert allow_self_review("require_web", writer_id="local") is False
    monkeypatch.setenv("REVIEW_POLICY", "self_review_allowed")
    assert allow_self_review("self_review_allowed") is True
    monkeypatch.delenv("REVIEW_POLICY", raising=False)


def test_run_review_require_web_refuses_self_review() -> None:
    from codey.app import review_service

    ctx = SimpleNamespace(
        providers=SimpleNamespace(supervisor=SimpleNamespace(is_available=lambda _pid: False)),
        emitted=[],
    )
    ctx.emit = lambda event: ctx.emitted.append(event)  # type: ignore[method-assign]
    ctx.set_provider_session = lambda *args: None  # type: ignore[method-assign]
    with mock.patch.object(
        review_service.providers, "connect_fresh_provider_tab",
        side_effect=AssertionError("must not self-review"),
    ):
        result = review_service.run_review(
            ctx,  # type: ignore[arg-type]
            session_id="s",
            project=".",
            task="t",
            writer_summary="w",
            changes={},
            recent_log="",
            writer_id="local",
            review_policy="require_web",
        )
    assert result is None
    assert any("no web reviewer" in str(event.get("text", "")) for event in ctx.emitted)


def test_queue_scope_covers_search_and_references(tmp_path: Path) -> None:
    from codey.runtime.core.models import ToolCall
    from codey.runtime.write.file_mutation_queue import (
        group_tool_calls_for_execution,
        scope_for_call,
    )

    assert scope_for_call(ToolCall(name="search", args={"query": "q", "path": "a.py"}))[0] == "read"
    # Unpathed search/references stay serial: future parallel readers must
    # never run a tree-wide scan concurrently with a writer.
    assert scope_for_call(ToolCall(name="references", args={"symbol": "s"}))[0] == "serial"
    assert scope_for_call(ToolCall(name="search", args={"query": "q"}))[0] == "serial"
    assert scope_for_call(ToolCall(name="search", args={"query": "q", "path": "."}))[0] == "serial"
    assert scope_for_call(ToolCall(name="run", args={"command": "pytest"}))[0] == "serial"
    assert scope_for_call(ToolCall(name="shell", args={"command": "ls"}))[0] == "serial"
    assert scope_for_call(ToolCall(name="read", args={"path": "a.py"}))[0] == "read"
    # Tree-wide ls stays serial; a concrete subpath stays a read.
    assert scope_for_call(ToolCall(name="ls", args={}))[0] == "serial"
    assert scope_for_call(ToolCall(name="ls", args={"path": "."}))[0] == "serial"
    assert scope_for_call(ToolCall(name="ls", args={"path": "docs"}))[0] == "read"
    # Same-path search/edit serialize; different paths batch.
    same = [
        ToolCall(name="edit", args={"path": "a.py"}),
        ToolCall(name="search", args={"query": "q", "path": "a.py"}),
    ]
    assert group_tool_calls_for_execution(same, str(tmp_path)) == [[0], [1]]
    other = [
        ToolCall(name="edit", args={"path": "a.py"}),
        ToolCall(name="search", args={"query": "q", "path": "b.py"}),
    ]
    assert group_tool_calls_for_execution(other, str(tmp_path)) == [[0, 1]]
    # Unpathed search serializes even against unrelated writes.
    unpathed = [
        ToolCall(name="edit", args={"path": "a.py"}),
        ToolCall(name="search", args={"query": "q"}),
    ]
    assert group_tool_calls_for_execution(unpathed, str(tmp_path)) == [[0], [1]]
    # A serial group is a true barrier: nothing joins it from either side.
    assert group_tool_calls_for_execution(
        [ToolCall(name="run", args={"command": "pytest"}), ToolCall(name="edit", args={"path": "a.py"})],
        str(tmp_path),
    ) == [[0], [1]]
    assert group_tool_calls_for_execution(
        [ToolCall(name="search", args={"query": "q"}), ToolCall(name="edit", args={"path": "a.py"})],
        str(tmp_path),
    ) == [[0], [1]]
    assert group_tool_calls_for_execution(
        [ToolCall(name="shell", args={"command": "ls"}), ToolCall(name="read", args={"path": "a.py"})],
        str(tmp_path),
    ) == [[0], [1]]
    assert group_tool_calls_for_execution(
        [ToolCall(name="edit", args={"path": "a.py"}), ToolCall(name="ls", args={"path": "."})],
        str(tmp_path),
    ) == [[0], [1]]
    assert group_tool_calls_for_execution(
        [ToolCall(name="edit", args={"path": "a.py"}), ToolCall(name="ls", args={"path": "docs"})],
        str(tmp_path),
    ) == [[0, 1]]
    # Unknown tools default to serial so future tools cannot silently batch.
    assert scope_for_call(ToolCall(name="mystery", args={}))[0] == "serial"
    assert scope_for_call(ToolCall(name="", args={})) == ("serial", "unknown")
    assert group_tool_calls_for_execution(
        [ToolCall(name="mystery", args={}), ToolCall(name="edit", args={"path": "a.py"})],
        str(tmp_path),
    ) == [[0], [1]]


def test_open_url_full_text_receipt(tmp_path: Path) -> None:
    from codey.research.output_receipts import maybe_externalize_output
    from codey.research.tools import ResearchToolOutput
    from codey.storage.managed_outputs import ManagedOutputStore

    store = ManagedOutputStore(tmp_path / "state")
    full = ("word " * 20000).strip()  # ~100k chars
    window = full[:6000]
    out = ResearchToolOutput(model_text=window, receipt_text=full)
    result = maybe_externalize_output(
        store=store,
        session_id="s",
        run_id="r",
        permission_profile="research",
        call=SimpleNamespace(name="open_url", args={"url": "https://example.com"}),
        output=out.receipt_text or out.model_text,
        turn=1,
        tool_index=0,
        presentation_result="",
        model_text_override=out.model_text,
    )
    assert result.truncated is True
    managed = result.managed_output()
    assert managed["original_bytes"] == len(full.encode("utf-8"))
    # The model sees the window, the store keeps the full text.
    assert len(result.model_text.encode("utf-8")) < len(full.encode("utf-8"))
    assert store.path_for("s", "r", str(managed["handle"])).is_file()


def test_api_save_accepts_bootstrap_fields() -> None:
    from codey.app import api as app_api
    from codey.providers.local_discovery import LocalEndpoint

    with (
        mock.patch.object(app_api, "load_local_config", return_value={"api_key": ""}),
        mock.patch.object(
            app_api, "probe_local_endpoint_detail",
            return_value=(LocalEndpoint("http://127.0.0.1:11434/v1", ("qwen",)), "ok"),
        ) as probe,
        mock.patch.object(app_api, "save_local_config") as save,
        mock.patch.object(app_api, "local_config_payload", return_value={"connected": True}),
    ):
        status, payload = app_api.save_local_provider_response({
            "base_url": "http://127.0.0.1:11434/v1",
            "model": "qwen",
            "native_tools_mode": "auto",
            "context_window_tokens": 262144,
        })
    assert status == 200 and payload["ok"] is True
    probe.assert_called_once_with("http://127.0.0.1:11434/v1", api_key="")
    _args, kwargs = save.call_args
    assert kwargs.get("native_tools_mode") == "auto"
    assert kwargs.get("context_window_tokens") == 262144
    assert kwargs.get("context_reserve_tokens") == 32768
    assert kwargs.get("context_keep_recent_tokens") == 32000


def test_parse_update_rejects_non_numeric_window() -> None:
    from codey.providers.local_config import LocalProviderConfig, parse_local_config_update

    previous = LocalProviderConfig(base_url="http://127.0.0.1:11434/v1", model="m", api_key="k")
    parsed, error = parse_local_config_update(
        {"base_url": "http://127.0.0.1:11434/v1", "model": "m", "context_window_tokens": "262kk"},
        previous,
    )
    assert parsed is None
    assert "positive integer" in error
    # Empty means "not provided" and keeps the previous context.
    parsed_empty, error_empty = parse_local_config_update(
        {"base_url": "http://127.0.0.1:11434/v1", "model": "m", "context_window_tokens": ""},
        previous,
    )
    assert error_empty == "" and parsed_empty is not None


def test_parse_update_rejects_non_numeric_reserve_and_keep() -> None:
    from codey.providers.local_config import LocalProviderConfig, parse_local_config_update

    previous = LocalProviderConfig(base_url="http://127.0.0.1:11434/v1", model="m", api_key="k")
    bad_reserve, reserve_error = parse_local_config_update(
        {
            "base_url": "http://127.0.0.1:11434/v1",
            "model": "m",
            "context_window_tokens": 32768,
            "context_reserve_tokens": "abc",
        },
        previous,
    )
    assert bad_reserve is None
    assert "context_reserve_tokens" in reserve_error
    bad_keep, keep_error = parse_local_config_update(
        {
            "base_url": "http://127.0.0.1:11434/v1",
            "model": "m",
            "context_window_tokens": 32768,
            "context_keep_recent_tokens": "0",
        },
        previous,
    )
    assert bad_keep is None
    assert "context_keep_recent_tokens" in keep_error
    # Explicit bad reserve fails even without a window (no silent ignore).
    bad_alone, alone_error = parse_local_config_update(
        {"base_url": "http://127.0.0.1:11434/v1", "model": "m", "context_reserve_tokens": "abc"},
        previous,
    )
    assert bad_alone is None
    assert "context_reserve_tokens" in alone_error
    # A good reserve/keep without a window is also a 400, not a silent no-op.
    good_alone, good_error = parse_local_config_update(
        {"base_url": "http://127.0.0.1:11434/v1", "model": "m", "context_reserve_tokens": "4096"},
        previous,
    )
    assert good_alone is None
    assert "context_window_tokens required" in good_error


def test_provider_ui_applies_recommended_before_and_after_catalog() -> None:
    from pathlib import Path as _Path

    text = (_Path(__file__).resolve().parents[1] / "codey" / "web" / "assets" / "provider_ui.js").read_text(
        encoding="utf-8"
    )
    assert "function applyRecommended(data)" in text
    before = text.index("applyRecommended(data);")
    assert text.index("if (!changed) return false;") > before
    after_providers = text.index("setProviders(ids, labels, data.default);")
    assert text.index("applyRecommended(data);", after_providers) > after_providers


def test_bootstrap_probes_once_and_prefers_remembered() -> None:
    from codey.providers import local_config as canonical
    from codey.providers.local_discovery import LocalEndpoint

    remembered = LocalEndpoint("http://127.0.0.1:1234/v1", ("m1", "m2"))
    calls: list[str] = []

    def fake_probe(base_url: str, *, api_key: str = "") -> LocalEndpoint | None:
        del api_key
        calls.append(f"probe:{base_url}")
        return remembered if base_url == remembered.base_url else None

    def fail_resolve(**kwargs: object) -> None:
        raise AssertionError(f"resolve must not run in bootstrap: {kwargs}")

    def fail_detect(**kwargs: object) -> None:
        raise AssertionError(f"detect must not run in bootstrap: {kwargs}")

    config = canonical.LocalProviderConfig(base_url=remembered.base_url, model="m2", api_key="")
    with (
        mock.patch.object(canonical, "load_local_config", return_value=config),
        mock.patch("codey.providers.local_discovery.probe_local_endpoint", side_effect=fake_probe),
        mock.patch(
            "codey.providers.local_discovery.detect_local_endpoint_probes",
            side_effect=AssertionError("remembered hit must skip the parallel pass"),
        ),
        mock.patch("codey.providers.local_discovery.resolve_local_endpoint", side_effect=fail_resolve),
        mock.patch("codey.providers.local_discovery.detect_local_endpoints", side_effect=fail_detect),
    ):
        payload = canonical.local_bootstrap_payload()
    assert payload["connected"] is True
    assert payload["base_url"] == remembered.base_url
    assert payload["models"][0] == "m2"
    assert payload["candidates"] == []
    assert calls == [f"probe:{remembered.base_url}"]


def test_bootstrap_single_parallel_pass_when_remembered_misses() -> None:
    from codey.providers import local_config as canonical
    from codey.providers.local_discovery import LocalEndpoint, LocalEndpointCandidate, LocalEndpointProbe

    candidates = (
        LocalEndpointCandidate("lmstudio", "LM Studio", "http://127.0.0.1:1234/v1"),
        LocalEndpointCandidate("ollama", "Ollama", "http://127.0.0.1:11434/v1"),
    )
    live = LocalEndpoint("http://127.0.0.1:11434/v1", ("qwen",))
    probe_calls: list[str] = []
    pass_calls: list[str] = []

    def fake_probe(base_url: str, *, api_key: str = "") -> LocalEndpoint | None:
        del api_key
        probe_calls.append(base_url)
        return None

    def fake_pass(*, api_key: str = "", timeout: float = 0.6, max_workers: int = 4) -> list[LocalEndpointProbe]:
        del api_key, timeout, max_workers
        pass_calls.append("pass")
        return [
            LocalEndpointProbe(candidate=candidates[0], endpoint=None, reason="unreachable"),
            LocalEndpointProbe(candidate=candidates[1], endpoint=live, reason="ok"),
        ]

    config = canonical.LocalProviderConfig(base_url="http://127.0.0.1:9/v1", model="", api_key="")
    with (
        mock.patch.object(canonical, "load_local_config", return_value=config),
        mock.patch("codey.providers.local_discovery.probe_local_endpoint", side_effect=fake_probe),
        mock.patch("codey.providers.local_discovery.detect_local_endpoint_probes", side_effect=fake_pass),
        mock.patch(
            "codey.providers.local_discovery.resolve_local_endpoint",
            side_effect=AssertionError("bootstrap must not call resolve"),
        ),
        mock.patch(
            "codey.providers.local_discovery.detect_local_endpoints",
            side_effect=AssertionError("bootstrap must not call detect twice"),
        ),
    ):
        payload = canonical.local_bootstrap_payload()
    assert pass_calls == ["pass"]
    assert payload["connected"] is True
    assert payload["base_url"] == live.base_url
    # Connected: no dead try-buttons next to the live endpoint.
    assert payload["candidates"] == []


def test_bootstrap_offers_try_buttons_only_when_unconnected() -> None:
    from codey.providers import local_config as canonical
    from codey.providers.local_discovery import LocalEndpointCandidate, LocalEndpointProbe

    candidates = (
        LocalEndpointCandidate("lmstudio", "LM Studio", "http://127.0.0.1:1234/v1"),
        LocalEndpointCandidate("ollama", "Ollama", "http://127.0.0.1:11434/v1"),
    )

    def fake_pass(*, api_key: str = "", timeout: float = 0.6, max_workers: int = 4) -> list[LocalEndpointProbe]:
        del api_key, timeout, max_workers
        return [
            LocalEndpointProbe(candidate=candidates[0], endpoint=None, reason="unreachable"),
            LocalEndpointProbe(candidate=candidates[1], endpoint=None, reason="unreachable"),
        ]

    config = canonical.LocalProviderConfig(base_url="http://127.0.0.1:9/v1", model="", api_key="")
    with (
        mock.patch.object(canonical, "load_local_config", return_value=config),
        mock.patch("codey.providers.local_discovery.probe_local_endpoint", return_value=None),
        mock.patch("codey.providers.local_discovery.detect_local_endpoint_probes", side_effect=fake_pass),
    ):
        payload = canonical.local_bootstrap_payload()
    assert payload["connected"] is False
    assert payload["candidates"] == ["http://127.0.0.1:1234/v1", "http://127.0.0.1:11434/v1"]


def test_bootstrap_falls_back_to_env_key_and_base(monkeypatch) -> None:
    from codey.providers import local_config as canonical
    from codey.providers.local_discovery import LocalEndpoint

    monkeypatch.setenv("LOCAL_OPENAI_API_KEY", "env-key")
    monkeypatch.setenv("LOCAL_OPENAI_BASE_URL", "http://127.0.0.1:8080/v1")
    live = LocalEndpoint("http://127.0.0.1:8080/v1", ("env-model",))
    seen: dict[str, str] = {}

    def fake_probe(base_url: str, *, api_key: str = "") -> LocalEndpoint | None:
        seen["base_url"] = base_url
        seen["api_key"] = api_key
        return live if base_url == live.base_url else None

    config = canonical.LocalProviderConfig(base_url="", model="", api_key="")
    with (
        mock.patch.object(canonical, "load_local_config", return_value=config),
        mock.patch("codey.providers.local_discovery.probe_local_endpoint", side_effect=fake_probe),
        mock.patch(
            "codey.providers.local_discovery.detect_local_endpoint_probes",
            side_effect=AssertionError("env hit must skip the parallel pass"),
        ),
    ):
        payload = canonical.local_bootstrap_payload()
    assert seen == {"base_url": live.base_url, "api_key": "env-key"}
    assert payload["connected"] is True
    assert payload["has_api_key"] is True
