"""Live-probe acceptance reads the saved response, including streaming calls."""
from __future__ import annotations

from unittest.mock import Mock

import pytest

from codey.operations.task_effects import KernelRecordedProvider
from codey.providers.base import AssistantTurn, ProviderToolCall
from tests.durable.test_provider_response_receipts import runtime
from tests.manual.real_local_done_probe import execution_observations


@pytest.mark.parametrize("native", [False, True])
def test_probe_counts_saved_done_with_no_chat_completion_message(tmp_path, native):
    durable, _, outputs, sink = runtime(tmp_path, sqlite=True)
    reply = AssistantTurn(tool_calls=(ProviderToolCall("done-original", "done", {"summary": "finished"}),),
                          raw={"finish_reason": "tool_calls"}) if native else '{"tool":"done","args":{"summary":"finished"}}'
    try:
        KernelRecordedProvider(Mock(send=Mock(return_value=reply)), sink).send("original")
        observed = execution_observations(durable, outputs, "s", "r")
    finally:
        durable.close()
    assert observed["done_call_requests"] == 1
    assert len(observed["pending_effects"]) == 1
    assert observed["operation_leaf"] == "provider_response_ready"
    assert observed["tool_effects"] == []
