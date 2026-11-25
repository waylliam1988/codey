from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import mock

from codey.agents.handoff import ConversationContext, ConversationSnapshot
from codey.app import server
from codey.knowledge.note import KnowledgeNote
from codey.knowledge.store import KnowledgeStore
from codey.operations.context import RunFrame
from codey.operations.research_flow import (
    ResearchFlowDeps,
    build_research_context,
    build_research_topic_continuity,
    recent_claim_refs,
)
from codey.task.model import TaskSubmission

_QUESTION = "Should provider recovery be re-checked against fresh sources?"


def _deps(state: server.AppContext) -> ResearchFlowDeps:
    return ResearchFlowDeps(state=state, knowledge_store=state.knowledge_store,
                            search_factory=object)


def _make_frame(project_text: str) -> RunFrame:
    request = TaskSubmission(
        session_id="s1",
        project=None,
        task="research the continuity question",
        max_turns=8,
        continue_task=False,
        provider_id="deepseek",
    )
    return RunFrame(
        request=request,
        run_id="run-topic-continuity",
        task_kind="research",
        provider=None,
        provider_id="deepseek",
        project_text=project_text,
        conversation=ConversationContext(),
        fresh_chat=True,
        handoff="",
        research_handoff="",
        prior_snapshot=ConversationSnapshot(mode="research"),
        recovered_owner_prompt="",
        provider_session_changed=False,
        preflight_tried=set(),
        preflight_switches=0,
    )




def _seed_open_question_note(store: KnowledgeStore, *, session_id: str) -> None:
    store.write_note(KnowledgeNote.create(
        id=f"synthesis-continuity-{session_id}",
        type="synthesis",
        title="Provider recovery synthesis",
        body="Research synthesis with an open follow-up question.",
        open_questions=[_QUESTION],
        tags=["research"],
        session_id=session_id,
    ))


def _seed_prior_claim(state: server.AppContext, *, session_id: str, project: str) -> None:
    _seed_prior_claim_overflow(state, session_id=session_id, project=project, count=1)


def _seed_prior_claim_overflow(
    state: server.AppContext, *, session_id: str, project: str, count: int,
) -> None:
    from codey.knowledge.model import KnowledgeMutation, KnowledgeObject

    store = state.knowledge_store
    assert store is not None
    claims = [KnowledgeMutation(KnowledgeObject(
        id=f"claim-{session_id}-{index}", kind="claim", content=f"Prior claim {index}",
        origin="user_statement", scope="session", scope_id=session_id,
    ), 0) for index in range(count)]
    store.commit_knowledge(f"prior-{session_id}", claims, project=project, session_id=session_id)


def test_prior_claim_overflow_reports_truncated_honestly() -> None:
    from codey.research.topic_continuity import MAX_TOPIC_CLAIM_REFS

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        project = str(root / "project")
        (root / "project").mkdir()
        state = server.AppContext(root / "state")
        state.knowledge_store = KnowledgeStore(root / "knowledge")
        _seed_prior_claim_overflow(
            state,
            session_id="s1",
            project=project,
            count=MAX_TOPIC_CLAIM_REFS + 2,
        )
        deps = _deps(state)

        refs = recent_claim_refs(state.knowledge_store, session_id="s1", project=project)
        _text, payload = build_research_topic_continuity(
            deps,
            session_id="s1",
            project=project,
        )

    # The extraction keeps the overflow signal (MAX+1) so the projection's
    # truncated verdict is honest even though production caps the carried
    # refs before projecting.
    assert len(refs) == MAX_TOPIC_CLAIM_REFS + 1
    assert payload is not None
    assert payload["truncated"] is True
    assert payload["claim_ref_count"] == MAX_TOPIC_CLAIM_REFS


def test_prior_claims_enter_as_stale_refs_never_evidence() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        project = str(root / "project")
        (root / "project").mkdir()
        state = server.AppContext(root / "state")
        state.knowledge_store = KnowledgeStore(root / "knowledge")
        _seed_prior_claim(state, session_id="s1", project=project)
        text, payload = build_research_topic_continuity(
            _deps(state),
            session_id="s1",
            project=project,
        )

    prior_rows = [
        row for row in (payload or {}).get("items", [])
        if row["kind"] == "prior_claim"
    ]
    assert prior_rows, "prior ledger claims must be carried as refs"
    assert all(row["stale"] is True for row in prior_rows)
    assert all("prior_claim_needs_recheck" in row["reason_codes"] for row in prior_rows)
    raw = json.dumps(payload or {})
    assert "evidence_refs" not in raw
    assert "re-check" in text


def test_closed_profile_gate_returns_empty_baseline() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        state = server.AppContext(root / "state")
        state.knowledge_store = KnowledgeStore(root / "knowledge")
        _seed_open_question_note(state.knowledge_store, session_id="s1")
        with mock.patch(
            "codey.operations.research_flow.allows_context_source",
            return_value=False,
        ):
            text, payload = build_research_topic_continuity(
                _deps(state),
                session_id="s1",
                project=str(root / "project"),
            )

    assert text == ""
    assert payload is None


def test_cancellation_is_never_swallowed_by_the_builder() -> None:
    # Stop/cancel semantics must not depend on luck: the fail-open guard
    # covers projection failures only.
    from codey.runtime.core import cancellation

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        state = server.AppContext(root / "state")
        state.knowledge_store = KnowledgeStore(root / "knowledge")
        _seed_open_question_note(state.knowledge_store, session_id="s1")
        trace_calls: list[tuple] = []

        cancelled_claim_refs = mock.Mock(
            side_effect=cancellation.TaskCancelled("task stopped"),
        )
        try:
            build_research_topic_continuity(
                _deps(state),
                session_id="s1",
                project=str(root / "project"),
                trace=lambda *a, **k: trace_calls.append((a, k)),
                prior_claim_refs=cancelled_claim_refs,
            )
        except cancellation.TaskCancelled:
            pass
        else:
            raise AssertionError("TaskCancelled was swallowed by the builder")

    assert trace_calls == []  # no warn row: this is a stop, not a failure


def test_empty_local_state_admits_nothing() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        state = server.AppContext(root / "state")
        state.knowledge_store = KnowledgeStore(root / "knowledge")
        text, payload = build_research_topic_continuity(
            _deps(state),
            session_id="s1",
            project=str(root / "project"),
        )

    assert text == ""
    assert payload is not None
    assert payload["admitted"] is False




def test_build_research_context_baseline_without_seeds() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        state = server.AppContext(root / "state")
        state.knowledge_store = KnowledgeStore(root / "knowledge")
        frame = _make_frame(project_text=str(root / "project"))

        context = build_research_context(
            _deps(state),
            frame,
            proof_question="",
            max_turns=6,
        )

    assert context.topic_continuity_context == ""
    assert context.topic_continuity_payload is not None
    assert context.topic_continuity_payload["admitted"] is False
