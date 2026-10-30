"""Research invocations resume from original receipts through the sole driver."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


def _run(root: Path, boundary: str = "", resume: bool = False, failure: Exception | None = None,
         cancel_on_followup: bool = False):
    import os
    from types import SimpleNamespace
    from unittest.mock import patch

    from codey.durable import DurableSession, SQLiteDurableStore
    from codey.durable.checkpoint import operation_from_checkpoint
    from codey.knowledge.store import KnowledgeStore
    from codey.operations.research_flow import ResearchFlowDeps, run_research_pipeline
    from codey.policies.task_policy import TaskPolicy
    from codey.research.context import ResearchContext, ResearchPipelineConfig
    from codey.research.pipeline import ResearchPipeline
    from codey.research.query_planner import QueryCandidate, ResearchPlan
    from codey.runtime.write.mutation_line import RuntimeMutationLine
    from codey.storage.managed_outputs import ManagedOutputStore

    durable = DurableSession(SQLiteDurableStore(root / "state.sqlite"))
    if resume:
        durable.recover_scheduler_state()
        if durable.task("r").status == "pending":
            durable.claim_task("r")
    else:
        durable.admit_input(conversation_id="s", request_id="input", task_id="r", text="research recovery",
                            provider_snapshot={"provider_id": "local"})
        durable.claim_task("r")
        durable.start_task("r", state=dict(durable.task("r").state))
    line = RuntimeMutationLine(durable=durable)
    if not resume:
        line.accept_operation(session_id="s", run_id="r", provider_id="local", turn_budget=12,
                              max_repair_rounds=0, task_kind="research")

    def state():
        return operation_from_checkpoint(durable.task("r"), "s")

    def append(event, **row):
        with (root / "observations.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"event": event, **row}) + "\n")

    class Provider:
        native_tools = False

        def new_chat(self):
            append("new_chat")

        def send(self, prompt):
            attempt = state().writer_attempt
            append("request", attempt=attempt)
            if attempt == 1:
                return json.dumps({"tool": "web_search", "args": {"query": "initial research"}})
            if attempt > 1 and attempt % 2:
                return json.dumps({"tool": "done", "args": {"summary": "Synthesized saved evidence; gaps remain explicit."}})
            if attempt == 2 and failure is not None:
                raise failure
            if attempt == 2 and cancel_on_followup:
                durable.request_cancel("r", reason="cancel real investigation")
            round_index = 1 if attempt == 2 else 2
            url = f"https://example.com/source-{round_index}"
            rows = [json.loads(row) for row in (root / "observations.jsonl").read_text(encoding="utf-8").splitlines()]
            if not any(row.get("query") == f"followup {round_index}" for row in rows):
                return json.dumps({"tool": "web_search", "args": {"query": f"followup {round_index}"}})
            if not any(row.get("url") == url for row in rows):
                return json.dumps({"tool": "open_url", "args": {"url": url}})
            excerpt = f"Recovery evidence number {round_index} is durable."
            if store.search_notes(excerpt):
                return json.dumps({"tool": "done", "args": {"summary": "Evidence saved."}})
            import hashlib

            from codey.research.passages import passage_ref

            ref = passage_ref(url, hashlib.sha256(excerpt.encode()).hexdigest()[:16], 0, len(excerpt))
            return json.dumps({"tool": "save_finding", "args": {
                "claim": excerpt, "source_ref": "s1", "passage_ref": ref}})

    class Search:
        def search(self, query, limit=8):
            append("search", query=query)
            if query == "initial research":
                return []
            number = query.rsplit(" ", 1)[-1]
            return [{"title": f"Source {number}", "url": f"https://example.com/source-{number}",
                     "snippet": f"Recovery evidence number {number} is durable."}]

        def fetch(self, url):
            append("fetch", url=url)
            number = url.rsplit("-", 1)[-1]
            return {"status": "ok", "url": url, "title": f"Source {number}",
                    "text": f"Recovery evidence number {number} is durable.", "truncated": False}

        def close(self):
            pass

    store = KnowledgeStore(root / "knowledge")
    original_write = store.write_note

    def write(note, *, changes=None):
        result = original_write(note, changes=changes)
        append("write", identity=note.id)
        if boundary == "vault" and note.type == "fact":
            os._exit(74)
        return result

    store.write_note = write
    original_publish = store.publish_note

    def publish(note, *args, **kwargs):
        result = original_publish(note, *args, **kwargs)
        if boundary == "synthesis_published" and note.type == "synthesis":
            os._exit(74)
        return result

    store.publish_note = publish
    if boundary and boundary != "vault":
        def committed(_result, _mutation):
            position = state()
            publications = [effect for effect in durable.effects("r")
                            if effect.intent.get("tool_name") == "_publish_research_answer"]
            if publications and ((boundary == "publication_admitted" and publications[-1].settlement is None)
                                 or (boundary == "publication_settled" and publications[-1].settlement is not None)):
                os._exit(74)
            if ((boundary == "material" and position.writer_attempt == 1 and position.continuation_ref)
                    or (boundary == "response" and position.writer_attempt == 2
                        and position.logical_turn >= 3 and position.leaf == "provider_response_ready")
                    or (boundary == "receipt" and position.writer_attempt == 2
                        and position.logical_turn >= 3 and position.leaf == "tool_delivery_pending")
                    or (boundary == "second_round" and position.writer_attempt == 4
                        and position.leaf == "provider_response_ready")
                    or (boundary == "synthesis_response" and position.writer_attempt == 5
                        and position.leaf == "provider_response_ready")
                    or (boundary == "synthesis_settled" and position.writer_attempt == 5
                        and position.leaf == "kernel_settled")):
                os._exit(74)
        durable.subscribe_commits(committed)

    policy = TaskPolicy(frozenset({"control", "web.read", "knowledge.read", "knowledge.write"}))
    deps = ResearchFlowDeps(state=SimpleNamespace(durable=durable), knowledge_store=store,
        evidence_ledgers=None, search_factory=Search, run_research_advisors=None, ghost_continuity=lambda **_: None,
        managed_outputs=ManagedOutputStore(root), runtime_mutations=line)
    frame = SimpleNamespace(provider=Provider(), provider_id="local", project_text="", trace=None,
        run_id="r", research_handoff="", entry_policy=policy,
        request=SimpleNamespace(task="research recovery", session_id="s", requested_capabilities=()))
    hooks = SimpleNamespace(on_event=lambda event: None)

    def context(*args, **kwargs):
        return ResearchContext(question="research recovery", session_id="s", run_id="r", max_turns=1)

    def review(self, result, *, require_ledger_record):
        # Keep acquisition deterministic across restarts while exercising the
        # real merge, staging, material acquisition and invocation machinery.
        return SimpleNamespace(count=len(result.evidence_items))

    def plan(self, review):
        number = review.count + 1
        return ResearchPlan(plan_ref=f"research_plan:{number:016x}",
            query_candidates=(QueryCandidate(f"research_query:{number:016x}", f"followup {number}"),),
            max_queries=1, max_sources=1)

    try:
        with (patch.object(ResearchPipeline, "_review", review),
              patch.object(ResearchPipeline, "_plan", plan),
              patch("codey.operations.research_flow.ResearchPipelineConfig",
                    return_value=ResearchPipelineConfig(max_followup_rounds=2)),
              patch.object(ResearchPipeline, "_followup_block_reason",
                  lambda self, result, review, plan: "sufficient" if review.count >= 2 else ""),
              patch("codey.research.followup_selection.selects_candidate", return_value=True)):
            result = run_research_pipeline(deps, frame, hooks, max_turns=1, build_context=context)
        return result, state()
    finally:
        store.index.close()
        durable.close()


@pytest.mark.parametrize("boundary", ["material", "response", "receipt", "vault", "second_round",
                                       "synthesis_response", "synthesis_settled", "synthesis_published",
                                       "publication_admitted", "publication_settled"])
def test_research_restart_preserves_material_invocations_and_note_ids(tmp_path, boundary):
    source = ("from pathlib import Path; from tests.durable.test_research_continuation_restart import _run; "
              "import sys; _run(Path(sys.argv[1]), sys.argv[2])")
    child = subprocess.run([sys.executable, "-c", source, str(tmp_path), boundary],
                           capture_output=True, timeout=50)
    assert child.returncode == 74, child.stderr.decode(errors="replace")
    first, state = _run(tmp_path, resume=True)
    assert first.followup_rounds == 2, first
    assert first.final_result.stop_reason == "done"
    assert first.final_result.summary == "Synthesized saved evidence; gaps remain explicit."
    assert first.final_result.completion_proof is not None
    assert first.new_evidence_count == 2
    assert len(first.final_result.notes_created) == 3
    before = (tmp_path / "observations.jsonl").read_bytes()
    notes = {str(path): path.read_bytes() for path in (tmp_path / "knowledge").rglob("*.md")}
    second, again = _run(tmp_path, resume=True)
    assert second == first
    assert again == state
    assert (tmp_path / "observations.jsonl").read_bytes() == before
    assert {str(path): path.read_bytes() for path in (tmp_path / "knowledge").rglob("*.md")} == notes
    observations = [json.loads(row) for row in before.splitlines()]
    assert [row["attempt"] for row in observations if row["event"] == "request"] == [1, 2, 2, 2, 2, 4, 4, 4, 4, 5]
    assert state.turns_used + state.logical_turn == 10
    assert [row["query"] for row in observations if row["event"] == "search"] == [
        "initial research", "followup 1", "followup 2"]
    assert [row["url"] for row in observations if row["event"] == "fetch"] == [
        "https://example.com/source-1", "https://example.com/source-2"]
    assert set(row["identity"] for row in observations if row["event"] == "write") == set(first.final_result.notes_created)
    from codey.durable import DurableSession, SQLiteDurableStore
    from codey.knowledge.store import KnowledgeStore

    durable = DurableSession(SQLiteDurableStore(tmp_path / "state.sqlite"))
    try:
        publications = [effect for effect in durable.effects("r")
                        if effect.intent.get("tool_name") == "_publish_research_answer"]
        assert len(publications) == 1 and publications[0].settlement["status"] == "ok"
        assert len(durable.artifacts_for_effect(publications[0].effect_id)) >= 1
    finally:
        durable.close()
    store = KnowledgeStore(tmp_path / "knowledge")
    try:
        assert store.index._read_conn().execute("SELECT COUNT(*) FROM knowledge_versions WHERE kind='answer'").fetchone()[0] == 1
    finally:
        store.close()


def test_ordinary_publication_rejects_changed_inputs_before_any_new_write(tmp_path, monkeypatch):
    from codey.operations import research_publication
    from codey.research.pipeline import ResearchContinuationError

    original = research_publication.publish_synthesis

    def publish(history, tools, task, summary, **kwargs):
        identity = original(history, tools, task, summary, **kwargs)
        for altered in ({"project": "other-scope"}, {"knowledge_refs": [{"id": "other", "revision": 2}]},
                        {"open_questions": ["Changed question"]}):
            with pytest.raises(ResearchContinuationError, match="frozen inputs"):
                original(history, tools, task, summary, **{**kwargs, **altered})
        return identity

    monkeypatch.setattr(research_publication, "publish_synthesis", publish)
    result, _ = _run(tmp_path)
    assert result.final_result.stop_reason == "done"


def test_ordinary_publication_cancelled_before_admission_never_writes_answer(tmp_path, monkeypatch):
    from codey.knowledge.store import KnowledgeStore
    from codey.runtime.core.cancellation import TaskCancelled
    from codey.runtime.write.mutation_line import RuntimeMutationLine

    original = RuntimeMutationLine.record_research_publication

    def cancel(self, session_id, run_id, **kwargs):
        if kwargs.get("intent") is not None:
            self.durable.request_cancel(run_id, reason="cancel before answer publication")
        return original(self, session_id, run_id, **kwargs)

    monkeypatch.setattr(RuntimeMutationLine, "record_research_publication", cancel)
    with pytest.raises(TaskCancelled):
        _run(tmp_path)
    store = KnowledgeStore(tmp_path / "knowledge")
    try:
        assert store.index._read_conn().execute("SELECT COUNT(*) FROM knowledge_versions WHERE kind='answer'").fetchone()[0] == 0
    finally:
        store.close()


def _settle_production_cancel(root, *, kind="research", publication_deps=True):
    from types import SimpleNamespace

    from codey.durable import DurableSession, SQLiteDurableStore
    from codey.durable.checkpoint import operation_from_checkpoint
    from codey.knowledge.store import KnowledgeStore
    from codey.operations.context import RunWork
    from codey.operations.ghost_post_turn import GhostTaskPolicyDeps
    from codey.operations.task_phases.settlement import settle_cancelled_run
    from codey.runtime.observe.execution_evidence import ExecutionEvidence
    from codey.runtime.write.mutation_line import RuntimeMutationLine
    from codey.storage.managed_outputs import ManagedOutputStore

    durable = DurableSession(SQLiteDurableStore(root / "state.sqlite"))
    store = KnowledgeStore(root / "knowledge")
    events = []
    state = SimpleNamespace(durable=durable, set_provider_session=lambda *_: None,
                            finish_run=lambda _, event: events.append(event))
    line = RuntimeMutationLine(durable=durable)
    deps = SimpleNamespace(state=state, runtime_mutations=line,
                           managed_outputs=ManagedOutputStore(root) if publication_deps else None,
                           knowledge_store=store if publication_deps else None, run_ledgers=None, work_checkpoints=None)
    try:
        position = operation_from_checkpoint(durable.task("r"), "s")
        work = RunWork(recent_events=[], evidence=ExecutionEvidence(), operation=position)
        result = settle_cancelled_run(deps, state, work, GhostTaskPolicyDeps(state=state),
            run_id="r", session_id="s", max_turns=12, provider_id="local", task_kind=kind,
            project=None, frame=None, finish_trace=lambda _: None)
        assert result.status == "aborted"
        assert "runtime_settlement_error" not in events[-1]
        assert str(durable.task("r").status) == "cancelled"
        assert operation_from_checkpoint(durable.task("r"), "s").leaf == "terminal"
        assert all(effect.settlement is not None for effect in durable.effects("r"))
        return durable.effects("r")
    finally:
        store.close()
        durable.close()


@pytest.mark.parametrize("boundary", ["publication_admitted", "synthesis_published", "publication_settled"])
@pytest.mark.parametrize("resume_pipeline", [False, True])
def test_crashed_publication_cancel_settles_original_transaction_without_new_work(tmp_path, boundary, resume_pipeline):
    from codey.durable import DurableSession, SQLiteDurableStore
    from codey.knowledge.store import KnowledgeStore
    from codey.runtime.core.cancellation import TaskCancelled

    source = ("from pathlib import Path; from tests.durable.test_research_continuation_restart import _run; "
              "import sys; _run(Path(sys.argv[1]), sys.argv[2])")
    child = subprocess.run([sys.executable, "-c", source, str(tmp_path), boundary],
                           capture_output=True, timeout=50)
    assert child.returncode == 74, child.stderr.decode(errors="replace")
    before = (tmp_path / "observations.jsonl").read_bytes()
    notes = {str(path): path.read_bytes() for path in (tmp_path / "knowledge").rglob("*.md")}
    durable = DurableSession(SQLiteDurableStore(tmp_path / "state.sqlite"))
    try:
        original = next(effect for effect in durable.effects("r")
                        if effect.intent.get("tool_name") == "_publish_research_answer")
        durable.request_cancel("r", reason="cancel after publication crash")
        durable.recover_scheduler_state()
    finally:
        durable.close()
    if resume_pipeline:
        expected = "research synthesis stopped" if boundary == "publication_settled" else "publication settled"
        with pytest.raises(TaskCancelled, match=expected):
            _run(tmp_path, resume=True)
    effects = _settle_production_cancel(tmp_path)
    publication = next(effect for effect in effects if effect.effect_id == original.effect_id)
    assert publication.intent == original.intent
    assert publication.settlement["status"] == ("interrupted" if boundary == "publication_admitted" else "ok")
    if boundary == "publication_admitted":
        assert publication.settlement["error_code"] == "cancelled"
    if original.settlement is not None:
        assert publication.settlement == original.settlement
    # Repeated production cleanup must retain the exact same facts and receipt.
    assert _settle_production_cancel(tmp_path) == effects
    assert (tmp_path / "observations.jsonl").read_bytes() == before
    assert {str(path): path.read_bytes() for path in (tmp_path / "knowledge").rglob("*.md")} == notes
    store = KnowledgeStore(tmp_path / "knowledge")
    try:
        assert store.index._read_conn().execute(
            "SELECT COUNT(*) FROM knowledge_versions WHERE kind='answer'").fetchone()[0] == (
                0 if boundary == "publication_admitted" else 1)
    finally:
        store.close()


@pytest.mark.parametrize("kind", ["coding", "research"])
def test_production_cancel_without_publication_needs_no_research_dependencies(tmp_path, kind):
    from codey.durable import DurableSession, SQLiteDurableStore
    from codey.runtime.write.mutation_line import RuntimeMutationLine

    durable = DurableSession(SQLiteDurableStore(tmp_path / "state.sqlite"))
    try:
        durable.admit_input(conversation_id="s", request_id="input", task_id="r", text="coding", provider_snapshot={})
        durable.claim_task("r")
        durable.start_task("r", state=dict(durable.task("r").state))
        line = RuntimeMutationLine(durable=durable)
        line.accept_operation(session_id="s", run_id="r", provider_id="local", turn_budget=12,
                              max_repair_rounds=0, task_kind=kind)
        line.mark_writer_running("s", "r", provider_id="local", writer_attempt=1)
        durable.request_cancel("r", reason="cancel coding")
    finally:
        durable.close()
    _settle_production_cancel(tmp_path, kind=kind, publication_deps=False)


def test_ordinary_research_accepts_no_stop_flag(tmp_path, monkeypatch):
    from codey.operations import research_iteration

    original = research_iteration.run_research_iteration

    def without_flag(*args, **kwargs):
        return original(*args, **{**kwargs, "stop_flag": None})

    monkeypatch.setattr(research_iteration, "run_research_iteration", without_flag)
    result, _ = _run(tmp_path)
    assert result.final_result.stop_reason == "done"


def test_lost_continuation_commit_acknowledgement_never_reacquires_material(tmp_path, monkeypatch):
    from codey.durable import DurableSession, SQLiteDurableStore
    from codey.research.pipeline import ResearchContinuationError
    from codey.runtime.write.mutation_line import RuntimeMutationLine

    original = RuntimeMutationLine.commit_research_continuation
    interrupted = []

    def commit(self, *args, **kwargs):
        original(self, *args, **kwargs)
        if not interrupted:
            interrupted.append(True)
            raise OSError("acknowledgement lost after commit")

    monkeypatch.setattr(RuntimeMutationLine, "commit_research_continuation", commit)
    with pytest.raises(ResearchContinuationError, match="acknowledgement lost after commit"):
        _run(tmp_path)
    # This injected failure unwound the worker in our own still-live pytest
    # process. Explicitly retire that stopped owner before claiming a new one.
    durable = DurableSession(SQLiteDurableStore(tmp_path / "state.sqlite"))
    try:
        durable.recover_scheduler_state(force=True)
    finally:
        durable.close()
    result, _ = _run(tmp_path, resume=True)
    assert result.followup_rounds == 2
    observations = [json.loads(row) for row in (tmp_path / "observations.jsonl").read_bytes().splitlines()]
    assert [row["query"] for row in observations if row["event"] == "search"] == [
        "initial research", "followup 1", "followup 2"]
    assert [row["attempt"] for row in observations if row["event"] == "request"] == [1, 2, 2, 2, 2, 4, 4, 4, 4, 5]


def test_research_continuation_admission_is_idempotent(tmp_path, monkeypatch):
    from codey.runtime.write.mutation_line import RuntimeMutationLine

    original = RuntimeMutationLine.commit_research_continuation
    commits = []

    def commit(self, session_id, run_id, **kwargs):
        original(self, session_id, run_id, **kwargs)
        revision = self.durable.task(run_id).revision
        original(self, session_id, run_id, **kwargs)
        assert self.durable.task(run_id).revision == revision
        commits.append(revision)

    monkeypatch.setattr(RuntimeMutationLine, "commit_research_continuation", commit)
    result, _ = _run(tmp_path)
    assert result.followup_rounds == 2
    assert len(commits) == 5


def test_durable_cancellation_during_followup_never_returns_previous_draft_as_done(tmp_path):
    from codey.durable import DurableSession, SQLiteDurableStore
    from codey.runtime.core.cancellation import TaskCancelled

    with pytest.raises(TaskCancelled, match="research investigation stopped"):
        _run(tmp_path, cancel_on_followup=True)
    durable = DurableSession(SQLiteDurableStore(tmp_path / "state.sqlite"))
    try:
        assert durable.task("r").state["cancel_requested"]
        observations = [json.loads(row) for row in (tmp_path / "observations.jsonl").read_bytes().splitlines()]
        assert [row["attempt"] for row in observations if row["event"] == "request"] == [1, 2]
        assert not any(row["event"] in {"fetch", "write"} for row in observations)
    finally:
        durable.close()


@pytest.mark.parametrize("not_sent", [True, False])
def test_failed_followup_retains_original_attempt_and_provider_diagnostic(tmp_path, not_sent):
    from codey.durable import DurableSession, SQLiteDurableStore
    from codey.durable.checkpoint import operation_from_checkpoint
    from codey.providers.error_classification import RequestPrepError
    from codey.research.pipeline import ResearchContinuationError

    failure = RequestPrepError("followup preparation failed") if not_sent else OSError("followup outcome unknown")
    with pytest.raises(ResearchContinuationError, match="provider failed.*followup"):
        _run(tmp_path, failure=failure)
    durable = DurableSession(SQLiteDurableStore(tmp_path / "state.sqlite"))
    try:
        position = operation_from_checkpoint(durable.task("r"), "s")
        assert position.writer_attempt == 2
        assert position.leaf != "kernel_settled" and position.continuation_ref
        failed = durable.effects("r")[-1]
        assert failed.settlement["sent_state"] == ("not_sent" if not_sent else "maybe_sent")
        durable.recover_scheduler_state(force=True)
    finally:
        durable.close()
    before = (tmp_path / "observations.jsonl").read_bytes()
    assert [row["attempt"] for row in map(json.loads, before.splitlines()) if row["event"] == "request"] == [1, 2]
    if not_sent:
        result, _ = _run(tmp_path, resume=True)
        assert result.followup_rounds == 2
        observations = [json.loads(row) for row in (tmp_path / "observations.jsonl").read_bytes().splitlines()]
        # The failed logical turn retains its budget charge. Recovery closes
        # that bounded invocation before admitting the explicit repair.
        assert [row["attempt"] for row in observations if row["event"] == "request"] == [1, 2, 2, 2, 2, 4, 4, 4, 4, 5]
        assert [row["query"] for row in observations if row["event"] == "search"] == [
            "initial research", "followup 1", "followup 2"]
    else:
        for _ in range(2):
            with pytest.raises(ResearchContinuationError, match="external_outcome_unknown"):
                _run(tmp_path, resume=True)
            assert (tmp_path / "observations.jsonl").read_bytes() == before
