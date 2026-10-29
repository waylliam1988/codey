import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from codey.knowledge.archive import stable_identity
from codey.knowledge.changes import KnowledgeChanges
from codey.knowledge.concepts import build_unified_research_graph
from codey.knowledge.model import KnowledgeMutation as M
from codey.knowledge.model import KnowledgeObject as K
from codey.knowledge.model import KnowledgeReference as R
from codey.knowledge.note import KnowledgeNote
from codey.knowledge.store import KnowledgeStore
from codey.operations.research_completion_checks import knowledge_reference_checks
from codey.operations.research_iteration import _persist_synthesis
from codey.policies.task_policy import TaskPolicy
from codey.research.methods import research_method_guidance
from codey.research.source_document import SourceDocument
from codey.research.tools import ResearchTools
from tests.test_knowledge_repository import example


def test_explicit_commit_undo_and_session_graph(tmp_path):
    with_store = KnowledgeStore(tmp_path)
    try:
        changes = KnowledgeChanges(tmp_path)
        tools = ResearchTools(None, with_store, changes, session_id="first")
        args = {"commit_id": "question", "changes": [{"expected_revision": 0,
            "value": K("question", "question", "Which conditions overturn the analogy?", "model_inference").to_dict()}]}
        assert tools.knowledge_commit(args).ok
        assert any("question@1" in node.id for node in build_unified_research_graph(with_store, "first").nodes)
        assert changes.restore_result().ok
        assert with_store.repository.read("question").value.status == "withdrawn"
    finally:
        with_store.close()


def test_purge_removes_dependent_documents_exports_and_prevents_replay(tmp_path):
    store = KnowledgeStore(tmp_path)
    try:
        private = K("private", "observation", "A private observation", "user_statement", scope="personal", scope_id="owner")
        derived = K("derived", "inference", "Private derived result", "model_inference", scope="personal", scope_id="owner",
                    references=(R("private", 1, "depends_on"),))
        mutations = [M(private), M(derived)]
        note = KnowledgeNote(id="private-report", body=private.content, session_id="owner")
        store.publish_note(note, "private-commit", mutations)
        exported = store.export_note(note.id)
        assert exported.exists()
        assert store.purge_knowledge(["private"]) == {"private", "derived"}
        assert not exported.exists()
        assert store.documents.history(note.id) == []
        assert not store.search("private", session_id="owner")
        assert store.note_references(note.id) == []
        with pytest.raises(ValueError, match="purged"):
            store.repository.commit("private-commit", mutations)
    finally:
        store.close()


def test_shared_staged_excerpts_keep_stances_and_exact_source_text(tmp_path):
    store = KnowledgeStore(tmp_path)
    try:
        tools = ResearchTools(None, store, KnowledgeChanges(tmp_path), session_id="first")
        url = "https://example.org/experiment"
        tools.sources_read.add(url)
        tools.ledger.record_open_document(SourceDocument.html(requested_url=url, final_url=url,
            title="Experiment", text="Under WAL, ONE writer\ncommits at a time. Multiple readers can proceed."))
        staged = tools.create_staged()
        for title in ("Writer serialization", "WAL constraint"):
            assert staged.knowledge_write({"type": "fact", "title": title, "body": title, "sources": [url],
                "evidence": [{"source_url": url, "excerpt": "one writer commits at a time", "claim": title,
                              "stance": "supports"}], "conditions": {"mode": {"state": "reported", "value": "WAL"}}}).ok
        tools.commit_staged(staged)
        for note_id in tools.created_ids:
            claim = next(store.repository.read(row["target"]) for row in store.note_references(note_id)
                         if store.repository.read(row["target"]).value.kind == "claim")
            fragment = store.repository.read(next(r.target for r in claim.value.references if r.relation == "supported_by"))
            assert fragment.value.content == "ONE writer\ncommits at a time"
        # Another observation about this exact claim preserves earlier support.
        note_id = tools.created_ids[0]
        assert tools.knowledge_write({"id": note_id, "type": "fact", "title": "Writer serialization", "body": "Scope limitation",
            "sources": [url], "evidence": [{"source_url": url, "excerpt": "Multiple readers can proceed.",
                "claim": "Writer serialization", "stance": "context"}]}).ok
        value = store.repository.read(stable_identity("claim", note_id, "Writer serialization")).value
        assert {ref.relation for ref in value.references} >= {"supported_by", "limited_by"}
    finally:
        store.close()


