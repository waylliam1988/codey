"""Cancellation wakes the real teaching continuation without finalizing capture."""

from pathlib import Path
from typing import NoReturn

import pytest

from codey.app import sibling_probe
from codey.app.context import AppContext
from codey.providers import controls as provider_controls


def test_cancelled_teaching_never_finishes_the_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = AppContext(tmp_path, start_scheduler=False, memory_enabled=False)
    page = object()
    cancelled_pages: list[object] = []

    def start(observed_page: object) -> str:
        assert observed_page is page
        return "capture-token"

    def cancel_on_publication(event: dict[str, object]) -> None:
        if event.get("type") == "teach_request":
            ctx.cancel_pending_teach()

    def cancel(observed_page: object) -> None:
        cancelled_pages.append(observed_page)

    def finish(
        observed_page: object, token: str, action: str, *, timeout: float,
    ) -> NoReturn:
        raise AssertionError("cancelled teaching attempted to finalize a captured control")

    monkeypatch.setattr(provider_controls, "start_click_capture", start)
    monkeypatch.setattr(provider_controls, "cancel_click_capture", cancel)
    monkeypatch.setattr(provider_controls, "finish_click_capture", finish)
    monkeypatch.setattr(ctx, "emit", cancel_on_publication)
    request = provider_controls.ControlTeachRequest(
        provider_id="qwen", action=provider_controls.CONTROL_SEND_BUTTON, page=page, session_id="session-1",
    )
    try:
        with pytest.raises(provider_controls.ControlTeachCancelled):
            _result: object = sibling_probe.handle_control_teach(ctx, request)
        assert cancelled_pages == [page]
        assert not ctx.pending_teach_requests()
        assert ctx.approvals.pending_ui_event(None) is None
    finally:
        ctx.close()
