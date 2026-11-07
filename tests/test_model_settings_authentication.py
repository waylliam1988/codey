"""Transport authentication labels do not masquerade as interactive auth."""
from pathlib import Path
from types import ModuleType

import pytest

from codey.app.context import AppContext
from codey.app.model_settings import settings_response
from codey.providers import api_connections
from codey.types.json_value import JsonObject


def test_settings_only_exposes_connector_authentication_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth: JsonObject = {"kind": "oauth_device", "state": "signed_out", "revision": 1, "actions": ["login"]}
    catalog: JsonObject = {"models": [], "authentication": "public"}

    class CatalogConnector(ModuleType):
        def cached_model_payload(self) -> JsonObject:
            return catalog

    class AuthConnector(CatalogConnector):
        def authentication_payload(self) -> JsonObject:
            return auth

    def connection(connection_id: str) -> ModuleType:
        return AuthConnector(connection_id) if connection_id == "cline" else CatalogConnector(connection_id)

    monkeypatch.setattr(api_connections, "connection_for", connection)
    ctx = AppContext(tmp_path, start_scheduler=False)
    try:
        status, payload = settings_response(ctx)
        assert status == 200
        sources = payload["sources"]
        assert isinstance(sources, list)
        by_id = {row["id"]: row for row in sources if isinstance(row, dict) and isinstance(row["id"], str)}
        assert "authentication" not in by_id["zen"]
        assert by_id["cline"]["authentication"] == auth
        assert catalog["authentication"] == "public"
    finally:
        ctx.close()
