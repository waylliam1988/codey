from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import tempfile
from unittest import mock

from codey.agent import RunResult
import codey.ghost.work_queue as work_queue_module
from codey.knowledge.note import KnowledgeNote
from codey.knowledge.store import KnowledgeStore
from codey.research.ledger import ResearchLedger
from codey.research.object_model import build_research_record
from codey.research.pipeline import ResearchIterationRun
from codey.research.report_quality import review_report_quality
from codey.research.runner import ResearchRunResult
from codey.review import ReviewResult
from codey import server
from codey.task_runner import TaskRequest, TaskRunner
from codey.work_checkpoint import WorkCheckpointStore


class _Provider:
    name = "DeepSeek Web"
    location = "https://chat.deepseek.com/"

    def __init__(self, reply: str = "ok") -> None:
        self.reply = reply
        self.prompts: list[str] = []
        self.closed = False

    def new_chat(self, timeout: float | None = None) -> None:
        del timeout

    def send(self, text: str, timeout: float | None = None) -> str:
        del timeout
        self.prompts.append(text)
        return self.reply

    def close(self) -> None:
        self.closed = True


class _FakeIndex:
    def recent(self, *args, **kwargs):
        return [{
            "id": "note-1",
            "type": "synthesis",
            "title": "Provider recovery synthesis",
            "body": "Research synthesis.",
            "open_questions": '["Should we keep tracking provider recovery?"]',
            "updated": "2999-01-01T00:00:00Z",
            "session_id": "s1",
            "project": "",
        }]


class _FakeKnowledge:
    index = _FakeIndex()


def _empty_changes(*_args, **_kwargs) -> dict:
    return {"ok": True, "changed_count": 0, "files": [], "diff": ""}


def _reviewable_changes(*_args, **_kwargs) -> dict:
    return {
        "ok": True,
        "changed_count": 1,
        "files": [{"path": "app.py", "status": "M"}],
        "diff": "diff --git a/app.py b/app.py\n-old\n+new\n",
    }


def _runner(
    state: server.State,
    *,
    agent_run=None,
    collect_changes=_empty_changes,
    run_review=None,
    router_provider_factory=None,
    is_git_repository=None,
) -> TaskRunner:
    return TaskRunner(
        state,
        agent_run=agent_run or mock.Mock(return_value=RunResult("done", "done", 1)),
        collect_changes=collect_changes,
        run_review=run_review or mock.Mock(return_value=None),
        capture_provider_failure=server.capture_provider_failure,
        project_facts=state.project_facts,
        work_checkpoints=state.work_checkpoints,
        run_ledgers=state.run_ledgers,
        run_traces=state.run_traces,
        evidence_ledgers=state.evidence_ledgers,
        managed_outputs=state.managed_outputs,
        knowledge_store=state.knowledge_store,
        is_git_repository=is_git_repository or (lambda _project: True),
        ghost_router_provider_factory=router_provider_factory,
    )


def _seed_research_item(state: server.State) -> str:
    assert state.ghost_continuity is not None
    assert state.ghost_work_queue is not None
    state.ghost_continuity.sync_from_sources(
        knowledge_store=_FakeKnowledge(),
        session_id="s1",
        run_id="note-run",
        mode="chat",
    )
    state.ghost_work_queue.sync_from_sources(
        continuity_store=state.ghost_continuity,
        session_id="s1",
    )
    item = state.ghost_work_queue.list_items(status="queued", session_id="s1")[0]
    return item.id


