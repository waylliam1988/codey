"""Exact read references reduce model work without certifying interpretations."""

import copy
import json

import pytest

from codey.knowledge.archive import stable_identity
from codey.knowledge.changes import KnowledgeChanges
from codey.knowledge.store import KnowledgeStore
from codey.operations.kernel_facts import project_result_observation
from codey.operations.task_execution import ExecutionDelegate
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.research.ledger import ResearchLedger
from codey.research.tools import ResearchTools
from codey.runtime.core.models import ToolCall

URL = "https://example.com/study"
TEXT = "Writers and readers can run at the same time. There is only one writer at a time."


class Search:
    text = TEXT

    def fetch(self, url):
        return {"status": "ok", "url": url, "title": "Original study", "text": self.text, "truncated": False}

    def search(self, query, limit=8):
        return [{"url": URL, "title": "Original study", "snippet": "Lead, not evidence"}]


def setup(tmp_path):
    store = KnowledgeStore(tmp_path)
    tools = ResearchTools(Search(), store, KnowledgeChanges(tmp_path), session_id="first")
    session = TaskSession(TaskPolicy(frozenset({"web.read", "knowledge.write", "knowledge.read", "control"})),
                          task_kind="research")
    tools.ledger.questions = ("What limits writer concurrency?",)
    session.research_questions = tools.ledger.questions
    delegate = ExecutionDelegate(session=session, research_tools=tools)
    return store, tools, session, delegate


def execute(delegate, tools, session, name, args):
    call = ToolCall(name, args)
    result, ok, _ = delegate._execute_research(call)
    if name == "open_url":
        assert ok, result.model_text
    if ok:
        # Source ids are allocated by the common commit entry, not by the tool.
        from codey.operations.kernel_facts import bind_source_identity

        result = bind_source_identity(session, call, result, ok=ok)
        project_result_observation(session, call, result, ok=ok, research_ledger=tools.ledger)
    return result


@pytest.mark.usefixtures("public_source_dns")
def test_real_dispatch_save_restart_and_exact_idempotency(tmp_path):
    store, tools, session, delegate = setup(tmp_path)
    try:
        opened = execute(delegate, tools, session, "open_url", {"url": URL})
        passage = next(iter(tools.ledger.read_passages))
        assert f"passage_ref={passage}" in opened.model_text
        args = {"claim": "Only one writer can write at a time.", "source_ref": "s1", "passage_ref": passage,
                "conditions": [{"name": "mode", "state": "not_extracted"}]}
        result = execute(delegate, tools, session, "save_finding", args)
        assert result.ok, result.model_text
        ref = json.loads(result.model_text)["finding_ref"]
        claim_id = stable_identity("claim", tools.ledger.references[ref].note_id, args["claim"])
        version = store.repository.read(claim_id)
        assert version.value.status == "candidate" and version.interpretation_review == "unreviewed"
        assert version.value.conditions == {"mode": {"state": "not_extracted"}}
        before = store.read_note(tools.ledger.references[ref].note_id).to_markdown()
        # Reconstruct the read bindings through the committed receipt alone.
        recovered = ResearchLedger()
        restored_session = TaskSession(session.policy, task_kind="research")
        project_result_observation(restored_session, opened.call, opened, ok=True, research_ledger=recovered)
        project_result_observation(restored_session, result.call, result, ok=True, research_ledger=recovered)
        assert recovered.read_passages[passage].excerpt == TEXT
        tools.ledger = recovered
        replay = execute(delegate, tools, restored_session, "save_finding", args)
        assert replay.ok and json.loads(replay.model_text)["finding_ref"] == ref
        assert store.read_note(tools.ledger.references[ref].note_id).to_markdown() == before
        assert store.repository.read(claim_id).revision == version.revision
    finally:
        store.close()
    store = KnowledgeStore(tmp_path)
    try:
        assert store.repository.read(claim_id).interpretation_review == "unreviewed"
    finally:
        store.close()


