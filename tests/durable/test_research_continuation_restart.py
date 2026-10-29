"""Research invocations resume from original receipts through the sole driver."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


def _run(root: Path, boundary: str = "", resume: bool = False, failure: Exception | None = None):
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
        line.accept_operation(session_id="s", run_id="r", provider_id="local", turn_budget=4,
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
            if attempt == 2:
                if failure is not None:
                    raise failure
                return json.dumps({"tool": "knowledge_write", "args": {
                    "type": "fact", "title": "Invalid evidence", "body": "missing sources and evidence"}})
            round_index = 1 if attempt == 3 else 2
            url = f"https://example.com/source-{round_index}"
            excerpt = f"Recovery evidence number {round_index} is durable."
            return json.dumps({"tool": "knowledge_write", "args": {
                "type": "fact", "title": f"Recovery evidence {round_index}", "body": excerpt,
                "sources": [url], "evidence": [{"source_url": url, "excerpt": excerpt,
                    "claim": excerpt, "stance": "supports"}]}})

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
    if boundary and boundary != "vault":
        def committed(_result, _mutation):
            position = state()
            if ((boundary == "material" and position.writer_attempt == 1 and position.continuation_ref)
                    or (boundary == "response" and position.writer_attempt == 3
                        and position.leaf == "provider_response_ready")
                    or (boundary == "receipt" and position.writer_attempt == 3
                        and position.leaf == "tool_delivery_pending")
                    or (boundary == "second_round" and position.writer_attempt == 4
                        and position.leaf == "provider_response_ready")):
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


@pytest.mark.parametrize("boundary", ["material", "response", "receipt", "vault", "second_round"])
def test_research_restart_preserves_material_invocations_and_note_ids(tmp_path, boundary):
    source = ("from pathlib import Path; from tests.durable.test_research_continuation_restart import _run; "
              "import sys; _run(Path(sys.argv[1]), sys.argv[2])")
    child = subprocess.run([sys.executable, "-c", source, str(tmp_path), boundary],
                           capture_output=True, timeout=50)
    assert child.returncode == 74, child.stderr.decode(errors="replace")
    first, state = _run(tmp_path, resume=True)
    assert first.followup_rounds == 2, first
    assert first.new_evidence_count == 2
    assert len(first.final_result.notes_created) == 2
    before = (tmp_path / "observations.jsonl").read_bytes()
    notes = {str(path): path.read_bytes() for path in (tmp_path / "knowledge").rglob("*.md")}
    second, again = _run(tmp_path, resume=True)
    assert second == first
    assert again == state
    assert (tmp_path / "observations.jsonl").read_bytes() == before
    assert {str(path): path.read_bytes() for path in (tmp_path / "knowledge").rglob("*.md")} == notes
    observations = [json.loads(row) for row in before.splitlines()]
    assert [row["attempt"] for row in observations if row["event"] == "request"] == [1, 2, 3, 4]
    assert [row["query"] for row in observations if row["event"] == "search"] == [
        "initial research", "followup 1", "followup 2"]
    assert [row["url"] for row in observations if row["event"] == "fetch"] == [
        "https://example.com/source-1", "https://example.com/source-2"]
    assert [row["identity"] for row in observations if row["event"] == "write"] == first.final_result.notes_created


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
    assert [row["attempt"] for row in observations if row["event"] == "request"] == [1, 2, 3, 4]


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
        assert [row["attempt"] for row in observations if row["event"] == "request"] == [1, 2, 3, 4]
        assert [row["query"] for row in observations if row["event"] == "search"] == [
            "initial research", "followup 1", "followup 2"]
    else:
        for _ in range(2):
            with pytest.raises(ResearchContinuationError, match="external_outcome_unknown"):
                _run(tmp_path, resume=True)
            assert (tmp_path / "observations.jsonl").read_bytes() == before