def test_answer_pins_cited_versions_and_replay_rejects_changed_output(tmp_path):
    store = KnowledgeStore(tmp_path)
    try:
        store.repository.commit("initial", example())
        tools = ResearchTools(None, store, KnowledgeChanges(tmp_path), session_id="second")
        kwargs = dict(session_id="second", project="", run_id="answer-run", on_event=lambda _: None,
                      policy=TaskPolicy(frozenset({"knowledge.write"})), knowledge_refs=[{"id": "claim", "revision": 1}])
        identity = _persist_synthesis(tools, "Question", "Qualified answer", **kwargs)
        answer = store.repository.read(stable_identity("answer", "answer-run", "initial"))
        assert answer.value.references == (R("claim", 1, "uses"),)
        assert _persist_synthesis(tools, "Question", "Qualified answer", **kwargs) == identity
        with pytest.raises(ValueError, match="changed"):
            _persist_synthesis(tools, "Question", "Different answer", **kwargs)
        store.repository.commit("withdraw", [M(replace(store.repository.read("claim").value, status="withdrawn"), 1)])
        assert store.repository.read(answer.value.id).needs_review
        graph = build_unified_research_graph(store, "second", depth=3)
        assert any(edge.kind == "uses" and edge.dst.endswith("claim@1") for edge in graph.edges)
    finally:
        store.close()


def test_reference_check_is_mechanical_and_does_not_prove_interpretation(tmp_path):
    store = KnowledgeStore(tmp_path)
    try:
        store.repository.commit("initial", example())
        session = SimpleNamespace(last_done_args={"knowledge_refs": [{"id": "claim", "revision": 1}]})
        assert knowledge_reference_checks(session, {"knowledge_store": store})[0].status == "pass"
        session.last_done_args["knowledge_refs"][0]["revision"] = 99
        assert knowledge_reference_checks(session, {"knowledge_store": store})[0].status == "fail"
        guidance = research_method_guidance("研究某种数据库医疗产品的商业前景与治疗效果")
        assert all(name + ": sources" in guidance for name in ("software", "clinical", "business"))
        assert "Old answers" in guidance and "not_reported" in guidance
    finally:
        store.close()


def test_conditions_object_shape_agrees_across_tool_protocols():
    from codey.research.tool_contract import openai_parameters_schema, validate_tool_args
    from codey.toolchain.tool_spec import spec_for_tool, validate_args_against_spec

    args = {"type": "hypothesis", "title": "Candidate", "body": "A mechanism may apply",
            "conditions": {"workload": {"state": "not_extracted"}}}
    assert openai_parameters_schema("knowledge_write")["properties"]["conditions"]["type"] == "object"
    assert dict(spec_for_tool("knowledge_write").parameters)["conditions"]["type"] == "object"
    assert not validate_args_against_spec("knowledge_write", args)
    assert validate_tool_args("knowledge_write", args).ok
    args["conditions"] = "Unstructured condition"
    assert "object" in validate_args_against_spec("knowledge_write", args)
    assert "object" in validate_tool_args("knowledge_write", args).error


