"""Model use is an explicit, durable allowlist, independent of discovery."""

from __future__ import annotations

import copy
from unittest.mock import Mock

import pytest


def store_at(path):
    from codey.providers.model_preferences import ModelPreferences

    return ModelPreferences(path)


def save_sources(store, change):
    view = store.snapshot()
    sources = copy.deepcopy(view["sources"])
    change(sources)
    return store.save(sources, base_revision=view["revision"])


def test_group_off_retains_subset_and_reopening_restores_only_that_subset(tmp_path):
    store = store_at(tmp_path)
    save_sources(store, lambda s: s["websites"].update(models=["deepseek", "qwen"]))
    save_sources(store, lambda s: s["websites"].update(enabled=False))
    assert not store.allows("deepseek")
    assert store.snapshot()["sources"]["websites"]["models"] == ["deepseek", "qwen"]
    reopened = store_at(tmp_path)
    save_sources(reopened, lambda s: s["websites"].update(enabled=True))
    assert reopened.allows("deepseek") and reopened.allows("qwen")
    assert not reopened.allows("mimo")


def test_all_sources_off_is_valid_and_does_not_reenable_default(tmp_path):
    store = store_at(tmp_path)
    save_sources(store, lambda s: [source.update(enabled=False) for source in s.values()])
    assert not any(store.allows(pid) for pid in ("deepseek", "qwen", "local", "zen"))
    assert not store_at(tmp_path).allows("deepseek")


@pytest.mark.parametrize("source_id", ["websites", "local", "zen"])
def test_empty_selection_is_saved_disabled_and_cannot_trigger_automatic_discovery(tmp_path, source_id):
    store = store_at(tmp_path)
    saved = save_sources(store, lambda sources: sources[source_id].update(enabled=True, models=[]))
    assert saved["sources"][source_id] == {"enabled": False, "models": []}
    assert store_at(tmp_path).enabled(source_id) is False


def test_catalog_changes_never_select_new_models_or_forget_absent_selections(tmp_path):
    store = store_at(tmp_path)
    save_sources(store, lambda s: s["local"].update(enabled=True, models=["original-model"]))
    store.observe_catalog({"id": "local", "models": [{"id": "new-model", "name": "Actual name"}]})
    assert store.allows("local", "original-model")
    assert not store.allows("local", "new-model")
    assert store.catalog("local")["models"][0]["name"] == "Actual name"


def test_stale_settings_save_cannot_overwrite_newer_choices(tmp_path):
    from codey.providers.model_preferences import ModelPreferencesConflict

    a, b = store_at(tmp_path), store_at(tmp_path)
    stale = a.snapshot()
    save_sources(b, lambda s: s["websites"].update(enabled=False))
    with pytest.raises(ModelPreferencesConflict):
        a.save(stale["sources"], base_revision=stale["revision"])
    assert not a.allows("deepseek")


@pytest.mark.parametrize("invalid", [1, "false", None])
def test_enabled_requires_exact_boolean_and_failed_save_preserves_policy(tmp_path, invalid):
    store = store_at(tmp_path)
    original = store.snapshot()
    changed = copy.deepcopy(original["sources"])
    changed["websites"]["enabled"] = invalid
    with pytest.raises(ValueError):
        store.save(changed, base_revision=original["revision"])
    assert store.snapshot() == original


def test_disabled_connections_are_never_discovered_by_automatic_catalog_request(tmp_path, monkeypatch):
    from codey.app import api
    from codey.app.context import AppContext

    ctx = AppContext(tmp_path)
    try:
        store = ctx.providers.model_preferences
        save_sources(store, lambda s: [source.update(enabled=False) for source in s.values()])
        factory = Mock(side_effect=AssertionError("disabled connection was imported/probed"))
        monkeypatch.setattr("codey.providers.api_connections.connection_for", factory)
        status, payload = api.api_models_response(ctx)
        assert status == 200 and payload["connections"] == []
        factory.assert_not_called()
    finally:
        ctx.close()


