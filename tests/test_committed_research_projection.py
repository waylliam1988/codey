"""Real receipt commits own live and restart facts, including failure windows."""
from types import SimpleNamespace

import pytest

from codey.operations.kernel_execution import execute_turn
from codey.operations.kernel_session_recovery import restore_task_session
from codey.operations.recovery import record_entry_policy, recover_effects_for_resume
from codey.operations.task_effects import KernelRecordedProvider
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.research.ledger import ResearchLedger
from codey.research.tools import ResearchToolOutput, ResearchTools
from codey.runtime.core.models import ToolCall
from codey.runtime.effects.effect_records import RuntimeEffectStore
from tests.test_session_log_receipt_recovery_preserves_facts import _dirs, _open_runtime

URL = "https://example.com/observed"
FINAL = "https://example.com/final"


class Backend:
    def __init__(self):
        self.calls = []

    def search(self, query, limit=8):
        self.calls.append(("search", query))
        return [{"url": URL, "title": "Observed source", "snippet": "Preview"}]

    def fetch(self, url):
        self.calls.append(("fetch", url))
        return {"status": "ok", "url": FINAL, "title": "Observed source", "text": "exact body\n" * 1000}


def runtime(tmp_path, monkeypatch):
    project, state, logdir = _dirs(tmp_path)
    _, mutations, managed, _, deps, sink = _open_runtime(logdir, state, "s", "r", project)
    policy = TaskPolicy(frozenset({"control", "web.read"}))
    record_entry_policy(mutations, session_id="s", run_id="r", policy=policy)
    session = TaskSession(policy=policy)
    tools = ResearchTools(search=Backend(), store=None, changes=None)
    monkeypatch.setattr("codey.research.source_gateway.check_fetch_url", lambda *a, **kw: None)
    return session, tools, managed, deps, sink


@pytest.mark.parametrize("name,args", [("web_search", {"query": "observed"}), ("open_url", {"url": URL})])
@pytest.mark.parametrize("after_commit", [False, True])
def test_commit_failure_never_publishes_and_committed_interruption_recovers(
    tmp_path, monkeypatch, name, args, after_commit,
):
    session, tools, managed, deps, sink = runtime(tmp_path, monkeypatch)
    original_settle = sink.settle

    def interrupted(*args, **kwargs):
        # At the actual commit boundary neither the session nor the formal
        # acquisition ledger may already contain the result being committed.
        assert not session.opened_sources and not session.searches and not session.executed
        assert not tools.ledger.searches and not tools.ledger.opened_sources
        assert not tools.sources_read and not tools.search_result_urls
        if after_commit:
            original_settle(*args, **kwargs)
        raise RuntimeError("injected commit interruption")

    monkeypatch.setattr(sink, "settle", interrupted)
    with pytest.raises(RuntimeError, match="injected commit interruption"):
        execute_turn(session, [ToolCall(name, args)], run_id="r", effect_scope="task", turn=1,
                     research_tools=tools, managed_outputs=managed, session_id="s", intent_sink=sink)
    assert not session.opened_sources and not session.searches and not session.executed
    assert not tools.ledger.searches and not tools.ledger.opened_sources
    rows = RuntimeEffectStore(durable=deps.state.durable).load_effects("s", "r")
    assert bool(rows[0].settlement) is after_commit
    calls = list(tools.search.calls)
    if after_commit:
        recovered = recover_effects_for_resume(deps, session_id="s", run_id="r", project="", task_kind="project")
        assert recovered.ok
        frame = SimpleNamespace(run_id="r", recovered_tool_outcomes=recovered.recovered_tool_outcomes,
                                settled_tool_outcomes=recovered.settled_tool_outcomes,
                                recovered_tool_result_batch_id=recovered.recovered_tool_result_batch_id)
        fresh, ledger = TaskSession(policy=session.policy), ResearchLedger()
        restore_task_session(frame, fresh, research_ledger=ledger)
        assert fresh.searches == (["observed"] if name == "web_search" else [])
        assert fresh.opened_sources == ({FINAL} if name == "open_url" else set())
        assert bool(ledger.searches) == (name == "web_search")
        assert bool(ledger.opened_sources) == (name == "open_url")
        if name == "open_url":
            assert ledger.source_text_for_url(FINAL) == "exact body\n" * 1000
        before = (list(fresh.searches), list(ledger.searches), list(ledger.opened_sources))
        restore_task_session(frame, fresh, research_ledger=ledger)
        assert before == (fresh.searches, ledger.searches, ledger.opened_sources)
        assert tools.search.calls == calls


def test_live_and_recovery_share_projection_without_duplicate_searches(tmp_path, monkeypatch):
    session, tools, managed, deps, sink = runtime(tmp_path, monkeypatch)
    for turn in (1, 2):
        results = execute_turn(session, [ToolCall("web_search", {"query": f"observed-{turn}"})],
                               run_id="r", effect_scope="task", turn=turn,
                               research_tools=tools, managed_outputs=managed, session_id="s", intent_sink=sink)
        assert results[0].ok
        KernelRecordedProvider(SimpleNamespace(send=lambda _: "ack"), sink).send("results")
    recovered = recover_effects_for_resume(deps, session_id="s", run_id="r", project="", task_kind="project")
    frame = SimpleNamespace(run_id="r", recovered_tool_outcomes=recovered.recovered_tool_outcomes,
                            settled_tool_outcomes=recovered.settled_tool_outcomes,
                            recovered_tool_result_batch_id=recovered.recovered_tool_result_batch_id)
    fresh, ledger = TaskSession(policy=session.policy), ResearchLedger()
    restore_task_session(frame, fresh, research_ledger=ledger)
    assert session.searches == fresh.searches == ["observed-1", "observed-2"]
    assert tools.ledger.searches == ledger.searches
    assert tools.search_result_urls == set(session.search_results.values())
    restore_task_session(frame, session, research_ledger=tools.ledger)
    assert session.searches == fresh.searches and tools.ledger.searches == ledger.searches


def test_failed_acquisition_cannot_leave_source_evidence_in_formal_ledger():
    class FailedTools:
        ledger = ResearchLedger()

        def open_url(self, url, **kwargs):
            self.ledger.record_open(url, url, "Uncommitted", "partial body")
            return ResearchToolOutput("ERROR: partial acquisition", ok=False)

    tools = FailedTools()
    session = TaskSession(policy=TaskPolicy(frozenset({"control", "web.read"})))
    result = execute_turn(session, [ToolCall("open_url", {"url": URL})], research_tools=tools,
                          run_id="r", turn=1)[0]
    assert not result.ok
    assert not tools.ledger.opened_sources and not session.opened_sources


def test_knowledge_acquisition_view_uses_restored_ledger_not_stale_caches():
    from codey.operations.task_execution import ExecutionDelegate

    tools = ResearchTools(search=Backend(), store=None, changes=None)
    tools.ledger.record_open(URL, FINAL, "Restored", "body")
    tools.sources_read.add("https://example.com/uncommitted")
    view = ExecutionDelegate(research_tools=tools)._observation_tools()
    assert view._source_problem([URL, FINAL]) is None
    assert view._source_problem(["https://example.com/uncommitted"]) is not None
    assert tools.sources_read == {"https://example.com/uncommitted"}
