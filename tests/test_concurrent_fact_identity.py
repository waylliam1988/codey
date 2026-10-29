"""Concurrent acquisition must survive the real receipt/recovery boundary."""
from threading import Event
from types import SimpleNamespace

import pytest

from codey.operations.kernel_execution import _begin_pending_intents, execute_turn
from codey.operations.kernel_protocol import build_turn_snapshot
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.research.ledger import ResearchLedger
from codey.research.source_document import SourceDocument, SourcePage
from codey.research.tools import ResearchToolOutput
from codey.runtime.core.models import ToolCall, ToolResult
from tests.support.receipt_fixtures import inspect_receipt_facts
from tests.support.session_projection import restore_task_session
from tests.test_session_log_receipt_recovery_preserves_facts import _dirs, _open_runtime


@pytest.mark.parametrize("pdf", [False, True])
def test_reverse_completion_survives_receipt_restart(tmp_path, pdf):
    project, state, logdir = _dirs(tmp_path)
    _, _, managed, _, deps, sink = _open_runtime(logdir, state, "s", "r", project, task_kind="research")
    policy = TaskPolicy(grants=frozenset({"control", "web.read"}))
    session = TaskSession(policy=policy, task_kind="research")
    ledger = ResearchLedger()
    first_acquired, release_first = Event(), Event()
    a, b = "https://example.com/a", "https://example.com/b"

    class Tools:
        def __init__(self):
            self.ledger = ledger

        def open_url(self, url, **kwargs):
            page = int(kwargs.get("pages") or 1)
            if pdf:
                self.ledger.record_open_document(SourceDocument(
                    url, url, "Report", "pdf", "application/pdf", f"page {page}",
                    False, 2, (page,), (SourcePage(page, f"page {page}"),)))
            else:
                self.ledger.record_open(url, url, "Page", url)
            if url == a and page == 1:
                first_acquired.set()
                assert release_first.wait(15)
            return ResearchToolOutput("Title: Report\nobserved", ok=True)

    tools = Tools()

    # Concurrent children belong to one CodeMode parent and delivery batch.
    # Two unrelated top-level batches cannot own the same execution position.
    from threading import Thread

    from codey.codemode.kernel import build_execute_turn_executor

    session.policy = TaskPolicy(grants=frozenset({"control", "web.read", "project.read"}))
    snapshot = build_turn_snapshot(session)
    parent = build_execute_turn_executor(session=session, snapshot=snapshot, run_id="r", effect_scope="parallel",
        turn=1, project_path=project, research_tools=tools, managed_outputs=managed, session_id="s",
        intent_sink=sink, durable=deps.state.durable)
    calls = [{"url": a if pdf or index == 0 else b, "pages": str(index + 1) if pdf else ""} for index in range(2)]
    call = ToolCall("codemode", {"code": "return await Promise.all(inputs.calls.map(args => tools.open_url(args)));",
                                 "inputs": {"calls": calls}}, call_id="parent")
    def release():
        assert first_acquired.wait(15)
        # The second acquisition is committed before the first is released.
        import time
        deadline = time.monotonic() + 15
        while not any(e.intent.get("tool_index") == 1 and e.settlement is not None for e in deps.state.durable.effects("r")):
            assert time.monotonic() < deadline
            time.sleep(0.01)
        release_first.set()
    releaser = Thread(target=release)
    releaser.start()
    try:
        result = execute_turn(session, [call], executors={"codemode": parent}, run_id="r", turn=1,
            effect_scope="parallel", managed_outputs=managed, session_id="s", intent_sink=sink,
            snapshot=snapshot, project_path=project, durable=deps.state.durable)[0]
        assert result.ok, result
    finally:
        release_first.set()
        releaser.join(20)
    if pdf:
        assert ledger.pages_read_for_url(a) == {1, 2}
        assert ledger.source_pages_for_url(a) == {1: "page 1", 2: "page 2"}
    else:
        assert session.source_ids == {"s1": b, "s2": a}

    # Close and reopen the durable owner, then use the production restoration.
    deps.state.durable.store.close()
    from codey.durable import DurableSession, SQLiteDurableStore
    from codey.runtime.effects.effect_records import RuntimeEffectStore
    from codey.runtime.effects.tool_result_delivery import ToolResultDeliveryStore
    from codey.runtime.write.mutation_line import RuntimeMutationLine

    durable = DurableSession(SQLiteDurableStore(state / "durable.sqlite3"))
    deps.state.durable = durable
    deps.runtime_effects = RuntimeEffectStore(durable=durable)
    deps.runtime_mutations = RuntimeMutationLine(durable=durable)
    deps.tool_result_delivery = ToolResultDeliveryStore(durable=durable)
    try:
        recovered = inspect_receipt_facts(deps, session_id="s", run_id="r", project="", task_kind="research")
        assert recovered.ok
        restored, restored_ledger = TaskSession(policy=session.policy), ResearchLedger()
        frame = SimpleNamespace(run_id="r", recovered_tool_outcomes=(),
                                settled_tool_outcomes=recovered.settled_tool_outcomes)
        restore_task_session(frame, restored, research_ledger=restored_ledger)
        assert restored.source_ids == session.source_ids
        assert list(restored.source_ids.items()) == list(session.source_ids.items())
        if not pdf:
            from codey.operations.task_execution import ExecutionDelegate

            assert ExecutionDelegate(session=restored)._resolve_alias_url(
                "reopen_source", {"source_id": "s1"}) == b
        assert restored_ledger.source_pages_for_url(a) == ledger.source_pages_for_url(a)
        assert restored_ledger.source_text_for_url(a) == ledger.source_text_for_url(a)
        assert restored_ledger.opened_sources == ledger.opened_sources
        restore_task_session(frame, restored, research_ledger=restored_ledger)
        assert restored.source_ids == session.source_ids
    finally:
        durable.store.close()


