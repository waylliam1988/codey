"""One writer, exact evidence accounting and one recoverable publication; no self-certification."""

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from codey.research.task_requirements import task_requirements
from tests.test_research_findings import URL

TASK = "Explain writer concurrency. Distinguish historical version restrictions."


def _run(root: Path, *, boundary="", resume=False, budget=10, native=False, task=TASK,
         provider_factory=None, planner_enabled=False, required_checks=(), omit_requirement=False,
         unsupported_claim=False, bad_reference=False):
    import hashlib
    import os
    from unittest.mock import patch

    from codey.durable import DurableSession, SQLiteDurableStore
    from codey.durable.checkpoint import operation_from_checkpoint
    from codey.knowledge.store import KnowledgeStore
    from codey.operations.research_flow import ResearchFlowDeps, run_research_pipeline
    from codey.policies.task_policy import TaskPolicy
    from codey.research.context import ResearchContext, ResearchPipelineConfig
    from codey.research.passages import passage_ref
    from codey.runtime.write.mutation_line import RuntimeMutationLine
    from codey.storage.managed_outputs import ManagedOutputStore

    root.mkdir(parents=True, exist_ok=True)
    durable = DurableSession(SQLiteDurableStore(root / "state.sqlite"))
    if resume:
        durable.recover_scheduler_state()
        if durable.task("r").status == "pending":
            durable.claim_task("r")
    else:
        durable.admit_input(conversation_id="s", request_id="input", task_id="r", text=task, provider_snapshot={})
        durable.claim_task("r")
        durable.start_task("r", state=dict(durable.task("r").state))
    line = RuntimeMutationLine(durable=durable)
    if not resume:
        line.accept_operation(session_id="s", run_id="r", provider_id="local", turn_budget=budget,
                              max_repair_rounds=0, task_kind="research")

    def state():
        return operation_from_checkpoint(durable.task("r"), "s")

    def log(**row):
        with (root / "calls.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row) + "\n")

    text = "Readers and a writer may run concurrently. Only one writer at a time."
    claim = "Multiple writers commit concurrently." if unsupported_claim else "Only one writer can write at a time."

    class Search:
        def search(self, query, limit=8):
            log(search=query)
            return [{"title": "Official example", "url": URL, "snippet": "One writer."}]

        def fetch(self, url):
            log(fetch=url)
            return {"status": "ok", "url": url, "title": "Official example", "text": text, "truncated": False}

        def close(self):
            pass

    class Provider:
        native_tools = native

        def new_chat(self):
            pass

        def send_turn(self, prompt, tools):
            from codey.providers.base import AssistantTurn, ProviderToolCall

            row = json.loads(self.send(prompt))
            assert "submit_research_review" not in [tool.name for tool in tools]
            return AssistantTurn(tool_calls=(ProviderToolCall(
                f"call-{state().writer_attempt}-{state().logical_turn}", row["tool"], row["args"]),))

        def send_tool_results(self, results, tools):
            return self.send_turn("\n".join(row.user_content for row in results), tools)

        def acknowledge_tool_results(self, results, tools, timeout=None):
            from codey.providers.base import AssistantTurn

            log(ack=state().writer_attempt)
            return AssistantTurn()

        def send(self, prompt):
            position = state()
            turn = position.logical_turn
            log(attempt=position.writer_attempt, turn=turn)
            assert position.writer_attempt == 1, "Unexpected model review or repeated synthesis"
            if turn == 1:
                name, args = "research_plan", {"questions": ["What limits writer concurrency?", "Which restrictions are historical?"]}
            elif turn == 2:
                name, args = "web_search", {"query": "official writer concurrency", "question_ref": "q1"}
            elif turn == 3:
                name, args = "open_url", {"url": URL, "question_ref": "q1"}
            elif turn == 4:
                name, args = "save_finding", {"claim": claim, "source_ref": "s1", "question_ref": "q1",
                    "passage_ref": passage_ref(URL, hashlib.sha256(text.encode()).hexdigest()[:16], 0, len(text))}
            else:
                refs = [row["task_ref"] for row in task_requirements(task)]
                coverage = [
                    {"question_ref": "q1", "state": "evidence_found", "answer": claim,
                     "findings": ["f99" if bad_reference else "f1"], "investigations": ["a1", "a2"],
                     "task_refs": refs[:-1]},
                    {"question_ref": "q2", "state": "not_investigated",
                     "answer": "Historical version restrictions remain unknown; they were not investigated.",
                     "findings": [], "investigations": [], "task_refs": [] if omit_requirement else refs[-1:]},
                ]
                name, args = "done", {"report_sections": {"coverage": coverage}}
            return json.dumps({"tool": name, "args": args})

    store = KnowledgeStore(root / "knowledge")
    original_publish = store.publish_note

    def publish(note, *args, **kwargs):
        value = original_publish(note, *args, **kwargs)
        if note.type == "synthesis":
            log(publication=note.id)
            if boundary == "publication_committed":
                os._exit(74)
        return value

    store.publish_note = publish
    if boundary:
        def committed(*_):
            position = state()
            publications = [effect for effect in durable.effects("r")
                            if effect.intent.get("tool_name") == "_publish_research_answer"]
            if publications and ((boundary == "publication_admitted" and publications[-1].settlement is None)
                                 or (boundary == "publication_receipt" and publications[-1].settlement is not None)):
                os._exit(74)
            if position.logical_turn == 5 and position.leaf == boundary:
                os._exit(74)
        durable.subscribe_commits(committed)
    policy = TaskPolicy(frozenset({"control", "web.read", "knowledge.read", "knowledge.write"}), strict_research=True,
                        required_checks=required_checks)
    deps = ResearchFlowDeps(state=SimpleNamespace(durable=durable), knowledge_store=store, evidence_ledgers=None,
        search_factory=Search, run_research_advisors=None, ghost_continuity=lambda **_: None,
        managed_outputs=ManagedOutputStore(root), runtime_mutations=line)
    frame = SimpleNamespace(provider=provider_factory(Provider()) if provider_factory else Provider(),
        provider_id="local", project_text="", trace=None, run_id="r", research_handoff="", entry_policy=policy,
        request=SimpleNamespace(task=task, session_id="s", requested_capabilities=()))
    try:
        with patch("codey.operations.research_flow.ResearchPipelineConfig", return_value=ResearchPipelineConfig(enabled=planner_enabled)):
            result = run_research_pipeline(deps, frame, SimpleNamespace(on_event=lambda _: None), max_turns=budget,
                build_context=lambda *a, **kw: ResearchContext(question=task, session_id="s", run_id="r", max_turns=budget))
        return result.final_result
    finally:
        store.close()
        durable.close()


@pytest.mark.usefixtures("public_source_dns")
@pytest.mark.parametrize("native", [False, True])
def test_writer_publishes_once_without_model_review_and_replays_without_new_calls(tmp_path, native):
    from codey.knowledge.store import KnowledgeStore
    from codey.research.report_projection import validate_accepted_projection

    result = _run(tmp_path, native=native, planner_enabled=True)
    assert result.stop_reason == "done" and result.semantic_review is None
    assert result.turns == 5 and result.completion_proof.satisfied
    assert not any("assessed" in row.check_id for row in result.completion_proof.checks)
    validate_accepted_projection(result, run_id="r", session_id="s", project="")
    before = (tmp_path / "calls.jsonl").read_bytes()
    assert _run(tmp_path, native=native, planner_enabled=True, resume=True) == result
    assert (tmp_path / "calls.jsonl").read_bytes() == before
    store = KnowledgeStore(tmp_path / "knowledge")
    try:
        rows = store.index._read_conn().execute("SELECT payload FROM knowledge_versions WHERE kind='answer'").fetchall()
        assert len(rows) == 1
        answer = json.loads(rows[0][0])
        assert answer["provenance"]["coverage_assessment"] == "model_reported_not_semantically_verified"
        assert answer["provenance"]["semantic_review"] is None
        assert "未经独立语义审查" in result.summary
    finally:
        store.close()


@pytest.mark.usefixtures("public_source_dns")
@pytest.mark.parametrize("options", [{"omit_requirement": True}, {"bad_reference": True}, {"budget": 4}])
def test_incomplete_accounting_invalid_reference_or_budget_never_publishes(tmp_path, options):
    result = _run(tmp_path, **options)
    assert result.stop_reason != "done" and not result.synthesis_id
    assert result.completion_proof is None
    calls = (tmp_path / "calls.jsonl").read_text()
    assert '"publication"' not in calls


@pytest.mark.usefixtures("public_source_dns")
def test_wrong_interpretation_stays_unreviewed_and_model_verdict_cannot_certify_it(tmp_path):
    from codey.research.semantic_review import reviewed_completion

    result = _run(tmp_path, unsupported_claim=True)
    assert result.stop_reason == "done"  # Workflow completion deliberately does not certify entailment.
    assert result.semantic_review is None
    assert all(row["interpretation_review"] == "unreviewed" for row in result.accepted_report.reference_metadata.values())
    assert "未经独立语义审查" in result.summary
    for verdict in ("passed", "failed"):
        with pytest.raises(ValueError, match="cannot certify"):
            reviewed_completion(result.completion_proof, {"status": verdict, "assessment": {}})


@pytest.mark.usefixtures("public_source_dns")
def test_original_required_search_receipt_remains_the_completion_evidence(tmp_path):
    from codey.operations.task_session import turn_effect_id
    from codey.policies.tool_requirement import require_tool_success

    checks = (require_tool_success("web_search", arguments={"query": "official writer concurrency"}),)
    result = _run(tmp_path, required_checks=checks)
    assert result.stop_reason == "done"
    assert turn_effect_id("r:research:1", 2, 0) in result.completion_proof.evidence_refs
    assert _run(tmp_path, required_checks=checks, resume=True) == result


@pytest.mark.usefixtures("public_source_dns")
@pytest.mark.parametrize("boundary", ["provider_response_ready", "kernel_settled", "publication_admitted",
                                       "publication_committed", "publication_receipt"])
def test_real_process_restart_reuses_writer_and_publication_receipts(tmp_path, boundary):
    source = ("from pathlib import Path; from tests.test_research_deterministic_completion import _run; import sys; "
              "_run(Path(sys.argv[1]), boundary=sys.argv[2])")
    child = subprocess.run([sys.executable, "-c", source, str(tmp_path), boundary], capture_output=True, timeout=50)
    assert child.returncode == 74, child.stderr.decode(errors="replace")
    result = _run(tmp_path, resume=True)
    assert result.stop_reason == "done", result.summary
    rows = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert sum("publication" in row for row in rows) == 1
    assert sum("fetch" in row for row in rows) == 1
    assert sum("attempt" in row for row in rows) == 5


def test_original_task_accounting_rejects_deleted_unknown_and_duplicate_refs():
    from codey.research.task_requirements import validate_task_accounting

    rows = [{"task_refs": ["r1"]}, {"task_refs": ["r2"]}]
    validate_task_accounting(rows, TASK)
    for changed in ([rows[0]], [{"task_refs": ["r1", "r9"]}], [{"task_refs": ["r1", "r1", "r2"]}]):
        with pytest.raises(ValueError):
            validate_task_accounting(changed, TASK)