def _research_record():
    url = "https://example.com/provider-recovery"
    source_text = (
        "Teams should keep tracking provider recovery because it depends on reliable "
        "browser session checks."
    )
    summary = (
        "## 结论\n"
        "- Teams should keep tracking provider recovery because it depends on reliable browser session checks. [1]\n\n"
        "## 关键证据\n"
        "- [1] The opened source says teams should keep tracking provider recovery because it depends on reliable browser session checks.\n\n"
        "## 反证与限制\n"
        "- 未找到强反证；需要持续追踪 provider UI 变化。\n\n"
        "## 来源质量\n"
        "- [1] secondary · web · fresh · example.com\n\n"
        "## 搜索覆盖\n"
        "- query: provider recovery\n"
        "- opened: Provider recovery article\n"
        "- skipped: none representative\n\n"
        "## 来源\n"
        f"[1] Provider recovery article - {url}"
    )
    ledger = ResearchLedger()
    ledger.record_search("provider recovery", [{
        "title": "Provider recovery article",
        "url": url,
        "snippet": "Provider recovery.",
    }])
    ledger.record_open(
        requested_url=url,
        final_url=url,
        title="Provider recovery article",
        text=source_text,
    )
    prepared = ledger.prepare_evidence_items(
        [{
            "claim": (
                "Teams should keep tracking provider recovery because it depends on reliable "
                "browser session checks."
            ),
            "source_url": url,
            "excerpt": (
                "Teams should keep tracking provider recovery because it depends on reliable "
                "browser session checks."
            ),
            "stance": "supports",
        }],
        fallback_sources=[url],
        fallback_claim=(
            "Teams should keep tracking provider recovery because it depends on reliable "
            "browser session checks."
        ),
        fallback_body=source_text,
        note_type="fact",
    )
    assert not prepared.error
    ledger.add_evidence_items(list(prepared.items), note_id="note-result")
    quality = review_report_quality(
        summary,
        ledger=ledger,
        opened_sources=ledger.final_url_set(),
        search_result_urls={url},
    )
    assert quality.ok
    return build_research_record(
        question="Should we keep tracking provider recovery?",
        summary=summary,
        ledger=ledger,
        review=quality,
        run_id="run-task-runner-research",
        session_id="s1",
        synthesis_id="note-result",
        stop_reason="done",
    )


def _seed_review_item(state: server.State, project: Path) -> str:
    assert state.ghost_work_queue is not None
    item = work_queue_module._new_item(
        kind="review",
        status="queued",
        scope="project",
        scope_ref=str(project),
        title="Review the current local diff",
        why_now="A bounded local follow-up requested review.",
        priority=0.8,
        confidence=0.9,
        source="user",
        source_ref="seed-review",
        evidence_refs=("review:seed",),
        run_refs=(),
        now="2999-01-01T00:00:00Z",
    )
    assert state.ghost_work_queue._replace_items([item], "test_seed_review_item")
    return item.id


def _last_trace_payload(state: server.State) -> dict[str, object]:
    assert state.run_traces is not None
    run_id = str(state.last_terminal_event["run_id"])
    path = state.run_traces.path_for("s1", run_id)
    return json.loads(path.read_text(encoding="utf-8"))


def test_strict_continue_consumes_research_item_before_router() -> None:
    with tempfile.TemporaryDirectory() as td:
        state = server.State(td)
        item_id = _seed_research_item(state)
        queued_item = state.ghost_work_queue.list_items(status="queued", session_id="s1")[0]
        wrapped_question = work_queue_module.render_work_item_task(
            queued_item,
            user_request="继续",
        )
        router_factory = mock.Mock(return_value=_Provider('{"mode":"chat","confidence":0.99}'))
        runner = _runner(state, router_provider_factory=router_factory)
        record = _research_record()
        runner._run_research_iteration = mock.Mock(return_value=ResearchIterationRun(
            result=ResearchRunResult(
                wrapped_question,
                "researched",
                "done",
                1,
                synthesis_id="note-result",
                citation_map=[{"claim": "x"}],
                research_record=record,
            ),
        ))

        with mock.patch.object(state, "get_provider", return_value=_Provider()):
            runner.run(TaskRequest("s1", None, "继续", 8, False, "deepseek"))
            state.wait_for_ghost_sleep(timeout=2)
        item = state.ghost_work_queue.list_items()[0]
        trace_payload = _last_trace_payload(state)

    router_factory.assert_not_called()
    assert runner._run_research_iteration.call_count == 1
    assert state.last_terminal_event["mode"] == "research"
    assert item.id == item_id
    assert item.status == "done"
    assert any(ref.startswith("research_proof:") for ref in item.proof_refs)
    assert "research:note-result" in item.proof_refs
    completion_proofs = trace_payload["completion_proofs"]
    assert len(completion_proofs) == 1
    assert completion_proofs[0]["domain"] == "research"
    assert completion_proofs[0]["status"] == "complete"
    assert completion_proofs[0]["satisfied"] is True
    assert "research:note-result" in completion_proofs[0]["external_refs"]
    assert any(ref.startswith("research_proof:") for ref in completion_proofs[0]["external_refs"])
    assert all(row["status"] == "pass" for row in completion_proofs[0]["checks"])