def test_identical_native_codemode_parents_have_distinct_children(tmp_path):
    from codey.codemode.kernel import build_execute_turn_executor

    project, state, logdir = _dirs(tmp_path)
    _, _, _, _, deps, sink = _open_runtime(logdir, state, "s", "r", project)
    session = TaskSession(policy=TaskPolicy(grants=frozenset({"control", "project.read"})))
    snapshot = build_turn_snapshot(session)
    children = []

    def read(call):
        children.append(call.call_id)
        return ToolResult(
            call,
            "contents",
            ok=True,
            canonical={
                "value": {
                    "path": "a.py",
                    "text": "contents",
                    "start_line": 1,
                    "end_line": 1,
                    "total_lines": 1,
                    "next_offset": 0,
                    "text_complete": True,
                }
            },
        )

    executor = build_execute_turn_executor(session=session, snapshot=snapshot, run_id="r",
        effect_scope="task", turn=1, intent_sink=sink, executors={"read_file": read})
    calls = [ToolCall("codemode", {"code": "return await tools.read_file({path:'a.py'});"}, call_id=n)
             for n in ("native-a", "native-b")]
    try:
        results = execute_turn(session, calls, executors={"codemode": executor}, run_id="r",
                               turn=1, effect_scope="task", intent_sink=sink, snapshot=snapshot)
        assert all(result.ok for result in results), [result.model_text for result in results]
        assert len(set(children)) == 2
        refs = [result.canonical["child_effect_ids"] for result in results]
        assert len(refs[0]) == len(refs[1]) == 1
        assert set(refs[0]).isdisjoint(refs[1])
        repeated = execute_turn(session, calls, executors={"codemode": executor}, run_id="r",
                                turn=1, effect_scope="task", intent_sink=sink, snapshot=snapshot)
        assert all(result.ok for result in repeated)
        assert len(children) == 2
    finally:
        deps.state.durable.store.close()


def test_scope_typeerror_never_retries_begin_turn():
    class Sink:
        count = 0

        def begin_turn(self, *args, **kwargs):
            self.count += 1
            raise TypeError("scope processing failed after writing intent")

    sink = Sink()
    with pytest.raises(TypeError, match="scope processing"):
        _begin_pending_intents(sink, [ToolCall("read_file", {"path": "a"})],
                               TaskSession(policy=None), {}, "r", 1, 0, batch_scope="task")
    assert sink.count == 1


