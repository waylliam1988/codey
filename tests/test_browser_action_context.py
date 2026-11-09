"""Browser work history comes from ordinary settled results, never evidence."""
from __future__ import annotations

from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.research.answer_context import answer_context
from codey.research.answer_contract import answer_scope
from codey.research.ledger import ResearchLedger
from codey.runtime.core.models import ToolCall, ToolResult
from tools.browser_experiment_contracts import browser_action_context


def test_settled_browser_actions_survive_research_view_without_becoming_passages() -> None:
    session = TaskSession(policy=TaskPolicy(grants=frozenset({"web.read"})), task_kind="research",
                          task_text="Find the locality quantity")
    session._memory_results["fill"] = ToolResult(call=ToolCall(name="browser_fill", args={"ref": "g1e1", "value": "Fuzhou"}),
        ok=True, model_text="Filled and verified g1e1", canonical={"browser_observation": {
            "action": "fill", "delivery": "verified", "value_length": 6}})
    session._memory_results["snapshot"] = ToolResult(call=ToolCall(name="browser_snapshot", args={}), ok=True,
        model_text="Current page", canonical={"browser_observation": {
            "generation": 3, "text": "uncitable DOM text", "elements": [], "complete": True}})
    scope = answer_scope(session)
    baseline = answer_context(session, ResearchLedger(), scope)
    assert "Browser action history" not in baseline
    with browser_action_context():
        rendered = answer_context(session, ResearchLedger(), scope)
    assert answer_context(session, ResearchLedger(), scope) == baseline
    assert '"action":"fill"' in rendered
    assert "Browser action history" in rendered
    assert "uncitable DOM text" not in rendered
    assert scope.aliases == ()
    assert scope.attempt_refs == ()