def test_strict_continue_blocks_research_item_without_research_record() -> None:
    with tempfile.TemporaryDirectory() as td:
        state = server.State(td)
        item_id = _seed_research_item(state)
        router_factory = mock.Mock(return_value=_Provider('{"mode":"chat","confidence":0.99}'))
        runner = _runner(state, router_provider_factory=router_factory)
        runner._run_research_iteration = mock.Mock(return_value=ResearchIterationRun(
            result=ResearchRunResult(
                "Should we keep tracking provider recovery?",
                "researched",
                "done",
                1,
                synthesis_id="note-result",
                citation_map=[{"claim": "x"}],
            ),
        ))

        with mock.patch.object(state, "get_provider", return_value=_Provider()):
            runner.run(TaskRequest("s1", None, "继续", 8, False, "deepseek"))
            state.wait_for_ghost_sleep(timeout=2)
        item = state.ghost_work_queue.list_items()[0]
        trace_payload = _last_trace_payload(state)

    router_factory.assert_not_called()
    assert runner._run_research_iteration.call_count == 1
    assert state.last_terminal_event["mode"] == "research"
    assert item.id == item_id
    assert item.status == "blocked"
    assert item.blocked_reason == "research_proof_missing_research_record"
    assert item.proof_refs == ()
    proof_reviews = trace_payload["research_proof_reviews"]
    assert len(proof_reviews) == 1
    assert proof_reviews[0]["proof_ref"].startswith("research_proof:")
    assert proof_reviews[0]["ok"] is False
    assert proof_reviews[0]["answers_question"] is False
    assert proof_reviews[0]["answer_status"] == "not_answered"
    assert proof_reviews[0]["question_digest"].startswith("sha256:")
    assert "record_id" not in proof_reviews[0]
    assert "record_digest" not in proof_reviews[0]
    assert "missing_research_record" in proof_reviews[0]["reason_codes"]
    completion_proofs = trace_payload["completion_proofs"]
    assert len(completion_proofs) == 1
    assert completion_proofs[0]["domain"] == "research"
    assert completion_proofs[0]["status"] == "failed"
    assert completion_proofs[0]["satisfied"] is False
    assert completion_proofs[0]["blocked_reason"] == "research_proof_missing_research_record"
    assert completion_proofs[0]["checks"][0] == {
        "check_id": "research_proof_review",
        "status": "fail",
        "reason_code": "research_proof_missing_research_record",
    }


def test_strict_continue_blocks_partial_research_item_without_duplicate_proof_trace() -> None:
    with tempfile.TemporaryDirectory() as td:
        state = server.State(td)
        item_id = _seed_research_item(state)
        queued_item = state.ghost_work_queue.list_items(status="queued", session_id="s1")[0]
        wrapped_question = work_queue_module.render_work_item_task(
            queued_item,
            user_request="继续",
        )
        router_factory = mock.Mock(return_value=_Provider('{"mode":"chat","confidence":0.99}'))
        runner = _runner(state, router_provider_factory=router_factory)
        record = replace(_research_record(), answer_status="partial")
        runner._run_research_iteration = mock.Mock(return_value=ResearchIterationRun(
            result=ResearchRunResult(
                wrapped_question,
                "researched",
                "done",
                1,
                synthesis_id="note-result",
                citation_map=[{"claim": "x"}],
                research_record=record,
            ),
        ))

        with mock.patch.object(state, "get_provider", return_value=_Provider()):
            runner.run(TaskRequest("s1", None, "继续", 8, False, "deepseek"))
            state.wait_for_ghost_sleep(timeout=2)
        item = state.ghost_work_queue.list_items()[0]
        trace_payload = _last_trace_payload(state)

    router_factory.assert_not_called()
    assert runner._run_research_iteration.call_count == 1
    assert state.last_terminal_event["mode"] == "research"
    assert item.id == item_id
    assert item.status == "blocked"
    proof_reviews = trace_payload["research_proof_reviews"]
    assert len(proof_reviews) == 1
    assert proof_reviews[0]["proof_ref"].startswith("research_proof:")
    assert proof_reviews[0]["answer_status"] == "partial"
    assert "record_id" in proof_reviews[0]
    assert "record_digest" in proof_reviews[0]
    assert "partial_answer" in proof_reviews[0]["reason_codes"]
    completion_proofs = trace_payload["completion_proofs"]
    assert len(completion_proofs) == 1
    assert completion_proofs[0]["domain"] == "research"
    assert completion_proofs[0]["status"] == "failed"
    assert completion_proofs[0]["satisfied"] is False
    assert completion_proofs[0]["blocked_reason"] == "research_proof_partial_answer"


