import pytest

from codey.automation.browser_contracts import BrowserAction, BrowserCommand


def test_browser_deadline_larger_than_float_is_invalid() -> None:
    command = BrowserCommand("operation", BrowserAction.SNAPSHOT, {}, "task", "browser", "", 1.0)
    payload = command.payload()
    payload["deadline"] = 10**400
    with pytest.raises(ValueError, match="invalid browser command deadline"):
        BrowserCommand.read(payload)
