"""Locator instructions, turn contracts and successful observations agree."""
import json
from dataclasses import replace

import pytest

from codey.operations.completion_gate import evaluate
from codey.operations.kernel_execution import execute_turn
from codey.operations.kernel_protocol import build_turn_snapshot, normalize_turn
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.protocols.text_turn import decode_text_turn
from codey.providers.base import AssistantTurn, ProviderToolCall, TurnFinish
from codey.research.tools import ResearchTools
from codey.runtime.core.models import ToolCall

URL = "https://example.com/source"


class Backend:
    def fetch(self, url):
        return {"status": "ok", "url": URL, "title": "Document", "text": "start\n" + "filler\n" * 1000 + "tail: AMBER-913"}


@pytest.mark.parametrize("native", [False, True])
def test_locator_is_advertised_parses_executes_and_satisfies_explicit_contract(native, monkeypatch):
    monkeypatch.setattr("codey.research.source_gateway.check_fetch_url", lambda *a, **kw: None)
    policy = TaskPolicy(frozenset({"control", "web.read"}), sources_open_required=True,
                        required_checks=("research_sources_opened", "tool_succeeded:open_hit"))
    session = TaskSession(policy=policy)
    tools = ResearchTools(search=Backend(), store=None, changes=None)
    assert "open_hit" not in build_turn_snapshot(session).tool_names
    for turn, call in enumerate((ToolCall("open_url", {"url": URL}),
                                 ToolCall("source_search", {"url": URL, "query": "tail"})), 1):
        results = execute_turn(session, [call], research_tools=tools, turn=turn,
                               snapshot=build_turn_snapshot(session), run_id="r")
        assert results[0].ok, results[0].model_text
    context = {"research_ledger": tools.ledger}
    assert not evaluate(session, "AMBER-913", context=context).complete
    snapshot = build_turn_snapshot(session, native=native)
    assert "open_hit" in snapshot.tool_names
    reply = (AssistantTurn(tool_calls=(ProviderToolCall("hit-call", "open_hit", {"hit_id": "h1"}),),
                           finish=TurnFinish.COMPLETE) if native else
             decode_text_turn(json.dumps({"tool": "open_hit", "args": {"hit_id": "h1"}})))
    plan = normalize_turn(reply, policy=policy, snapshot=snapshot)
    assert not plan.protocol_error, plan.protocol_error
    opened = execute_turn(session, plan.calls, research_tools=tools, turn=3, snapshot=snapshot, run_id="r")
    assert opened[0].ok and "AMBER-913" in opened[0].model_text
    verdict = evaluate(session, "AMBER-913", context=context)
    assert verdict.complete, verdict.followup
    # The exact same schema is kept out by an explicit controller restriction.
    session.controller_denied = ("open_hit",)
    assert "open_hit" not in build_turn_snapshot(session).tool_names
    session.policy = replace(policy, grants=frozenset({"control"}))
    assert "open_hit" not in build_turn_snapshot(session).tool_names


def test_required_tool_observation_never_comes_from_answer_text_or_failed_results():
    session = TaskSession(policy=TaskPolicy(frozenset({"control"}), required_checks=("tool_succeeded:read_file",)))
    assert not evaluate(session, "read_file succeeded").complete
    session.executed["effect"] = {"name": "read_file", "ok": False}
    assert not evaluate(session, "done").complete
    session.executed["effect"]["ok"] = True
    assert evaluate(session, "done").complete


def test_locator_rendering_does_not_advertise_actions_outside_the_snapshot():
    from codey.research.source_search import SourceSearchHit, render_results
    text = render_results([SourceSearchHit(offset=12, snippet="observed")], hit_ids=("h1",))
    assert "open_hit" not in text
    assert "h1" in text and "12" in text


@pytest.mark.parametrize("commit_fails", [False, True])
def test_required_tool_check_uses_committed_receipt_and_recovers(tmp_path, monkeypatch, commit_fails):
    from types import SimpleNamespace

    from codey.operations.kernel_session_recovery import restore_task_session
    from codey.operations.recovery import recover_effects_for_resume
    from codey.research.ledger import ResearchLedger
    from tests.test_session_log_receipt_recovery_preserves_facts import _dirs, _open_runtime

    monkeypatch.setattr("codey.research.source_gateway.check_fetch_url", lambda *a, **kw: None)
    project, state, logdir = _dirs(tmp_path)
    _, mutations, managed, _, deps, sink = _open_runtime(logdir, state, "s", "r", project)
    policy = TaskPolicy(frozenset({"control", "web.read"}), required_checks=("tool_succeeded:open_url",))
    session = TaskSession(policy=policy)
    tools = ResearchTools(search=Backend(), store=None, changes=None)
    if commit_fails:
        def failed_commit(*args, **kwargs):
            raise OSError("injected commit failure")
        monkeypatch.setattr(sink, "settle", failed_commit)
    try:
        def execute():
            return execute_turn(session, [ToolCall("open_url", {"url": URL})], research_tools=tools,
                                turn=1, run_id="r", effect_scope="task", intent_sink=sink,
                                managed_outputs=managed, session_id="s", snapshot=build_turn_snapshot(session))
        if commit_fails:
            with pytest.raises(OSError, match="injected commit failure"):
                execute()
            assert not session.executed
            assert not evaluate(session, "opened").complete
        else:
            assert execute()[0].ok
            assert evaluate(session, "opened").complete
        recovered = recover_effects_for_resume(deps, session_id="s", run_id="r", project="", task_kind="project")
        fresh, ledger = TaskSession(policy=policy), ResearchLedger()
        frame = SimpleNamespace(run_id="r", recovered_tool_outcomes=recovered.recovered_tool_outcomes,
                                settled_tool_outcomes=recovered.settled_tool_outcomes,
                                recovered_tool_result_batch_id=recovered.recovered_tool_result_batch_id)
        restore_task_session(frame, fresh, research_ledger=ledger)
        assert evaluate(fresh, "opened").complete is (not commit_fails)
        count = len(fresh.executed)
        restore_task_session(frame, fresh, research_ledger=ledger)
        assert len(fresh.executed) == count
    finally:
        mutations.durable.close()