def test_disabled_websites_never_enter_failover_repair_or_review(tmp_path):
    from codey.app import provider_services
    from codey.app.context import AppContext

    ctx = AppContext(tmp_path)
    try:
        save_sources(ctx.providers.model_preferences, lambda s: s["websites"].update(models=["deepseek", "qwen"]))
        assert ctx.providers.failover_order(lambda: {"mimo": True, "qwen": True}) == ("qwen", "deepseek")
        assert ctx.providers.self_repair_candidates("deepseek", ordered=("mimo", "qwen")) == ("qwen",)
        assert provider_services.reviewer_candidates(ctx, "deepseek") == ("qwen",)
    finally:
        ctx.close()


def test_disabled_send_is_rejected_before_connector_capture_or_worker_admission(tmp_path, monkeypatch):
    from codey.app import api
    from codey.app.context import AppContext

    ctx = AppContext(tmp_path)
    try:
        save_sources(ctx.providers.model_preferences, lambda s: s["local"].update(enabled=False, models=["model"]))
        capture = Mock(side_effect=AssertionError("disabled connection captured"))
        monkeypatch.setattr("codey.providers.api_connections.capture_selection", capture)
        submit = Mock()
        status, payload = api.run_submit_response(
            {"task": "hello", "provider": "local", "model_selection": {"model": "model"}}, submit, ctx=ctx
        )
        assert status == 409 and payload["code"] == "model_disabled"
        capture.assert_not_called()
        submit.assert_not_called()
    finally:
        ctx.close()


def test_settings_cannot_disable_active_or_approval_paused_source(tmp_path):
    from codey.app import model_settings
    from codey.app.context import AppContext

    ctx = AppContext(tmp_path)
    try:
        active = ctx.reserve_run(session_id="a", project=None, task="work", provider_id="deepseek")
        assert active is not None
        view = ctx.providers.model_preferences.snapshot()
        changed = copy.deepcopy(view["sources"])
        changed["websites"]["models"] = ["qwen"]
        status, payload = model_settings.save_response(ctx, {"sources": changed, "base_revision": view["revision"]})
        assert status == 409 and payload["code"] == "model_in_use"
        assert ctx.providers.model_preferences.allows("deepseek")
        ctx.release_run(active.run_id)
        ctx.approvals.add_shell("approval", {"provider": "deepseek", "run_id": active.run_id})
        assert model_settings.save_response(ctx, {"sources": changed, "base_revision": view["revision"]})[0] == 409
    finally:
        ctx.close()


def test_all_disabled_availability_never_starts_a_browser_or_local_probe(tmp_path, monkeypatch):
    from codey.app import provider_services
    from codey.app.context import AppContext

    ctx = AppContext(tmp_path)
    try:
        save_sources(ctx.providers.model_preferences, lambda s: [source.update(enabled=False) for source in s.values()])
        probe = Mock(side_effect=AssertionError("all disabled must not probe"))
        monkeypatch.setattr(provider_services, "provider_tab_availability", probe)
        assert not any(provider_services.provider_availability(ctx).values())
        probe.assert_not_called()
    finally:
        ctx.close()


def test_web_only_availability_does_not_probe_disabled_local_model(tmp_path, monkeypatch):
    from codey.app import provider_services
    from codey.app.context import AppContext
    from codey.providers import registry

    ctx = AppContext(tmp_path)
    try:
        provider_services.reset_provider_availability_cache()
        monkeypatch.setattr(registry, "detect_open_provider_tabs", lambda: {"deepseek": True})
        probe = Mock(side_effect=AssertionError("Local was disabled"))
        monkeypatch.setattr(registry, "local_endpoint_available", probe)
        assert provider_services.provider_availability(ctx)["deepseek"] is True
        probe.assert_not_called()
    finally:
        provider_services.reset_provider_availability_cache()
        ctx.close()


