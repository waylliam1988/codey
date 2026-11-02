"""Research invocations resume from original receipts through the sole driver."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


def _run(root: Path, boundary: str = "", resume: bool = False, failure: Exception | None = None,
         cancel_on_submission: bool = False):
    """Exercise the production whole-answer path, without forcing an outer planner."""
    import os
    from types import SimpleNamespace

    from codey.durable import DurableSession, SQLiteDurableStore
    from codey.durable.checkpoint import operation_from_checkpoint
    from codey.knowledge.store import KnowledgeStore
    from codey.operations.research_flow import ResearchFlowDeps, run_research_pipeline
    from codey.operations.task_entry import build_task_policy_for_entry
    from codey.research.context import ResearchContext
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
            position = state()
            assert position.writer_attempt == 1
            append("request", attempt=position.writer_attempt, turn=position.logical_turn)
            if position.logical_turn == 1:
                return json.dumps({"tool": "open_url", "args": {"url": "https://example.com/source-1"}})
            if failure is not None:
                raise failure
            if cancel_on_submission:
                durable.request_cancel("r", reason="cancel actual answer submission")
            return json.dumps({"tool": "submit_research_answer", "args": {
                "state": "answered", "markdown": "The original source states that recovery evidence is durable [e1]."}})

    class Search:
        def search(self, query, limit=8):
            raise AssertionError("The source is already supplied; no outside acquisition is required.")

        def fetch(self, url):
            append("fetch", url=url)
            return {"status": "ok", "url": url, "title": "Original recovery source",
                    "text": "Recovery evidence is durable.", "truncated": False}

        def close(self):
            pass

    store = KnowledgeStore(root / "knowledge")
    original_publish = store.publish_note

    def publish(note, *args, **kwargs):
        result = original_publish(note, *args, **kwargs)
        append("write", identity=note.id)
        if boundary == "synthesis_published":
            os._exit(74)
        return result

    store.publish_note = publish
    if boundary:
        def committed(*_):
            position = state()
            publications = [effect for effect in durable.effects("r")
                            if effect.intent.get("tool_name") == "_publish_research_answer"]
            if publications and ((boundary == "publication_admitted" and publications[-1].settlement is None)
                                 or (boundary == "publication_settled" and publications[-1].settlement is not None)):
                os._exit(74)
            leaves = {"request": "provider_request_ready", "response": "provider_response_ready",
                      "tool_pending": "tool_effect_pending", "receipt": "tool_delivery_pending",
                      "writer_settled": "kernel_settled"}
            if position.logical_turn == 2 and leaves.get(boundary) == position.leaf:
                os._exit(74)
        durable.subscribe_commits(committed)
    request = SimpleNamespace(task="research recovery", session_id="s", project="", requested_capabilities=())
    policy = build_task_policy_for_entry(request, "research")
    deps = ResearchFlowDeps(state=SimpleNamespace(durable=durable), knowledge_store=store,
        evidence_ledgers=None, search_factory=Search, ghost_continuity=lambda **_: None,
        managed_outputs=ManagedOutputStore(root), runtime_mutations=line)
    frame = SimpleNamespace(provider=Provider(), provider_id="local", project_text="", trace=None,
        run_id="r", research_handoff="", entry_policy=policy, request=request)
    try:
        result = run_research_pipeline(deps, frame, SimpleNamespace(on_event=lambda _: None), max_turns=12,
            build_context=lambda *_args, **_kwargs: ResearchContext("research recovery", "s", "r", max_turns=12))
        return result, state()
    finally:
        store.close()
        durable.close()


@pytest.mark.parametrize("boundary", ["request", "response", "tool_pending", "receipt", "writer_settled",
                                       "synthesis_published", "publication_admitted", "publication_settled"])
def test_research_restart_preserves_original_reads_and_single_publication(tmp_path, boundary):
    source = ("from pathlib import Path; from tests.durable.test_research_continuation_restart import _run; "
              "import sys; _run(Path(sys.argv[1]), sys.argv[2])")
    child = subprocess.run([sys.executable, "-c", source, str(tmp_path), boundary], capture_output=True, timeout=50)
    assert child.returncode == 74, child.stderr.decode(errors="replace")
    first, state = _run(tmp_path, resume=True)
    assert first.followup_rounds == 0 and not first.followup_applied
    assert first.final_result.stop_reason == "done" and first.final_result.completion_proof.satisfied
    before = (tmp_path / "observations.jsonl").read_bytes()
    notes = {str(path): path.read_bytes() for path in (tmp_path / "knowledge").rglob("*.md")}
    second, again = _run(tmp_path, resume=True)
    assert second == first and again == state
    assert (tmp_path / "observations.jsonl").read_bytes() == before
    assert {str(path): path.read_bytes() for path in (tmp_path / "knowledge").rglob("*.md")} == notes
    observations = [json.loads(row) for row in before.splitlines()]
    assert [(row["attempt"], row["turn"]) for row in observations if row["event"] == "request"] == [(1, 1), (1, 2)]
    assert [row["url"] for row in observations if row["event"] == "fetch"] == ["https://example.com/source-1"]
    assert sum(row["event"] == "write" for row in observations) == 1
    from codey.durable import DurableSession, SQLiteDurableStore
    from codey.knowledge.store import KnowledgeStore

    durable = DurableSession(SQLiteDurableStore(tmp_path / "state.sqlite"))
    try:
        publications = [effect for effect in durable.effects("r")
                        if effect.intent.get("tool_name") == "_publish_research_answer"]
        assert len(publications) == 1 and publications[0].settlement["status"] == "ok"
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
        if boundary == "publication_settled":
            result, _ = _run(tmp_path, resume=True)
            assert result.final_result.stop_reason == "stopped"
        else:
            with pytest.raises(TaskCancelled, match="publication settled"):
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


def test_durable_cancellation_during_submission_never_returns_done(tmp_path):
    from codey.durable import DurableSession, SQLiteDurableStore

    result, _ = _run(tmp_path, cancel_on_submission=True)
    assert result.final_result.stop_reason == "stopped"
    durable = DurableSession(SQLiteDurableStore(tmp_path / "state.sqlite"))
    try:
        assert durable.task("r").state["cancel_requested"]
        observations = [json.loads(row) for row in (tmp_path / "observations.jsonl").read_bytes().splitlines()]
        assert [row["attempt"] for row in observations if row["event"] == "request"] == [1, 1]
        assert sum(row["event"] == "fetch" for row in observations) == 1
        assert not any(row["event"] == "write" for row in observations)
    finally:
        durable.close()


@pytest.mark.parametrize("not_sent", [True, False])
def test_failed_submission_retains_original_turn_and_provider_diagnostic(tmp_path, not_sent):
    from codey.durable import DurableSession, SQLiteDurableStore
    from codey.durable.checkpoint import operation_from_checkpoint
    from codey.providers.error_classification import RequestPrepError
    from codey.research.pipeline import ResearchContinuationError

    failure = RequestPrepError("submission preparation failed") if not_sent else OSError("submission outcome unknown")
    with pytest.raises(ResearchContinuationError, match="provider failed.*submission"):
        _run(tmp_path, failure=failure)
    durable = DurableSession(SQLiteDurableStore(tmp_path / "state.sqlite"))
    try:
        position = operation_from_checkpoint(durable.task("r"), "s")
        assert position.writer_attempt == 1
        # A definitely unsent request retains the preceding tool receipt for
        # redelivery. An unknown outcome retains the admitted second turn.
        assert position.logical_turn == (1 if not_sent else 2)
        assert position.leaf != "kernel_settled"
        failed = durable.effects("r")[-1]
        assert failed.settlement["sent_state"] == ("not_sent" if not_sent else "maybe_sent")
        durable.recover_scheduler_state(force=True)
    finally:
        durable.close()
    before = (tmp_path / "observations.jsonl").read_bytes()
    assert [row["attempt"] for row in map(json.loads, before.splitlines()) if row["event"] == "request"] == [1, 1]
    if not_sent:
        result, _ = _run(tmp_path, resume=True)
        assert result.followup_rounds == 0 and result.final_result.stop_reason == "done"
        observations = [json.loads(row) for row in (tmp_path / "observations.jsonl").read_bytes().splitlines()]
        assert sum(row["event"] == "fetch" for row in observations) == 1
        assert sum(row["event"] == "write" for row in observations) == 1
    else:
        for _ in range(2):
            with pytest.raises(ResearchContinuationError, match="external_outcome_unknown"):
                _run(tmp_path, resume=True)
            assert (tmp_path / "observations.jsonl").read_bytes() == before
