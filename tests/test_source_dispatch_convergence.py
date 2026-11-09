"""Both provider protocols use the same source references, guards and receipts."""
from __future__ import annotations

import json

import pytest

from codey.operations.kernel_execution import execute_turn
from codey.operations.kernel_facts import project_result_observation
from codey.operations.kernel_protocol import build_turn_snapshot, normalize_turn
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.protocols.text_turn import decode_text_turn
from codey.providers.base import AssistantTurn, ProviderToolCall
from codey.research.ledger import ResearchLedger
from codey.research.tools import ResearchTools
from codey.runtime.core.models import ToolCall, ToolResult
from codey.types.json_value import JsonObject

URL = "https://example.com/" + "x" * 600 + "/article"


class Source:
    def __init__(self) -> None:
        self.requests: list[str] = []

    def search(self, query: str, *, limit: int) -> list[JsonObject]:
        return [{"url": URL, "title": "Field record", "snippet": "Locator preview"}]

    def fetch(self, url: str) -> dict[str, object]:
        self.requests.append(url)
        return {"status": "ok", "url": url, "title": "Field record",
                "text": "Original saffron quantity: 17 grams.\n" * 80}


@pytest.mark.parametrize("native", [False, True])
def test_source_aliases_share_dispatch_identity_and_recovery(
    monkeypatch: pytest.MonkeyPatch, native: bool,
) -> None:
    monkeypatch.setattr("codey.research.source_gateway.check_fetch_url", lambda *a, **kw: None)
    session = TaskSession(TaskPolicy(frozenset({"control", "web.read"})))
    source = Source()
    tools = ResearchTools(source, None, None)
    receipts: list[ToolResult] = []
    calls = [ToolCall("web_search", {"query": "saffron"}),
             ToolCall("open_result", {"result_id": "r1"}),
             ToolCall("source_search", {"source_id": "s1", "query": "saffron"}),
             ToolCall("open_hit", {"hit_id": "h1"}),
             ToolCall("reopen_source", {"source_id": "s1"}),
             ToolCall("source_search", {"url": URL, "query": "saffron"})]
    for turn, call in enumerate(calls, 1):
        snapshot = build_turn_snapshot(session, native=native)
        reply = (AssistantTurn(tool_calls=(ProviderToolCall(f"call-{turn}", call.name, call.args),)) if native else
                 decode_text_turn(json.dumps({"tool": call.name, "args": call.args})))
        plan = normalize_turn(reply, policy=session.policy, snapshot=snapshot)
        assert not plan.protocol_error and len(plan.calls) == 1
        result = execute_turn(session, plan.calls, research_tools=tools, run_id="r", turn=turn, snapshot=snapshot)[0]
        assert result.ok, result.model_text
        receipts.append(result)
    assert source.requests == [URL]
    assert session.source_ids == {"s1": URL} and session.search_results == {"r1": URL}
    assert receipts[2].canonical["hit_targets"] == receipts[5].canonical["hit_targets"]
    assert receipts[1].canonical["read_passages"] == receipts[4].canonical["read_passages"]
    restored = TaskSession(session.policy)
    ledger = ResearchLedger()
    for receipt in receipts:
        project_result_observation(restored, receipt.call, receipt, ok=True, research_ledger=ledger)
    assert restored.source_ids == session.source_ids and restored.hit_targets == session.hit_targets
    assert ledger.opened_sources == tools.ledger.opened_sources and ledger.read_passages == tools.ledger.read_passages
    # The same references cannot grant reading after web permission is removed.
    restored.policy = TaskPolicy(frozenset({"control"}))
    snapshot = build_turn_snapshot(restored, native=native)
    for call in calls[1:5]:
        assert call.name not in snapshot.tool_names
        result = execute_turn(restored, [call], research_tools=tools, snapshot=snapshot, run_id="r", turn=8)[0]
        assert not result.ok
    assert source.requests == [URL]
