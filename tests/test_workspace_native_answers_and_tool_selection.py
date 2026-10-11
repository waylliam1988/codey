"""Real native HTTP requests allow an answer or a tool without another router."""
import json

from codey.providers import api_transport
from codey.providers.api_provider import ApiProvider
from codey.providers.base import ProviderToolDefinition
from codey.runtime.core.api_selection import ApiRunSelection
from tests.test_native_tool_choice_matches_task_lifecycle import _Response


def test_default_native_selection_can_answer_with_tools_available(monkeypatch):
    requests = []

    def reply(request, timeout=None):
        requests.append(json.loads(request.data))
        return _Response({"finish_reason": "stop", "message": {"content": "Hello!"}})

    monkeypatch.setattr(api_transport, "open_request", reply)
    provider = ApiProvider("http://fixture.test/v1", "fixture")
    turn = provider.send_turn("Hello", [ProviderToolDefinition("shell", "", {"type": "object"})])
    assert turn.text == "Hello!"
    assert requests[0]["tool_choice"] == "auto"
    assert len(requests) == 1
    assert ApiRunSelection("local", "revision", "fixture", "openai-completions", True).tool_choice == "auto"