def test_non_strict_continue_does_not_consume_queue_and_uses_router() -> None:
    with tempfile.TemporaryDirectory() as td:
        state = server.State(td)
        _seed_research_item(state)
        route_provider = _Provider('{"mode":"chat","confidence":0.99,"reason":"chat"}')
        router_factory = mock.Mock(return_value=route_provider)
        main_provider = _Provider("chat reply")
        runner = _runner(state, router_provider_factory=router_factory)

        with mock.patch.object(state, "get_provider", return_value=main_provider):
            runner.run(TaskRequest("s1", None, "继续查 pytest 变化", 8, False, "deepseek"))
            state.wait_for_ghost_sleep(timeout=2)
        item = state.ghost_work_queue.list_items(status="queued", session_id="s1")[0]

    router_factory.assert_called_once()
    assert state.last_terminal_event["mode"] == "chat"
    assert item.status == "queued"


def test_post_turn_sync_harvests_research_interest_candidates() -> None:
    with tempfile.TemporaryDirectory() as td:
        state = server.State(td)
        state.knowledge_store = KnowledgeStore(Path(td, "knowledge"))
        assert state.ghost_work_queue is not None
        try:
            state.knowledge_store.write_note(KnowledgeNote.create(
                id="war-helium",
                type="synthesis",
                title="War and helium supply",
                body="Evidence note.",
                tags=["research"],
                relations=[{"src": "war", "dst": "helium supply", "kind": "affects"}],
                session_id="s1",
            ))
            state.knowledge_store.write_note(KnowledgeNote.create(
                id="war-copper",
                type="synthesis",
                title="War and copper supply",
                body="Evidence note.",
                tags=["research"],
                relations=[{"src": "war", "dst": "copper supply", "kind": "affects"}],
                session_id="s1",
            ))
            runner = _runner(state, router_provider_factory=None)

            with mock.patch.object(state, "get_provider", return_value=_Provider("plain chat")):
                runner.run(TaskRequest("s1", None, "hello", 8, False, "deepseek"))
                state.wait_for_ghost_sleep(timeout=2)
            items = state.ghost_work_queue.list_items(status="queued", session_id="s1")
        finally:
            state.knowledge_store.close()

    assert len(items) == 1
    assert items[0].kind == "research"
    assert "copper supply" in items[0].title
    assert "helium supply" in items[0].title


