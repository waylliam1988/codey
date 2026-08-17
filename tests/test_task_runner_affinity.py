from __future__ import annotations

import tempfile
from unittest import mock

from codey.agent import RunResult
import codey.ghost.work_queue as work_queue_module
from codey.knowledge.research_interest import ResearchInterestCandidate
from codey.research.ledger import ResearchLedger
from codey.research.object_model import build_research_record
from codey.research.report_quality import review_report_quality
from codey.research.runner import ResearchRunResult
from codey import server
from codey.task_runner import TaskRequest, TaskRunner


class _Provider:
    name = "DeepSeek Web"

    def __init__(self, reply: str = "ok", *, error: Exception | None = None) -> None:
        self.reply = reply
        self.error = error

    def new_chat(self, timeout: float | None = None) -> None:
        del timeout

    def send(self, text: str, timeout: float | None = None) -> str:
        del timeout
        if self.error is not None:
            raise self.error
        return self.reply

    def close(self) -> None:
        pass


def _runner(state: server.State, *, agent_run=None, router_provider_factory=None) -> TaskRunner:
    return TaskRunner(
        state,
        agent_run=agent_run or mock.Mock(return_value=RunResult("done", "done", 1)),
        collect_changes=lambda *_args, **_kwargs: {"ok": True, "changed_count": 0, "files": [], "diff": ""},
        run_review=mock.Mock(return_value=None),
        capture_provider_failure=server.capture_provider_failure,
        project_facts=state.project_facts,
        work_checkpoints=state.work_checkpoints,
        run_ledgers=state.run_ledgers,
        run_traces=state.run_traces,
        evidence_ledgers=state.evidence_ledgers,
        managed_outputs=state.managed_outputs,
        knowledge_store=state.knowledge_store,
        is_git_repository=lambda _project: True,
        ghost_router_provider_factory=router_provider_factory,
    )


def _research_candidate(concept: str) -> ResearchInterestCandidate:
    return ResearchInterestCandidate(
        id=f"ric-{concept}",
        question=f"Research whether {concept} should be tracked",
        related_concepts=(concept,),
        shared_neighbors=(),
        source_refs=(f"note:{concept}",),
        scope="session",
        scope_ref="s1",
        priority=0.72,
        confidence=0.85,
        why_now="Bounded local test.",
        source="concept_open_question",
        source_ref=f"concept:{concept}",
        strong_support=True,
    )


def _queued_research_item(*, title: str, concept: str, priority: float):
    return work_queue_module._new_item(
        kind="research",
        status="queued",
        scope="session",
        scope_ref=work_queue_module._session_ref("s1"),
        title=title,
        why_now="Bounded queued research item.",
        priority=priority,
        confidence=0.86,
        source="research_interest",
        source_ref=f"seed:{concept}",
        evidence_refs=(f"research_interest:{concept}",),
        run_refs=(),
        now="2999-01-01T00:00:00Z",
        metadata={"related_concepts": [concept]},
    )


def _research_record(concept: str):
    url = f"https://example.com/{concept}-provider-recovery"
    claim = f"{concept} provider recovery should be tracked with opened evidence."
    source_text = f"{claim} 2026 source note."
    summary = (
        "## 结论\n"
        f"- {claim} [1]\n\n"
        "## 关键证据\n"
        f"- [1] The opened source says {claim}\n\n"
        "## 反证与限制\n"
        "- 未找到强反证；需要持续追踪新的 provider recovery evidence。\n\n"
        "## 来源质量\n"
        "- [1] secondary · web · fresh · example.com\n\n"
        "## 搜索覆盖\n"
        f"- query: Research {concept} provider recovery\n"
        f"- opened: {concept} provider recovery article\n"
        "- skipped: none representative\n\n"
        "## 来源\n"
        f"[1] {concept} provider recovery article - {url}"
    )
    ledger = ResearchLedger()
    ledger.record_search(f"{concept} provider recovery", [{
        "title": f"{concept} provider recovery article",
        "url": url,
        "snippet": claim,
    }])
    ledger.record_open(
        requested_url=url,
        final_url=url,
        title=f"{concept} provider recovery article",
        text=source_text,
    )
    prepared = ledger.prepare_evidence_items(
        [{
            "claim": claim,
            "source_url": url,
            "excerpt": claim,
            "stance": "supports",
        }],
        fallback_sources=[url],
        fallback_claim=claim,
        fallback_body=source_text,
        note_type="fact",
    )
    assert not prepared.error
    ledger.add_evidence_items(list(prepared.items), note_id=f"{concept}-result")
    quality = review_report_quality(
        summary,
        ledger=ledger,
        opened_sources=ledger.final_url_set(),
        search_result_urls={url},
    )
    assert quality.ok
    return build_research_record(
        question=f"Research {concept} provider recovery",
        summary=summary,
        ledger=ledger,
        review=quality,
        run_id=f"run-{concept}",
        session_id="s1",
        synthesis_id=f"{concept}-result",
        stop_reason="done",
    )


