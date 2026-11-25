"""Control continuations observe delayed readiness without replaying input."""
from __future__ import annotations

from pathlib import Path

import pytest

from codey.automation.browser_session import BrowserSession
from codey.knowledge.store import KnowledgeStore
from codey.operations.completion_context import CompletionContext
from codey.operations.kernel.kernel_execution import execute_turn
from codey.operations.task_execution import ExecutionDelegate
from codey.operations.task_handoff import task_handoff
from codey.operations.task_session import TaskSession
from codey.operations.turn_snapshot import build_turn_snapshot
from codey.policies.task_policy import TaskPolicy
from codey.research.answer_contract import answer_scope
from codey.research.answer_report import AnswerReport, compile_answer_report
from codey.research.tools import ResearchTools
from codey.runtime.core.models import ToolCall
from codey.types.json_value import JsonObject, decode_json_object
from tests.support.task_browser import install
from tests.test_browser_context_delivery import URL
from tests.test_browser_context_delivery import production_browser as production_browser
from tests.test_browser_readable_references import observed_snapshot, program


@pytest.mark.parametrize("transition", ["added", "enabled"])
def test_controls_purpose_waits_past_loading_text_for_the_actual_next_control(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession], transition: str,
) -> None:
    session, tools, browser = production_browser
    assert browser.navigate(URL).ok
    change = ("document.querySelector('#next').disabled=false;" if transition == "enabled" else
              "const next=document.createElement('button');next.textContent='Continue';this.after(next);")
    install(browser, '<p>Ready</p><button onclick="window.clicks=(window.clicks||0)+1;'
        "document.querySelector('p').textContent='Wait...';setTimeout(()=>{" + change +
        '},900)">Begin</button>' + ('<button id="next" disabled>Continue</button>' if transition == "enabled" else ''))
    snapshot = observed_snapshot(session, browser)
    result = program(session, tools, snapshot,
        "return unwrap(await tools.browser_click({ref:{role:'button',name:'Begin'},purpose:'controls'}));")
    assert result.ok, result.model_text
    current, _hint = task_handoff(session, build_turn_snapshot(session), CompletionContext(), browser)
    assert current.browser.references is not None
    assert current.browser.references.resolve({"role": "button", "name": "Continue"})
    assert browser.evaluate("window.clicks").observation["value"] == 1


def test_control_change_wait_does_not_treat_loading_text_as_a_new_control(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession],
) -> None:
    from codey.operations.browser.browser_waits import snapshot_wait_baseline

    session, _tools, browser = production_browser
    assert browser.navigate(URL).ok
    install(browser, '<p>Ready</p><button>Existing</button>')
    snapshot = observed_snapshot(session, browser)
    assert snapshot.browser.references is not None
    controls, document = snapshot_wait_baseline(session, snapshot.browser.references.receipt_ref, "controls_changed")
    assert isinstance(controls, list)
    assert browser.evaluate("document.querySelector('p').textContent='Wait...'").ok
    browser.discard_references()
    output = browser.wait_for_control_change(controls, document, 150)
    assert not output.ok and output.observation["reason"] == "timeout"
    assert output.observation["matched"] is False


def test_same_named_controls_swapping_enabled_regions_count_as_a_change(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession],
) -> None:
    session, tools, browser = production_browser
    assert browser.navigate(URL).ok
    install(browser, """<p>Waiting</p><button onclick="
        window.clicks=(window.clicks||0)+1;
        setTimeout(()=>{document.querySelector('#a').disabled=false;
            document.querySelector('#b').disabled=true;},200)
        ">Prepare</button>
        <section aria-label="Region A"><button id="a" disabled
            onclick="document.querySelector('p').textContent='Region A approved'">Review</button></section>
        <section aria-label="Region B"><button id="b"
            onclick="document.querySelector('p').textContent='Wrong region'">Review</button></section>""")
    initial = observed_snapshot(session, browser)
    changed = program(session, tools, initial, "return unwrap(await tools.browser_click("
        "{ref:{role:'button',name:'Prepare'},purpose:'controls'}));")
    assert changed.ok, changed.model_text
    waits = [row for row in session._memory_results.values() if row.call.name == "browser_wait"]
    assert len(waits) == 1 and waits[0].ok
    current, _ = task_handoff(session, build_turn_snapshot(session), CompletionContext(), browser)
    assert current.browser.references is not None
    assert current.browser.references.resolve({"role": "button", "name": "Review", "context": "Region A"})
    selected = program(session, tools, current, "return unwrap(await tools.browser_click("
        "{ref:{role:'button',name:'Review',context:'Region A'},purpose:'action'}));", turn=3)
    assert selected.ok, selected.model_text
    assert browser.evaluate("document.querySelector('p').textContent").observation["value"] == "Region A approved"
    assert browser.evaluate("window.clicks").observation["value"] == 1


