"""Unsupported JSON representations must reach document request validation."""

from pathlib import Path

import pytest

from codey.app.context import AppContext
from codey.app.documents import document_response
from codey.types.json_value import JsonValue

REPRESENTATIONS: list[JsonValue] = [None, False, 42, "unknown", [], {}, ["content"], {"kind": "image"}]


@pytest.mark.parametrize("representation", REPRESENTATIONS)
def test_invalid_representations_are_client_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, representation: JsonValue,
) -> None:
    (tmp_path / "report.md").write_text("Report", encoding="utf-8")

    def state() -> dict[str, object]:
        return {
            "sessions": [{"id": "chat", "projectId": "project"}],
            "projects": [{"id": "project", "path": str(tmp_path)}],
        }

    ctx = AppContext(tmp_path / "state", start_scheduler=False, memory_enabled=False)
    monkeypatch.setattr(ctx, "load_ui_state", state)
    try:
        # The public JSON field accepts only the supported representation strings;
        # its validation must not depend on whether an unsupported value is hashable.
        status, payload = document_response(ctx, {
            "session_id": "chat", "kind": "workspace", "path": "report.md", "representation": representation,
        })
        assert status == 400
        assert payload == {"ok": False, "error": "document_representation_invalid"}
    finally:
        ctx.close()
