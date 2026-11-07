"""Capability queries respect optional registrations and the active selection."""
from pathlib import Path
from types import ModuleType

import pytest

from codey.app.attachment_routes import image_support
from codey.app.context import AppContext
from codey.providers import api_connections
from codey.runtime.core.api_selection import ApiRunSelection
from codey.types.json_value import JsonObject


def test_removed_connector_is_not_imported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delitem(api_connections.API_CONNECTIONS, "cline")

    def forbidden_import(connection_id: str) -> ModuleType:
        raise AssertionError(f"removed connector imported: {connection_id}")

    monkeypatch.setattr(api_connections, "connection_for", forbidden_import)
    ctx = AppContext(tmp_path, start_scheduler=False)
    try:
        assert image_support(ctx, "chat", "cline", "vision") is False
    finally:
        ctx.close()


@pytest.mark.parametrize("supports_images", [True, False, None])
def test_cached_model_capability_requires_explicit_support(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, supports_images: bool | None,
) -> None:
    class Connector(ModuleType):
        def cached_model_payload(self) -> JsonObject:
            return {"models": [{"id": "model", "supports_images": supports_images}]}

    connector = Connector("fixture")
    monkeypatch.setattr(api_connections, "connection_for", lambda connection_id: connector)
    ctx = AppContext(tmp_path, start_scheduler=False)
    try:
        assert image_support(ctx, "chat", "local", "model") is (supports_images is True)
        assert image_support(ctx, "chat", "local", "missing") is False
    finally:
        ctx.close()


def test_connector_without_cached_capability_port_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api_connections, "connection_for", lambda connection_id: ModuleType("fixture"))
    assert api_connections.cached_model_supports_images("local", "model") is False


def test_running_task_uses_its_captured_capability(tmp_path: Path) -> None:
    ctx = AppContext(tmp_path, start_scheduler=False)
    try:
        run = ctx.reserve_run(session_id="chat", project=None, task="Inspect", provider_id="local")
        assert run is not None
        ctx.run_registry.bind_api_selection(run.run_id, ApiRunSelection(
            "local", "fixture", "vision", "openai-completions", True, supports_images=True,
        ))
        assert image_support(ctx, "chat", "removed", "unknown") is True
        ctx.release_run(run.run_id)
    finally:
        ctx.close()