def test_truncated_control_sample_cannot_satisfy_a_wait(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession],
) -> None:
    from codey.operations.browser.browser_waits import snapshot_wait_baseline

    session, _tools, browser = production_browser
    assert browser.navigate(URL).ok
    install(browser, "<p>Waiting</p><button>Original</button>")
    snapshot = observed_snapshot(session, browser)
    assert snapshot.browser.references is not None
    controls, document = snapshot_wait_baseline(session, snapshot.browser.references.receipt_ref, "controls_changed")
    assert isinstance(controls, list)
    assert browser.evaluate("document.body.insertAdjacentHTML('beforeend','<button>Added</button>'.repeat(85))").ok
    result = browser.wait_for_control_change(controls, document, 750)
    assert not result.ok and result.observation["reason"] == "timeout"


def test_research_controls_acquire_current_formal_text_and_reject_a_quote_from_the_old_source(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession], tmp_path: Path,
) -> None:
    session, tools, browser = production_browser
    session.policy = TaskPolicy(session.policy.grants | {"knowledge.read", "knowledge.write"}, strict_research=True)
    session.task_kind = "research"
    assert browser.navigate(URL).ok
    install(browser, '<p>Process not started.</p><button onclick="window.clicks=(window.clicks||0)+1;'
        'document.querySelector(\'p\').textContent=\'All Buttons Clicked\'">Advance</button>')
    initial = observed_snapshot(session, browser)
    assert program(session, tools, initial, "return unwrap(await tools.browser_read({}));", turn=0).ok
    initial_label = answer_scope(session).aliases[0][0]
    snapshot = observed_snapshot(session, browser)
    clicked = program(session, tools, snapshot,
        "return unwrap(await tools.browser_click({ref:{role:'button',name:'Advance'},purpose:'controls'}));")
    assert clicked.ok, clicked.model_text
    scope = answer_scope(session)
    assert len(scope.aliases) == 2
    final_label = scope.aliases[-1][0]
    delegate = ExecutionDelegate(session=session, research_tools=tools, research_scope=scope)
    wrong = ToolCall("submit_research_answer", {"markdown":
        f'Current page result [{final_label}]. "All Buttons Clicked" [{initial_label}]', "state": "answered"})
    rejected = delegate.execute(wrong)[0]
    assert not rejected.ok and "browser_quote_not_in_source" in rejected.model_text
    valid = ToolCall("submit_research_answer", {"markdown":
        f'Background instructions [{initial_label}]. "All Buttons Clicked" [{final_label}]', "state": "answered"})
    accepted = execute_turn(session, [valid], run_id="submitted", turn=2,
        snapshot=build_turn_snapshot(session), research_tools=tools)[0]
    assert accepted.ok, accepted.model_text
    assert "点击 1 次" in str(accepted.canonical["research_submission"])
    assert browser.evaluate("window.clicks").observation["value"] == 1
    waits = [row for row in session._memory_results.values() if row.call.name == "browser_wait"]
    assert len(waits) == 1 and not waits[0].ok
    assert decode_json_object(waits[0].canonical["value"])["reason"] == "timeout"
    store = KnowledgeStore(tmp_path / "knowledge")
    try:
        report = compile_answer_report(session, tools.ledger, store, project="", session_id="first")
        assert AnswerReport.from_payload(report.to_payload()) == report
        assert report.markdown.count("点击 1 次") == 1
        # A later delivered action must not rewrite the already accepted report.
        later = observed_snapshot(session, browser)
        assert later.browser.references is not None
        ref = later.browser.references.resolve({"role": "button", "name": "Advance"})
        assert execute_turn(session, [ToolCall("browser_click", {"ref": ref})], run_id="later", turn=3,
            snapshot=later, research_tools=tools)[0].ok
        assert compile_answer_report(session, tools.ledger, store, project="", session_id="first") == report
    finally:
        store.close()


def test_answered_requires_a_post_action_read_but_honest_missing_evidence_is_reportable(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession],
) -> None:
    session, tools, browser = production_browser
    session.policy = TaskPolicy(session.policy.grants | {"knowledge.read", "knowledge.write"}, strict_research=True)
    session.task_kind = "research"
    assert browser.navigate(URL).ok
    install(browser, '<p>Initial instructions</p><button onclick="this.textContent=\'Changed\'">Advance</button>')
    initial = observed_snapshot(session, browser)
    assert program(session, tools, initial, "return unwrap(await tools.browser_read({}));", turn=0).ok
    current = observed_snapshot(session, browser)
    assert current.browser.references is not None
    ref = current.browser.references.resolve({"role": "button", "name": "Advance"})
    clicked = execute_turn(session, [ToolCall("browser_click", {"ref": ref})], run_id="atomic", turn=1,
        snapshot=current, research_tools=tools)[0]
    assert clicked.ok, clicked.model_text
    scope = answer_scope(session)
    delegate = ExecutionDelegate(session=session, research_tools=tools, research_scope=scope)
    args: JsonObject = {"markdown": f"The current result is complete [{scope.aliases[0][0]}].", "state": "answered"}
    rejected = delegate.execute(ToolCall("submit_research_answer", args))[0]
    assert not rejected.ok and "browser_result_evidence_missing" in rejected.model_text
    partial = delegate.execute(ToolCall("submit_research_answer", {
        "markdown": f"Initial instructions [{scope.aliases[0][0]}]. The current result has not been read.", "state": "partial"}))[0]
    assert partial.ok, partial.model_text
