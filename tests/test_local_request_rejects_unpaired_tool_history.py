"""No malformed native history may reach HTTP or mutate committed history."""
from copy import deepcopy
from unittest.mock import patch

import pytest

from codey.providers.api_provider import ApiProvider
from codey.providers.base import ProviderToolResult
from codey.providers.error_classification import RequestPrepError


@pytest.mark.parametrize("result_ids", [["a", "a"], ["a"], ["a", "foreign"], ["a", "b", "b"]])
def test_unpaired_result_batch_is_rejected_before_request(result_ids):
    provider = ApiProvider("http://localhost:5001/v1", "fake")
    provider._messages = [{"role": "assistant", "tool_calls": [{"id": "a"}, {"id": "b"}]}]
    before = deepcopy(provider._messages)
    with (
        patch.object(provider, "_complete_message", return_value={"content": "ok"}) as send,
        pytest.raises(RequestPrepError, match="tool.*pair"),
    ):
        provider.send_tool_results([ProviderToolResult(x, "ok") for x in result_ids])
    send.assert_not_called()
    assert provider._messages == before


@pytest.mark.parametrize("calls", [{"id": "a"}, "a", True, 1])
def test_non_list_tool_calls_are_rejected_before_request(calls):
    provider = ApiProvider("http://localhost:5001/v1", "fake")
    provider._messages = [{"role": "assistant", "tool_calls": calls}]
    before = deepcopy(provider._messages)
    with (
        patch.object(provider, "_complete_message", return_value={"content": "ok"}) as send,
        pytest.raises(RequestPrepError, match="tool.*pair"),
    ):
        provider.send_turn("continue")
    send.assert_not_called()
    assert provider._messages == before


@pytest.mark.parametrize("result_id", [1, 1.0, True, None, "", " "])
def test_result_ids_are_not_coerced_or_silently_dropped(result_id):
    provider = ApiProvider("http://localhost:5001/v1", "fake")
    provider._messages = [{"role": "assistant", "tool_calls": [{"id": "1"}]}]
    before = deepcopy(provider._messages)
    # The valid row also detects silently discarding an invalid extra row.
    rows = [ProviderToolResult("1", "ok"), ProviderToolResult(result_id, "bad")]
    if result_id in (1, 1.0, True):
        rows = rows[1:]
    with (
        patch.object(provider, "_complete_message", return_value={"content": "ok"}) as send,
        pytest.raises(RequestPrepError, match="tool.*pair"),
    ):
        provider.send_tool_results(rows)
    send.assert_not_called()
    assert provider._messages == before
