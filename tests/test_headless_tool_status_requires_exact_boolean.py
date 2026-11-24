"""Headless cannot upgrade missing or malformed UI tool status to success."""
import pytest

from codey.app.event_payloads import machine_event_payload


@pytest.mark.parametrize("bad", [None, "false", "true", 1, [], {}])
def test_malformed_status_never_becomes_success(bad: object) -> None:
    event = {"type": "tool", "ok": bad, "error": False}
    payload = machine_event_payload(event)
    assert payload is not None and payload["ok"] is False


def test_missing_status_never_uses_legacy_error_fallback() -> None:
    payload = machine_event_payload({"type": "tool", "error": False})
    assert payload is not None and payload["ok"] is False