def test_shared_driver_bounds_parallel_source_calls(tmp_path):
    from codey.operations.task_session import TaskSession
    from tests.support.kernel_runtime import KernelExecutionDeps, KernelRunRequest, KernelTransportDeps, run_task_kernel

    calls = []

    class Search:
        def search(self, query, limit=8):
            calls.append(query)
            return []

    class Provider:
        native_tools = False

        def send(self, prompt):
            return "\n".join(json.dumps({"tool": "web_search", "args": {"query": query}}) for query in ("first", "second"))

    store = KnowledgeStore(tmp_path)
    try:
        tools = ResearchTools(Search(), store, None)
        session = TaskSession(TaskPolicy(frozenset({"control", "web.read"})), max_turns=1,
                              research_acquisition_limits=(1, 1))
        run_task_kernel(session, request=KernelRunRequest(transport=KernelTransportDeps(provider=Provider()),
                                                         execution=KernelExecutionDeps(research_tools=tools)))
        assert len(calls) == 1
        assert any("budget exhausted" in result.model_text for result in session._memory_results.values())
    finally:
        store.close()


def test_repeated_staged_note_updates_keep_prior_evidence(tmp_path):
    store = KnowledgeStore(tmp_path)
    try:
        tools = ResearchTools(None, store, KnowledgeChanges(tmp_path), session_id="first")
        url = "https://example.org/staged"
        tools.sources_read.add(url)
        tools.ledger.record_open_document(SourceDocument.html(requested_url=url, final_url=url,
            title="Experiment", text="One writer commits. Readers can proceed."))
        staged = tools.create_staged()
        output = staged.knowledge_write({"type": "fact", "title": "Concurrency", "body": "Writer constraint",
            "sources": [url], "evidence": [{"source_url": url, "excerpt": "One writer commits.",
                "claim": "Serialized writer", "stance": "supports"}]})
        assert output.ok and "Structured knowledge versions" in output.model_text
        note_id = staged.created_ids[0]
        assert staged.knowledge_write({"id": note_id, "type": "fact", "title": "Concurrency", "body": "Reader scope",
            "sources": [url], "evidence": [{"source_url": url, "excerpt": "Readers can proceed.",
                "claim": "Serialized writer", "stance": "context"}]}).ok
        tools.commit_staged(staged)
        claim = store.repository.read(stable_identity("claim", note_id, "Serialized writer"))
        assert {ref.relation for ref in claim.value.references} == {"supported_by", "limited_by"}
        assert len(store.note_references(note_id)) == 4
    finally:
        store.close()


@pytest.mark.parametrize("stop_reason", ["controller_failure", "provider_failure", "recovery_failure", "delivery_pending"])
def test_initial_research_failure_is_not_replaced_by_a_successor_error(tmp_path, monkeypatch, stop_reason):
    from codey.operations.kernel_contracts import KernelResult
    from codey.research.pipeline import ResearchContinuationError
    from tests.support.kernel_runtime import run_research_iteration

    store = KnowledgeStore(tmp_path)
    try:
        monkeypatch.setattr("codey.operations.task_loop.run_task_kernel",
                            lambda *_args, **_kwargs: KernelResult(False, "Original failure", 2, stop_reason))
        with pytest.raises(ResearchContinuationError, match="Original failure"):
            run_research_iteration(SimpleNamespace(state=SimpleNamespace(), knowledge_store=store),
                provider=SimpleNamespace(new_chat=lambda: None), session_id="s", project="", task="question",
                max_turns=2, on_event=lambda _: None, stop_flag=None, provider_id="fake", run_id="run",
                chat_handoff="", trace_recorder=None, search=None)
    finally:
        store.close()


def test_strict_research_can_read_long_tool_results_at_every_stage():
    from codey.operations.kernel_protocol import controller_allowed_for_session
    from codey.operations.task_session import TaskSession

    session = TaskSession(TaskPolicy(frozenset({"control", "web.read", "knowledge.read"}), strict_research=True))
    assert "read_tool_result" in controller_allowed_for_session(session)
    session.search_results["r1"] = {"url": "https://example.org"}
    assert "read_tool_result" in controller_allowed_for_session(session)
    session.opened_sources.add("https://example.org")
    assert "read_tool_result" in controller_allowed_for_session(session)
