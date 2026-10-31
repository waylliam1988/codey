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


@pytest.mark.usefixtures("public_source_dns")
@pytest.mark.parametrize("page_count", [1, 2])
def test_pdf_findings_pin_page_body_and_restore_repeated_page_text(tmp_path, monkeypatch, page_count):
    import sys
    from types import SimpleNamespace

    from codey.research.finding_report import compile_finding_report

    body = "Only one writer can write at a time."
    monkeypatch.setitem(sys.modules, "pypdf", SimpleNamespace(PdfReader=lambda _stream: SimpleNamespace(
        pages=[SimpleNamespace(extract_text=lambda: body, get_contents=lambda: []) for _ in range(page_count)])))
    store, tools, session, delegate = setup(tmp_path)
    monkeypatch.setattr(tools.search, "fetch", lambda url: {
        "status": "ok", "url": url, "title": "PDF study", "content_kind": "pdf",
        "mime_type": "application/pdf", "bytes": b"%PDF fixture", "truncated": False})
    try:
        opened = execute(delegate, tools, session, "open_url", {"url": URL, "pages": f"1-{page_count}"})
        passages = list(tools.ledger.read_passages.values())
        assert [row.page for row in passages] == list(range(1, page_count + 1))
        assert all(row.excerpt == body for row in passages)
        for number in range(1, page_count + 1):
            assert f"[page {number}]" in opened.model_text
        saved = []
        for passage in passages:
            result = execute(delegate, tools, session, "save_finding", {
                "claim": body, "passage_ref": passage.ref})
            assert result.ok, result.model_text
            saved.append(result)
        assert [item.page for item in tools.ledger.evidence_items] == list(range(1, page_count + 1))
        ref = json.loads(saved[-1].model_text)["finding_ref"]
        compiled = compile_finding_report(answer_args(ref), tools.ledger, store, task=session.task_text)
        assert compiled.evidence_bindings[0]["page"] == page_count
        recovered = ResearchLedger()
        recovered.questions = tools.ledger.questions
        for result in (opened, *saved):
            project_result_observation(session, result.call, result, ok=True, research_ledger=recovered)
        expected = list(tools.ledger.evidence_items)
        tools.ledger = recovered
        for passage in passages:
            replay = execute(delegate, tools, session, "save_finding", {"claim": body, "passage_ref": passage.ref})
            assert replay.ok, replay.model_text
        assert tools.ledger.evidence_items == expected
    finally:
        store.close()


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
                          task_kind="research", task_text="Explain writer concurrency.")
    tools.ledger.questions = ("What limits writer concurrency?",)
    session.research_questions = tools.ledger.questions
    delegate = ExecutionDelegate(session=session, research_tools=tools)
    return store, tools, session, delegate


def execute(delegate, tools, session, name, args):
    from codey.research.questions import QUESTION_BOUND_TOOLS

    if name in QUESTION_BOUND_TOOLS and tools.ledger.questions:
        args = {"question_ref": "q1", **args}
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
@pytest.mark.parametrize("claim", [
    "Only one writer can write at a time.",
    "Writer concurrency:\n1. Only one writer can write at a time.\n2. Readers  and writers can run concurrently.",
])
def test_real_dispatch_save_restart_and_exact_idempotency(tmp_path, claim):
    store, tools, session, delegate = setup(tmp_path)
    try:
        opened = execute(delegate, tools, session, "open_url", {"url": URL})
        passage = next(iter(tools.ledger.read_passages))
        assert f"passage_ref={passage}" in opened.model_text
        args = {"question_ref": "q1", "claim": claim, "source_ref": "s1", "passage_ref": passage,
                "conditions": [{"name": "mode", "state": "not_extracted"}]}
        result = execute(delegate, tools, session, "save_finding", args)
        assert result.ok, result.model_text
        ref = json.loads(result.model_text)["finding_ref"]
        claim_id = stable_identity("claim", tools.ledger.references[ref].note_id, args["claim"])
        version = store.repository.read(claim_id)
        assert version.value.content == claim
        assert version.value.status == "candidate" and version.interpretation_review == "unreviewed"
        assert version.value.conditions == {"mode": {"state": "not_extracted"}}
        before = store.read_note(tools.ledger.references[ref].note_id).to_markdown()
        # Reconstruct the read bindings through the committed receipt alone.
        recovered = ResearchLedger()
        restored_session = TaskSession(session.policy, task_kind="research")
        from codey.runtime.core.models import ToolResult

        plan_call = ToolCall("research_plan", {"questions": list(tools.ledger.questions)})
        plan_result = ToolResult(ok=True, call=plan_call, model_text="Registered questions",
            canonical={"research_observation": {"questions": list(tools.ledger.questions)}})
        project_result_observation(restored_session, plan_call, plan_result, ok=True, research_ledger=recovered)
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
def test_repair_context_recovers_passage_and_per_question_refs_without_new_reads(tmp_path):
    from codey.research.context_view import build_research_context_view
    from codey.research.evidence_view import reference_evidence_preview
    from codey.research.references import restore_bindings

    store, tools, session, delegate = setup(tmp_path)
    try:
        tools.ledger.questions += ("What limits readers?",)
        opened = execute(delegate, tools, session, "open_url", {"url": URL})
        passage = next(iter(tools.ledger.read_passages))
        saved = execute(delegate, tools, session, "save_finding", {
            "claim": "Readers and writers run at the same time; only one writer may proceed.", "passage_ref": passage})
        finding = json.loads(saved.model_text)["finding_ref"]
        restored = ResearchLedger()
        restored.questions = tools.ledger.questions
        fresh = TaskSession(session.policy, task_kind="research")
        for receipt in (opened, saved):
            project_result_observation(fresh, receipt.call, receipt, ok=True, research_ledger=restored)
        tools.ledger = restored
        view = build_research_context_view(fresh, restored, store, session_id="first")
        assert view.references[0]["passage_refs"] == [passage]
        assert view.questions[0]["finding_refs"] == [finding]
        assert view.questions[0]["investigation_refs"] == ["a1"]
        assert view.questions[1]["finding_refs"] == view.questions[1]["investigation_refs"] == []
        # A fresh repair session uses the already-read exact passage directly.
        tools.search.fetch = lambda url: pytest.fail("repair must not reread this source")
        repair = ExecutionDelegate(session=fresh, research_tools=tools)
        corrected = execute(repair, tools, fresh, "save_finding", {
            "claim": "Only one writer can write at a time.", "passage_ref": view.references[0]["passage_refs"][0]})
        assert corrected.ok and len(restored.investigations) == 1
        # Knowledge alone must not invent a read passage in another run.
        unread = ResearchLedger()
        unread.questions = restored.questions
        restore_bindings(unread, saved.canonical["research_reference_bindings"])
        preview = reference_evidence_preview(unread, store, finding, project="", session_id="first")
        assert preview["evidence"] and all("passage_ref" not in row for row in preview["evidence"])
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
        "coverage": [{"question_ref": "q1", "state": "partial", "answer": "Writer concurrency answered; version boundaries remain unknown.", "findings": [ref], "investigations": [], "task_refs": ["r1"]}]}}


