from unittest.mock import patch

import pytest

from codey.app.cli import main
from codey.control.client import ControlClient
from codey.types.json_value import parse_json_object
from codey.utils.object_fields import object_fields


def test_control_send_without_session_returns_a_structured_input_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["control", "send", "--json"]) == 1
    output = capsys.readouterr()
    result = parse_json_object(output.out)
    assert result["ok"] is False
    error = result["error"]
    assert isinstance(error, dict)
    assert error["code"] == "INVALID_ARGUMENT"
    assert "conversation_id" in str(error["message"])
    assert output.err == ""


def test_control_send_looks_up_generation_for_valid_session(capsys: pytest.CaptureFixture[str]) -> None:
    operations: list[str] = []

    def call(_client: ControlClient, operation: str, params: object = None) -> dict[str, object]:
        operations.append(operation)
        assert object_fields(params)
        assert params["conversation_id"] == "session-1"
        if operation == "get_control":
            return {"control_generation": 4}
        assert params["control_generation"] == 4
        assert params["text"] == "hello"
        assert isinstance(params["request_id"], str) and params["request_id"]
        return {"run_id": "example-run"}

    with patch.object(ControlClient, "call", new=call):
        assert main(["control", "send", "--session", "session-1", "--text", "hello", "--json"]) == 0

    assert tuple(operations) == ("get_control", "send_message")
    assert parse_json_object(capsys.readouterr().out)["ok"] is True