def test_saved_catalog_contains_no_credentials_even_in_model_records(tmp_path):
    store = store_at(tmp_path)
    store.observe_catalog(
        {
            "id": "local",
            "api_key": "secret",
            "models": [{"id": "a", "name": "Actual", "api_key": "secret", "headers": {"Authorization": "secret"}}],
        }
    )
    assert "secret" not in store.path.read_text()


def test_disabled_local_settings_reads_saved_values_without_endpoint_probe(tmp_path, monkeypatch):
    from codey.app import api
    from codey.app.context import AppContext
    from codey.providers import local_discovery

    ctx = AppContext(tmp_path)
    try:
        monkeypatch.setattr(
            local_discovery, "detect_local_endpoint_probes", Mock(side_effect=AssertionError("disabled discovery"))
        )
        monkeypatch.setattr(local_discovery, "probe_local_endpoint", Mock(side_effect=AssertionError("disabled probe")))
        status, payload = api.local_provider_response(ctx)
        assert status == 200 and payload["local"]["connected"] is False
    finally:
        ctx.close()


def test_api_review_can_only_choose_an_explicitly_selected_model(monkeypatch):
    from codey.providers import api_connections
    from codey.runtime.core.api_selection import ApiRunSelection

    writer = ApiRunSelection("local", "fixture", "writer", "openai-completions", True)
    connection = Mock()
    connection.model_payload.return_value = {"models": [{"id": "disabled", "review_eligible": True}, {"id": "chosen"}]}
    connection.capture_selection.side_effect = lambda data: data
    monkeypatch.setattr(api_connections, "connection_for", lambda _: connection)
    assert api_connections.capture_reviewer_selection(writer, allowed_models={"writer", "chosen"}) == {
        "model": "chosen"
    }


def test_disabled_between_http_check_and_reservation_never_queues_worker(tmp_path, monkeypatch):
    from codey.app import task_submit
    from codey.app.context import AppContext

    ctx = AppContext(tmp_path)
    try:
        save_sources(ctx.providers.model_preferences, lambda s: s["websites"].update(enabled=False))
        worker = Mock(return_value=True)
        monkeypatch.setattr(task_submit, "submit_browser_task", worker)
        with pytest.raises(ValueError, match="disabled"):
            task_submit.submit_task("a", None, "hello", 1, False, "deepseek", get_state=lambda: ctx)
        assert ctx.current_run() is None
        worker.assert_not_called()
    finally:
        ctx.close()


def test_old_chat_keeps_model_display_name_when_optional_source_is_removed(tmp_path):
    from codey.storage.ui_state_store import UiStateStore

    store = UiStateStore(tmp_path)
    store.save(
        {
            "active_id": "a",
            "sessions": [
                {
                    "id": "a",
                    "provider": "removed",
                    "modelSelection": {
                        "connection_id": "removed",
                        "model": "actual-id",
                        "name": "Actual model name",
                        "efforts": {},
                    },
                }
            ],
            "projects": [],
        },
        base_revision=0,
    )
    assert store.load()["sessions"][0]["modelSelection"]["name"] == "Actual model name"


def test_paused_api_approval_keeps_its_exact_model_selected(tmp_path):
    from codey.app import model_settings
    from codey.app.context import AppContext
    from codey.runtime.core.api_selection import ApiRunSelection

    ctx = AppContext(tmp_path)
    try:
        save_sources(
            ctx.providers.model_preferences, lambda s: s["local"].update(enabled=True, models=["paused", "other"])
        )
        frozen = ApiRunSelection("local", "fixture", "paused", "openai-completions", True)
        ctx.approvals.add_shell("approval", {"provider": "local", "_api_selection": frozen})
        view = ctx.providers.model_preferences.snapshot()
        view["sources"]["local"]["models"] = ["other"]
        status, result = model_settings.save_response(
            ctx, {"base_revision": view["revision"], "sources": view["sources"]}
        )
        assert status == 409 and result["code"] == "model_in_use"
        assert model_settings.settings_response(ctx)[1]["in_use"] == [{"provider": "local", "model": "paused"}]
    finally:
        ctx.close()