def answer_args(ref):
    return {"report_sections": {"coverage": [{"question_ref": "q1", "state": "partial",
        "answer": "Only one writer can write at a time. Version boundaries remain unknown.",
        "findings": [ref], "investigations": [], "task_refs": ["r1"]}]}}


def selection_args(ref):
    return {"report_sections": {"coverage": [{"question_ref": "q1", "state": "partial",
        "findings": [ref], "task_refs": ["r1"]}]}}


@pytest.mark.usefixtures("public_source_dns")
def test_multiple_passages_pin_separate_exact_excerpts_and_survive_receipt_restore(tmp_path):
    from codey.research.evidence_view import resolve_selected_references

    store, tools, session, delegate = setup(tmp_path)
    try:
        tools.search.text = ("The first condition applies during a long running read transaction. " * 10
                             + "\n\n" + "The second condition concerns automatic checkpoints. " * 14)
        opened = execute(delegate, tools, session, "open_url", {"url": URL})
        refs = list(tools.ledger.read_passages)
        assert len(refs) >= 2
        args = {"claim": "Read transactions and automatic checkpoints impose separate conditions.",
                "source_ref": "s1", "passage_ref": refs[0], "additional_passage_refs": [refs[1]]}
        saved = execute(delegate, tools, session, "save_finding", args)
        assert saved.ok, saved.model_text
        label = json.loads(saved.model_text)["finding_ref"]
        resolved = resolve_selected_references((label,), tools.ledger, store, project="", session_id="first")[0]
        assert {row.evidence.value.provenance["locator"] for row in resolved.evidence} == {
            "passage:" + ref for ref in refs[:2]}
        assert {row.evidence.value.content for row in resolved.evidence} == {
            tools.ledger.read_passages[ref].excerpt.strip() for ref in refs[:2]}
        before = resolved.version
        recovered = ResearchLedger()
        for result in (opened, saved):
            project_result_observation(TaskSession(session.policy), result.call, result, ok=True,
                                       research_ledger=recovered)
        tools.ledger = recovered
        assert len(recovered.evidence_items) == 2
        assert execute(delegate, tools, session, "save_finding", args).ok
        assert len(recovered.evidence_items) == 2
        assert store.repository.read(before.value.id).revision == before.revision
        tools.search.text = "Different source version with a different condition."
        execute(delegate, tools, session, "open_url", {"url": URL})
        fresh = next(ref for ref in tools.ledger.read_passages if ref not in refs)
        args["additional_passage_refs"] = [fresh]
        rejected = execute(delegate, tools, session, "save_finding", args)
        assert not rejected.ok and "same exact source version" in rejected.model_text
        assert len(tools.created_ids) == 1
    finally:
        store.close()


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
                name, args = "done", selection_args(next(iter(tools.ledger.references)))
            from codey.research.questions import QUESTION_BOUND_TOOLS

            if name in QUESTION_BOUND_TOOLS:
                args["question_ref"] = "q1"
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