def test_source_binding_conflict_and_missing_binding_fail_without_mutation():
    from codey.operations.kernel_errors import RecoveryFailed
    from codey.operations.kernel_facts import record_facts_for_result
    from codey.research.ledger_receipts import ledger_observation

    url = "https://example.com/a"
    ledger = ResearchLedger()
    ledger.record_open(url, url, "A", "body")
    observation = ledger_observation(ledger, "open", url=url)
    session = TaskSession(policy=None)
    session.record_open("https://example.com/b", source_id="s2")
    call = ToolCall("open_url", {"url": url})
    result = ToolResult(call, "body", ok=True, canonical={"research_observation": observation, "source_id": "s2"})
    with pytest.raises(ValueError, match="conflicting source identity"):
        record_facts_for_result(session, call, result, ok=True)
    assert session.opened_sources == {"https://example.com/b"}
    assert session.source_id_for(url) == "s3"
    from codey.agents.request import RecoveredToolOutcome
    from codey.toolchain.runtime import ToolOutcome

    frame = SimpleNamespace(run_id="r", recovered_tool_outcomes=(), settled_tool_outcomes=(
        RecoveredToolOutcome(call, ToolOutcome("body", True, canonical={"research_observation": observation}),
                             1, 0, effect_id="unbound"),))
    with pytest.raises(RecoveryFailed, match="committed source_id"):
        restore_task_session(frame, session)
    assert session.opened_sources == {"https://example.com/b"}


@pytest.mark.parametrize("reverse", [False, True])
def test_pdf_overlap_uses_observation_time_and_delta_not_replay_order(reverse):
    from codey.operations.task_execution import ExecutionDelegate
    from codey.research.ledger_receipts import ledger_observation, restore_ledger_observation

    url = "https://example.com/report.pdf"
    receipts = []
    for stamp, pages in [("2026-10-24T01:00:00+00:00", {1: "old page", 2: "second page"}),
                         ("2026-10-24T10:00:01+09:00", {1: "new page"})]:
        acquisition = ResearchLedger()
        acquisition.record_open_document(SourceDocument(
            url, url, "Report", "pdf", "application/pdf", "\n".join(pages.values()),
            False, 2, tuple(pages), tuple(SourcePage(n, text) for n, text in pages.items())))
        receipt = ledger_observation(acquisition, "open", url=url)
        receipt["source"]["retrieved_at"] = stamp
        receipts.append(receipt)
    restored = ResearchLedger()
    for receipt in reversed(receipts) if reverse else receipts:
        restore_ledger_observation(restored, receipt)
    assert restored.pages_read_for_url(url) == {1, 2}
    assert restored.source_pages_for_url(url) == {1: "new page", 2: "second page"}
    assert restored.source_text_for_url(url) == "new page\n\nsecond page"
    assert restored.page_was_read(url, 1)
    view = ExecutionDelegate(session=TaskSession(policy=None),
                             research_tools=SimpleNamespace(ledger=restored))._observation_tools(source_read=True)
    assert view.ledger.opened_sources == []
    assert restored.pages_read_for_url(url) == {1, 2}


def test_repeated_pdf_observation_is_idempotent():
    from codey.research.ledger_receipts import ledger_observation, restore_ledger_observation

    url = "https://example.com/report.pdf"
    ledger = ResearchLedger()
    ledger.record_open_document(SourceDocument(url, url, "Report", "pdf", "application/pdf",
        "[page 1]\nexact body", False, 1, (1,), (SourcePage(1, "exact body"),)))
    receipt = ledger_observation(ledger, "open", url=url)
    restored = ResearchLedger()
    for _ in range(2):
        restore_ledger_observation(restored, receipt)
        assert restored.opened_sources == ledger.opened_sources
        assert restored.source_text_for_url(url) == "[page 1]\nexact body"
