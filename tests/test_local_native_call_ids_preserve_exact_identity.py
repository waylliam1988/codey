"""Native IDs must be strings and unique before a provider turn is committed."""
from unittest.mock import patch

import pytest

from codey.providers.api_provider import ApiProvider


@pytest.mark.parametrize("ids", [[1], [True], ["a", "a"]])
def test_unanswerable_native_ids_are_not_coerced_or_committed(ids):
    provider = ApiProvider("http://localhost:5001/v1", "fake")
    message = {"tool_calls": [{"id": call_id, "function": {"name": "done", "arguments": '{"summary":"ok"}'}} for call_id in ids]}
    with patch.object(provider, "_complete_message", return_value=message):
        turn = provider.send_turn("task")
    assert turn.tool_calls == ()
    assert turn.raw["malformed_dropped"] > 0
    assert provider._messages == []