def test_application_api_reviewer_excludes_disabled_catalog_model(tmp_path, monkeypatch):
    from dataclasses import replace

    from codey.app.context import AppContext
    from codey.app.review_service import _selected_api_reviewer
    from codey.runtime.core.api_selection import ApiRunSelection

    ctx = AppContext(tmp_path)
    try:
        save_sources(
            ctx.providers.model_preferences, lambda s: s["local"].update(enabled=True, models=["writer", "chosen"])
        )
        run = ctx.reserve_run(session_id="s", project=None, task="review", provider_id="local")
        writer = ApiRunSelection("local", "fixture", "writer", "openai-completions", True)
        ctx.run_registry.bind_api_selection(run.run_id, writer)
        connector = Mock()
        connector.model_payload.return_value = {
            "models": [{"id": "disabled", "review_eligible": True}, {"id": "chosen"}]
        }
        connector.capture_selection.side_effect = lambda data: replace(writer, model_id=data["model"])
        monkeypatch.setattr("codey.providers.api_connections.connection_for", lambda _: connector)
        monkeypatch.setattr("codey.providers.api_connections.open_selection", lambda selection: selection)
        selected = _selected_api_reviewer(ctx, "s", run.run_id, "local")
        assert selected[1].model_id == "chosen"
    finally:
        ctx.close()


def test_live_reviewer_lease_blocks_only_its_used_model_until_released(tmp_path):
    from codey.app import model_settings, provider_services
    from codey.app.context import AppContext

    ctx = AppContext(tmp_path)
    try:
        with provider_services.use_model(ctx, "qwen"):
            view = ctx.providers.model_preferences.snapshot()
            view["sources"]["websites"]["models"] = ["deepseek"]
            assert (
                model_settings.save_response(ctx, {"base_revision": view["revision"], "sources": view["sources"]})[0]
                == 409
            )
            view["sources"]["websites"]["models"] = ["deepseek", "qwen"]
            assert (
                model_settings.save_response(ctx, {"base_revision": view["revision"], "sources": view["sources"]})[0]
                == 200
            )
        view = ctx.providers.model_preferences.snapshot()
        view["sources"]["websites"]["models"] = ["deepseek"]
        assert (
            model_settings.save_response(ctx, {"base_revision": view["revision"], "sources": view["sources"]})[0] == 200
        )
    finally:
        ctx.close()


def test_failover_availability_never_probes_disabled_api_connections(tmp_path, monkeypatch):
    from codey.app import provider_services
    from codey.app.context import AppContext
    from codey.providers import registry

    ctx = AppContext(tmp_path)
    try:
        monkeypatch.setattr(registry, "detect_open_provider_tabs", lambda: {"qwen": True})
        monkeypatch.setattr(registry, "local_endpoint_available", Mock(side_effect=AssertionError("disabled Local probe")))
        assert provider_services.provider_failover_order(ctx.providers)[0] == "qwen"
    finally:
        ctx.close()


def test_http_api_send_requires_an_explicit_model_instead_of_defaulting(tmp_path):
    from codey.app import api
    from codey.app.context import AppContext

    ctx = AppContext(tmp_path)
    try:
        save_sources(ctx.providers.model_preferences, lambda s: s["local"].update(enabled=True, models=["chosen"]))
        submit = Mock(return_value="run")
        status, data = api.run_submit_response({"task": "hello", "provider": "local"}, submit, ctx=ctx)
        assert status == 400 and data["reason"] == "model_selection_invalid"
        submit.assert_not_called()
    finally:
        ctx.close()