@pytest.mark.usefixtures("public_source_dns")
def test_old_passage_still_archives_old_source_after_url_changes(tmp_path):
    store, tools, session, delegate = setup(tmp_path)
    try:
        old = execute(delegate, tools, session, "open_url", {"url": URL})
        ref = next(iter(tools.ledger.read_passages))
        tools.search.text = "This updated document has entirely different evidence and conditions."
        execute(delegate, tools, session, "open_url", {"url": URL})
        result = execute(delegate, tools, session, "save_finding", {
            "claim": "Only one writer can write at a time.", "source_ref": "s1", "passage_ref": ref})
        assert result.ok, result.model_text
        finding = json.loads(result.model_text)["finding_ref"]
        claim = store.repository.read(stable_identity("claim", tools.ledger.references[finding].note_id, "Only one writer can write at a time."))
        evidence = store.repository.read(claim.value.references[0].target, claim.value.references[0].revision)
        source = store.repository.read(evidence.value.references[0].target, evidence.value.references[0].revision)
        assert source.value.content == TEXT
        assert tools.ledger.source_text_for_url(URL) == tools.search.text
        recovered = ResearchLedger()
        project_result_observation(TaskSession(session.policy), old.call, old, ok=True, research_ledger=recovered)
        project_result_observation(TaskSession(session.policy), result.call, result, ok=True, research_ledger=recovered)
        assert len(recovered.evidence_items) == 1
    finally:
        store.close()


@pytest.mark.usefixtures("public_source_dns")
def test_forged_unopened_and_cross_source_references_fail_without_writes(tmp_path):
    store, tools, session, delegate = setup(tmp_path)
    try:
        opened = execute(delegate, tools, session, "open_url", {"url": URL})
        ref = next(iter(tools.ledger.read_passages))
        execute(delegate, tools, session, "open_url", {"url": URL + "-other"})
        for source, passage in [("s99", ref), ("s2", ref), ("s1", "pdeadbeef"), ("s1", "h1"), ("s1", f"[{ref}]")]:
            result = execute(delegate, tools, session, "save_finding", {
                "claim": "Only one writer can write at a time.", "source_ref": source, "passage_ref": passage})
            assert not result.ok, result.model_text
            if passage == f"[{ref}]":
                assert '"passage_ref":"' + ref + '"' in result.model_text and "without square brackets" in result.model_text
        assert not tools.created_ids and not tools.ledger.evidence_items
        forged = copy.deepcopy(opened)
        forged.canonical["read_passages"][0]["end"] -= 1
        with pytest.raises(ValueError, match="identity differs"):
            project_result_observation(TaskSession(session.policy), forged.call, forged, ok=True,
                                       research_ledger=ResearchLedger())
    finally:
        store.close()


@pytest.mark.usefixtures("public_source_dns")
def test_comparison_is_optional_unreviewed_and_never_filled_by_application(tmp_path):
    store, tools, session, delegate = setup(tmp_path)
    try:
        execute(delegate, tools, session, "open_url", {"url": URL})
        ref = next(iter(tools.ledger.read_passages))
        args = {"claim": "Only one writer can write at a time.", "source_ref": "s1", "passage_ref": ref,
                "comparison": {"subject": "writers", "factor": "WAL", "comparator": "unknown",
                               "endpoint": "write concurrency", "direction": "one", "uncertainty": "unknown"}}
        result = execute(delegate, tools, session, "save_finding", args)
        assert result.ok, result.model_text
        finding = json.loads(result.model_text)["finding_ref"]
        claim = store.repository.read(stable_identity("claim", tools.ledger.references[finding].note_id, args["claim"]))
        assert claim.value.conditions["comparison_comparator"] == {"state": "not_extracted"}
        assert claim.interpretation_review == "unreviewed"
        args["comparison"].pop("uncertainty")
        result = execute(delegate, tools, session, "save_finding", args)
        assert not result.ok and "comparison requires" in result.model_text
    finally:
        store.close()


def report_args(ref):
    return {"summary": f"Only one writer can write at a time. [{ref}]", "report_sections": {
        "evidence": [f"Only one writer can write at a time. [{ref}]"],
        "counter": ["本轮尚未调查反证，仍有证据缺口。"],
        "source_quality": [f"Original documentation; interpretation remains unreviewed. [{ref}]"],
        "coverage": [{"question_ref": "q1", "state": "partial", "answer": "Writer concurrency answered; version boundaries remain unknown.", "findings": [ref], "investigations": []}]}}