def test_project_followup_item_consumes_into_project_mode() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td, "project")
        project.mkdir()
        (project / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
        state = server.State(Path(td, "state"))
        assert state.ghost_work_queue is not None
        checkpoints = WorkCheckpointStore(state.state_home)
        checkpoint = checkpoints.start(
            run_id="run-old",
            session_id="s1",
            project=project,
            task="Fix the failing parser test",
        )
        checkpoints.set_status(checkpoint, "interrupted", "error")
        state.ghost_work_queue.sync_from_sources(
            work_checkpoint_store=checkpoints,
            session_id="s1",
            project=str(project),
        )
        # Real observable facts (edit + passing check): under 0.4.13 a
        # claimed green with no local observation would block this run.
        def _fake_agent_run(_provider, _project, _task, **kwargs):
            from codey.events import RunEvent
            from codey.models import ToolCall
            from codey.tool_runtime import ToolOutcome

            kwargs["on_event"](RunEvent.tool_finished(
                1,
                ToolCall("edit", {"path": "app.py", "old_string": "1", "new_string": "2"}),
                ToolOutcome("edited", True, changed=True),
            ))
            kwargs["on_event"](RunEvent.tool_finished(
                2,
                ToolCall("run", {"command": "python -m pytest", "path": "."}),
                ToolOutcome("all passed", True, exit_code=0),
            ))
            return RunResult("fixed", "done", 2, changed=True, checks_passed=True)

        agent_run = mock.Mock(side_effect=_fake_agent_run)
        runner = _runner(
            state,
            agent_run=agent_run,
            collect_changes=_reviewable_changes,
            router_provider_factory=mock.Mock(side_effect=AssertionError("router should be bypassed")),
        )
        events = state.subscribe()

        with mock.patch.object(state, "get_provider", return_value=_Provider()):
            runner.run(TaskRequest("s1", str(project), "continue", 8, False, "deepseek"))
            state.wait_for_ghost_sleep(timeout=2)
        emitted = []
        while not events.empty():
            emitted.append(events.get_nowait())
        start = next(event for event in emitted if event.get("type") == "task_start")
        item = state.ghost_work_queue.list_items()[0]

    assert state.last_terminal_event["mode"] == "agent"
    assert start["continue_task"] is True
    assert "Continue this saved local task" in start["task"]
    assert agent_run.called
    assert "Continue this saved local task" in agent_run.call_args.args[2]
    assert "Fix the failing parser test" in agent_run.call_args.args[2]
    assert item.status == "done"


def test_project_followup_without_proof_blocks_item() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td, "project")
        project.mkdir()
        state = server.State(Path(td, "state"))
        assert state.ghost_work_queue is not None
        checkpoints = WorkCheckpointStore(state.state_home)
        checkpoint = checkpoints.start(
            run_id="run-old",
            session_id="s1",
            project=project,
            task="Fix the failing parser test",
        )
        checkpoints.set_status(checkpoint, "interrupted", "error")
        state.ghost_work_queue.sync_from_sources(
            work_checkpoint_store=checkpoints,
            session_id="s1",
            project=str(project),
        )
        runner = _runner(
            state,
            agent_run=mock.Mock(return_value=RunResult("done", "done", 1, changed=False, checks_passed=False)),
            collect_changes=_empty_changes,
            router_provider_factory=mock.Mock(side_effect=AssertionError("router should be bypassed")),
        )

        with mock.patch.object(state, "get_provider", return_value=_Provider()):
            runner.run(TaskRequest("s1", str(project), "continue", 8, False, "deepseek"))
            state.wait_for_ghost_sleep(timeout=2)
        item = state.ghost_work_queue.list_items()[0]

    assert state.last_terminal_event["mode"] == "agent"
    assert item.status == "blocked"
    assert item.blocked_reason == "missing_proof"


def test_review_item_consumes_into_review_without_writer() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td, "project")
        project.mkdir()
        state = server.State(Path(td, "state"))
        _seed_review_item(state, project)
        agent_run = mock.Mock(return_value=RunResult("should not run", "done", 1))
        run_review = mock.Mock(return_value=("qwen", ReviewResult("approved", "Looks good", [])))
        runner = _runner(
            state,
            agent_run=agent_run,
            collect_changes=_reviewable_changes,
            run_review=run_review,
            router_provider_factory=mock.Mock(side_effect=AssertionError("router should be bypassed")),
        )

        with mock.patch.object(
            state,
            "get_provider",
            side_effect=AssertionError("review should not connect main provider"),
        ):
            runner.run(TaskRequest("s1", str(project), "下一个", 8, False, "deepseek"))
            state.wait_for_ghost_sleep(timeout=2)
        item = state.ghost_work_queue.list_items()[0]

    agent_run.assert_not_called()
    run_review.assert_called_once()
    assert state.last_terminal_event["mode"] == "review"
    assert item.status == "done"


def test_no_queued_item_falls_back_to_router() -> None:
    with tempfile.TemporaryDirectory() as td:
        state = server.State(td)
        route_provider = _Provider('{"mode":"chat","confidence":0.99,"reason":"chat"}')
        router_factory = mock.Mock(return_value=route_provider)
        runner = _runner(state, router_provider_factory=router_factory)

        with mock.patch.object(state, "get_provider", return_value=_Provider("plain chat")):
            runner.run(TaskRequest("s1", None, "继续", 8, False, "deepseek"))
            state.wait_for_ghost_sleep(timeout=2)

    router_factory.assert_called_once()
    assert state.last_terminal_event["mode"] == "chat"


def test_claimed_item_is_released_on_stop() -> None:
    with tempfile.TemporaryDirectory() as td:
        state = server.State(td)
        _seed_research_item(state)
        provider = _Provider()
        runner = _runner(state)
        runner._run_research_iteration = mock.Mock(side_effect=server.cancellation.TaskCancelled("stop"))

        with mock.patch.object(state, "get_provider", return_value=provider):
            runner.run(TaskRequest("s1", None, "继续", 8, False, "deepseek"))
            state.wait_for_ghost_sleep(timeout=2)
        item = state.ghost_work_queue.list_items()[0]

    assert state.last_terminal_event["stop_reason"] == "stopped"
    assert item.status == "queued"