def test_captured_model_is_rechecked_before_admission(tmp_path, monkeypatch):
    from codey.app import api
    from codey.app.context import AppContext
    from codey.runtime.core.api_selection import ApiRunSelection

    ctx = AppContext(tmp_path)
    try:
        save_sources(ctx.providers.model_preferences, lambda s: s["local"].update(enabled=True, models=["chosen"]))
        monkeypatch.setattr("codey.providers.api_connections.capture_selection", lambda *_: ApiRunSelection("local", "revision", "other", "openai-completions", True))
        submit = Mock(return_value="run")
        status, data = api.run_submit_response({"task": "hello", "provider": "local", "model_selection": {"model": "chosen"}}, submit, ctx=ctx)
        assert status == 409 and data["code"] == "model_disabled"
        submit.assert_not_called()
    finally:
        ctx.close()


def test_consensus_uses_selected_api_model_and_holds_it_during_advice(tmp_path, monkeypatch):
    from codey.app import consensus_service, model_settings
    from codey.app.context import AppContext
    from codey.runtime.core.api_selection import ApiRunSelection

    ctx = AppContext(tmp_path)
    try:
        save_sources(ctx.providers.model_preferences, lambda s: s["local"].update(enabled=True, models=["chosen"]))
        selection = ApiRunSelection("local", "revision", "chosen", "openai-completions", True)
        capture = Mock(return_value=selection)
        monkeypatch.setattr("codey.providers.api_connections.capture_selection", capture)
        monkeypatch.setattr("codey.providers.api_connections.open_selection", lambda value: value)
        monkeypatch.setattr(consensus_service.providers, "connect_existing_provider", Mock(side_effect=AssertionError("unselected default")))
        def advice(**kwargs):
            assert kwargs["connect_existing"]("local") == selection
            view = ctx.providers.model_preferences.snapshot()
            view["sources"]["local"]["enabled"] = False
            assert model_settings.save_response(ctx, {"base_revision": view["revision"], "sources": view["sources"]})[0] == 409
            return "advice"
        monkeypatch.setattr(consensus_service, "run_consensus_core", advice)
        assert consensus_service.run_consensus(ctx, selected_provider=None, selected_provider_id="deepseek", task="advise") == "advice"
        capture.assert_called_once_with("local", {"model": "chosen"})
        assert not ctx.providers.model_uses
    finally:
        ctx.close()


def test_failed_reviewer_connection_does_not_keep_unused_model_locked(tmp_path, monkeypatch):
    from codey.app import provider_services, review_service
    from codey.app.context import AppContext

    ctx = AppContext(tmp_path)
    try:
        monkeypatch.setattr(provider_services, "reviewer_candidates", lambda *_: ("qwen", "mimo"))
        connector = Mock(side_effect=[RuntimeError("offline"), object()])
        monkeypatch.setattr(provider_services, "connect_existing_provider", connector)
        def attempt(*_args, **_kwargs):
            assert dict(ctx.providers.model_uses) == {("mimo", ""): 1}
            return "mimo", "reviewed"
        monkeypatch.setattr(review_service, "run_review_attempt", attempt)
        assert review_service.run_review(ctx, session_id="s", project="fixture", task="review", writer_summary="",
            changes={}, recent_log="", writer_id="deepseek", review_impact_map="") == ("mimo", "reviewed")
        assert not ctx.providers.model_uses
    finally:
        ctx.close()


def test_failed_advisor_connection_releases_its_model_scope(tmp_path, monkeypatch):
    from contextlib import ExitStack

    from codey.app import consensus_service
    from codey.app.context import AppContext

    ctx = AppContext(tmp_path)
    try:
        monkeypatch.setattr(consensus_service, "connect_consensus_provider", Mock(side_effect=RuntimeError("offline")))
        with ExitStack() as leases:
            with pytest.raises(RuntimeError, match="offline"):
                consensus_service._scoped_advisor(ctx, None, "qwen", leases)
            assert not ctx.providers.model_uses
    finally:
        ctx.close()