@pytest.mark.usefixtures("public_source_dns")
def test_report_uses_only_selected_findings_and_pins_original_versions(tmp_path):
    from codey.operations.research_iteration import _persist_synthesis
    from codey.research.finding_report import compile_finding_report

    store, tools, session, delegate = setup(tmp_path)
    try:
        execute(delegate, tools, session, "open_url", {"url": URL})
        passage = next(iter(tools.ledger.read_passages))
        saved = execute(delegate, tools, session, "save_finding", {
            "claim": "Only one writer can write at a time.", "source_ref": "s1", "passage_ref": passage})
        chosen = json.loads(saved.model_text)["finding_ref"]
        execute(delegate, tools, session, "open_url", {"url": URL + "-uncited"})
        other_passage = next(ref for ref, p in tools.ledger.read_passages.items() if p.source.final_url.endswith("-uncited"))
        unused = execute(delegate, tools, session, "save_finding", {
            "claim": "Writers and readers can run concurrently.", "source_ref": "s2", "passage_ref": other_passage})
        omitted = json.loads(unused.model_text)["finding_ref"]
        args = report_args(chosen)
        original = copy.deepcopy(args)
        compiled = compile_finding_report(args, tools.ledger, store)
        assert args == original
        assert URL + "-uncited" not in compiled.text
        assert "[1]" in compiled.text and "[2]" not in compiled.text and "[f" not in compiled.text
        assert compiled.finding_ids == (tools.ledger.references[chosen].note_id,)
        refs = compiled.knowledge_refs
        _persist_synthesis(tools, "Research writers", compiled.text, session_id="first", project="",
            run_id="selected", on_event=lambda _: None, policy=session.policy,
            knowledge_refs=list(refs), finding_ids=compiled.finding_ids)
        answer = store.repository.read(stable_identity("answer", "selected", "initial"))
        assert {(r.target, r.revision) for r in answer.value.references} == {(r["id"], r["revision"]) for r in refs}
        assert stable_identity("claim", tools.ledger.references[omitted].note_id, "Writers and readers can run concurrently.") not in {
            r.target for r in answer.value.references}
        for bad in (report_args("f999"), report_args("1"), report_args("s1")):
            with pytest.raises(ValueError):
                compile_finding_report(bad, tools.ledger, store)
        with pytest.raises(ValueError, match="exactly"):
            compile_finding_report({**args, "report_sections": {**args["report_sections"], "sources": [URL]}},
                                   tools.ledger, store)
    finally:
        store.close()


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.usefixtures("public_source_dns")
def test_simple_finding_and_generated_report_use_the_real_shared_driver(tmp_path, monkeypatch, native):
    from codey.operations.kernel_contracts import (
        KernelExecutionDeps,
        KernelObservationDeps,
        KernelRunRequest,
        KernelTransportDeps,
    )
    from codey.providers.base import AssistantTurn, ProviderToolCall
    from tests.support.kernel_runtime import run_task_kernel

    store, tools, session, _ = setup(tmp_path)
    session.policy = TaskPolicy(session.policy.grants, strict_research=True)
    session.research_questions = ()
    tools.ledger.questions = ()
    session.max_turns = 7
    monkeypatch.setattr("codey.operations.kernel_transport.provider_uses_native", lambda *a, **k: native)

    class Provider:
        turns = 0

        def reply(self):
            self.turns += 1
            if self.turns == 1:
                name, args = "research_plan", {"questions": ["What limits writer concurrency?"]}
            elif self.turns == 2:
                name, args = "web_search", {"query": "writer concurrency official documentation"}
            elif self.turns == 3:
                name, args = "open_result", {"result_id": "r1"}
            elif self.turns == 4:
                name, args = "save_finding", {"claim": "Only one writer can write at a time.",
                    "source_ref": "s1", "passage_ref": next(iter(tools.ledger.read_passages))}
            else:
                name, args = "done", report_args(next(iter(tools.ledger.references)))
            return (AssistantTurn(tool_calls=(ProviderToolCall(f"call-{self.turns}", name, args),)) if native else
                    json.dumps({"tool": name, "args": args}))

        def send(self, prompt):
            return self.reply()

        def send_turn(self, prompt, declared):
            return self.reply()

        def send_tool_results(self, results, declared):
            return self.reply()

        def acknowledge_tool_results(self, results, declared, timeout=None):
            return AssistantTurn()

    try:
        result = run_task_kernel(session, request=KernelRunRequest(
            transport=KernelTransportDeps(Provider(), user_task="Research writer concurrency"),
            execution=KernelExecutionDeps(research_tools=tools, permission_profile="research"),
            observation=KernelObservationDeps(completion_context={"research_ledger": tools.ledger})))
        assert result.completed, result.summary
        assert "## 来源" in result.summary and "[1] Original study" in result.summary
        assert "实际检索词" in result.summary
        assert session.notes_saved == 1 and len(tools.ledger.evidence_items) == 1
    finally:
        store.close()