def test_task_runner_uses_affinity_to_order_strict_continue_work_items() -> None:
    with tempfile.TemporaryDirectory() as td:
        state = server.State(td)
        assert state.ghost_work_queue is not None
        assert state.ghost_affinity is not None
        favored = _queued_research_item(title="Research alpha provider recovery", concept="alpha", priority=0.50)
        baseline_top = _queued_research_item(title="Research beta provider recovery", concept="beta", priority=0.52)
        assert state.ghost_work_queue._replace_items([favored, baseline_top], "test_seed_affinity_order")
        state.ghost_affinity.sync_from_sources(
            research_interest_candidates=(_research_candidate("alpha"),),
            session_id="s1",
        )
        runner = _runner(
            state,
            router_provider_factory=mock.Mock(side_effect=AssertionError("router should be bypassed")),
        )
        runner._run_research_task = mock.Mock(return_value=ResearchRunResult(
            "Research alpha provider recovery",
            "researched",
            "done",
            1,
            synthesis_id="alpha-result",
            citation_map=[{"claim": "x"}],
            research_record=_research_record("alpha"),
        ))

        with mock.patch.object(state, "get_provider", return_value=_Provider()):
            runner.run(TaskRequest("s1", None, "continue", 8, False, "deepseek"))
            state.wait_for_ghost_sleep(timeout=2)
        items = {item.id: item for item in state.ghost_work_queue.list_items(session_id="s1")}

    assert runner._run_research_task.call_count == 1
    assert items[favored.id].status == "done"
    assert items[baseline_top.id].status == "queued"


def test_task_runner_syncs_affinity_after_turn_from_local_sources() -> None:
    from codey.ghost.schema import GhostSignal, GhostSignalParseResult

    with tempfile.TemporaryDirectory() as td:
        state = server.State(td)
        assert state.ghost_inbox is not None
        assert state.ghost_hebbian is not None
        assert state.ghost_affinity is not None
        created = state.ghost_inbox.ingest_signals(
            GhostSignalParseResult(signals=(GhostSignal(
                kind="style_preference",
                scope="user",
                summary="Prefer concise replies.",
                evidence_quote="short please",
                confidence=0.9,
                metadata={"conflict_key": "reply_length", "value_key": "concise"},
                source="test",
            ),), ok=True, provider_id="test"),
            session_id="s1",
            run_id="r1",
            user_text="short please",
        )
        reviewed = state.ghost_inbox.review_candidate(created[0].id, "accept", reviewed_by="test")
        assert reviewed is not None
        state.ghost_hebbian.reinforce_candidate(reviewed)
        runner = _runner(state, router_provider_factory=None)

        with mock.patch.object(state, "get_provider", return_value=_Provider("plain chat")):
            runner.run(TaskRequest("s1", None, "hello", 8, False, "deepseek"))
            state.wait_for_ghost_sleep(timeout=2)
        nodes = state.ghost_affinity.list_nodes(kind="user_preference")

    assert len(nodes) == 1
    assert nodes[0].metadata["source"] == "hebbian"


def test_ghost_disable_prevents_affinity_hint_consumption() -> None:
    with tempfile.TemporaryDirectory() as td:
        state = server.State(td)
        assert state.ghost_inbox is not None
        assert state.ghost_work_queue is not None
        assert state.ghost_affinity is not None
        favored = _queued_research_item(title="Research alpha provider recovery", concept="alpha", priority=0.50)
        baseline_top = _queued_research_item(title="Research beta provider recovery", concept="beta", priority=0.52)
        assert state.ghost_work_queue._replace_items([favored, baseline_top], "test_seed_disabled_order")
        state.ghost_affinity.sync_from_sources(
            research_interest_candidates=(_research_candidate("alpha"),),
            session_id="s1",
        )
        state.ghost_inbox.set_learning_enabled(False)
        runner = _runner(state, router_provider_factory=None)

        with mock.patch.object(state, "get_provider", return_value=_Provider("chat")):
            runner.run(TaskRequest("s1", None, "continue", 8, False, "deepseek"))
            state.wait_for_ghost_sleep(timeout=2)
        items = {item.id: item for item in state.ghost_work_queue.list_items(session_id="s1")}

    assert items[favored.id].status == "queued"
    assert items[baseline_top.id].status == "queued"


def test_provider_failure_exception_path_syncs_affinity_behavior() -> None:
    with tempfile.TemporaryDirectory() as td:
        state = server.State(td)
        assert state.ghost_affinity is not None
        runner = _runner(state, router_provider_factory=None)

        with mock.patch.object(
            state,
            "get_provider",
            return_value=_Provider(error=RuntimeError("raw sk-test-secret provider failure")),
        ):
            runner.run(TaskRequest("s1", None, "hello", 8, False, "deepseek"))
            state.wait_for_ghost_sleep(timeout=2)
        nodes = state.ghost_affinity.list_nodes(kind="provider_behavior", session_id="s1")
        raw = state.ghost_affinity.events_path.read_text(encoding="utf-8")

    assert nodes
    assert "transient" in raw
    assert "sk-test-secret" not in raw
