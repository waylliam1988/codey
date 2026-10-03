"""Headless cannot upgrade missing or malformed UI tool status to success."""
import pytest

from codey.app.event_payloads import machine_event_payload


@pytest.mark.parametrize("bad", [None, "false", "true", 1, [], {}])
def test_malformed_status_never_becomes_success(bad):
    event = {"type": "tool", "ok": bad, "error": False}
    assert machine_event_payload(event)["ok"] is False


def test_missing_status_never_uses_legacy_error_fallback():
    assert machine_event_payload({"type": "tool", "error": False})["ok"] is False
