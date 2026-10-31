"""Assessment scope and real durable review/repair, without semantic keyword gates."""

import copy
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from codey.research.finding_report import compile_finding_report
from codey.research.semantic_review import parse_review, review_basis, review_issues, review_scope
from tests.test_research_findings import URL, answer_args, execute, report_args, setup

TASK = "Explain writer concurrency. Distinguish historical version restrictions."


@pytest.mark.usefixtures("public_source_dns")
@pytest.mark.parametrize("inline", [False, True])
def test_compiled_coverage_findings_bind_the_rendered_answer_for_review(tmp_path, inline):
    store, tools, session, delegate = setup(tmp_path)
    try:
        tools.search.text = "Only one writer at a time. Readers and writers can run concurrently."
        execute(delegate, tools, session, "open_url", {"url": URL})
        execute(delegate, tools, session, "save_finding", {
            "claim": "Only one writer can write at a time.", "source_ref": "s1",
            "passage_ref": next(iter(tools.ledger.read_passages))})
        args = report_args("f1")
        coverage = args["report_sections"]["coverage"][0]
        coverage.update(state="partial", answer="Only one writer can write at a time." + (" [f1]" if inline else ""))
        report = compile_finding_report(args, tools.ledger, store)
        basis = review_basis(report, tools, "Explain writer concurrency.")
        answer = next(row for row in basis["paragraphs"] if row["path"] == "report_sections.coverage[0].answer")
        assert answer["refs"] == ["f1"] and answer["kinds"] == ["claim"]
        raw = assessment(basis)
        assert parse_review(json.dumps(raw), basis)["paragraphs"][-1]["units"][0]["finding_units"] == [
            {"ref": "f1", "unit": 1}]
        # Structured findings must still reject a conflicting inline reference.
        coverage["answer"] += " [f2]"
        execute(delegate, tools, session, "save_finding", {
            "claim": "Readers and writers can run concurrently.", "source_ref": "s1",
            "passage_ref": next(iter(tools.ledger.read_passages))})
        with pytest.raises(ValueError, match="answer citations must also occur"):
            compile_finding_report(args, tools.ledger, store)
    finally:
        store.close()


def assessment(basis, reject=False):
    requirements = []
    paths = basis.get("report_paths", [row["path"] for row in basis["paragraphs"]])
    for row in basis["requirements"]:
        historical = "historical version restrictions" in row["text"]
        requirements.append({"task_ref": row["task_ref"],
            "question_refs": ([] if reject else ["q2"]) if historical else ["q1"],
            "report_paths": ([] if reject else [paths[-1]]) if historical else [paths[0]],
            "state": ("missing" if reject else "acknowledged_gap") if historical else "addressed",
            "reason": "Version evidence is missing." if historical else "Explicit writer limit."})
    return {"requirements": requirements,
        "investigations": [{"ref": ref, "question_ref": row["question_ref"], "verdict": "relevant",
            "reason": "The investigation targets the registered question."}
            for row in basis["coverage"] for ref in row["investigations"]
            if ref in basis.get("investigation_scope", [ref])],
        "claims": [{"ref": row["ref"], "units": [{"unit": unit["unit"],
                    "verdict": "unsupported" if reject else "supported",
                    "quotes": [] if reject else [{"evidence": 1, "quote": "Only one writer at a time."}],
                    "reason": "The excerpt permits only one writer; concurrent writers are unsupported." if reject else
                              "The excerpt explicitly permits only one writer."} for unit in row["units"]]}
                   for row in basis["claims"]],
        "paragraphs": [{"path": row["path"], "units": [{"unit": unit["unit"],
            "verdict": "unsupported" if reject else "supported",
            "basis": "findings" if row["refs"] else "gap",
            "finding_units": [{"ref": ref, "unit": 1} for ref in row["refs"]],
            "reason": "Check writer limit and stated gaps."} for unit in row["units"]]}
            for row in basis["paragraphs"]]}


@pytest.mark.usefixtures("public_source_dns")
def test_reported_conditions_cannot_disappear_from_the_frozen_review_scope(tmp_path):
    store, tools, session, delegate = setup(tmp_path)
    try:
        execute(delegate, tools, session, "open_url", {"url": URL})
        execute(delegate, tools, session, "save_finding", {
            "claim": "Only one writer can write at a time.", "source_ref": "s1",
            "passage_ref": next(iter(tools.ledger.read_passages)), "conditions": [
                {"name": "host", "state": "reported", "value": "same host only"},
                {"name": "workload", "state": "not_extracted"}]})
        report = compile_finding_report(report_args("f1"), tools.ledger, store)
        basis = review_basis(report, tools, "Explain writer concurrency.")
        assert review_scope(report, basis["task"])["claims"][0]["units"] == basis["claims"][0]["units"]
        units = basis["claims"][0]["units"]
        assert len(units) == 2 and units[-1]["field"] == "conditions.host"
        assert "same host only" in units[-1]["text"]
        raw = assessment(basis, reject=True)
        raw["claims"][0]["units"].pop()
        with pytest.raises(ValueError, match="every frozen claim unit"):
            parse_review(json.dumps(raw), basis)
    finally:
        store.close()


@pytest.mark.usefixtures("public_source_dns")
def test_review_uses_full_exact_excerpt_and_rejects_missing_duplicate_or_forged_judgments(tmp_path):
    store, tools, session, delegate = setup(tmp_path)
    try:
        tools.search.text = "Context without the essential condition. " * 20 + "Only one writer at a time."
        execute(delegate, tools, session, "open_url", {"url": URL})
        saved = execute(delegate, tools, session, "save_finding", {"claim": "Only one writer can write at a time.",
            "source_ref": "s1", "passage_ref": next(iter(tools.ledger.read_passages))})
        label = json.loads(saved.model_text)["finding_ref"]
        report = compile_finding_report(report_args(label), tools.ledger, store)
        assert report.evidence_bindings[0]["display_truncated"]
        basis = review_basis(report, tools, TASK)
        assert basis["claims"][0]["evidence"][0]["excerpt"].endswith("Only one writer at a time.")
        assert "claim" not in basis["claims"][0]["evidence"][0]
        assert "conditions" not in basis["claims"][0]["evidence"][0]
        raw = assessment(basis, reject=True)
        parsed = parse_review(json.dumps(raw), basis)
        assert {row["kind"] for row in review_issues(parsed)} == {"task_coverage", "claim_support", "report_scope"}
        assert review_issues(parsed)[0]["task_ref"] == "r2"
        for mutation in (lambda row: row["claims"].clear(),
                         lambda row: row["paragraphs"].append(row["paragraphs"][0]),
                         lambda row: row["requirements"][0].update(task_ref="invented task requirement"),
                         lambda row: row["requirements"][0].update(question_refs=[]),
                         lambda row: row["requirements"].pop(),
                         lambda row: row["requirements"].__setitem__(1, row["requirements"][0]),
                         lambda row: row["requirements"][0].update(kind="delivery"),
                         lambda row: row["claims"][0]["units"][0].update(verdict="probably"),
                         lambda row: row["claims"][0]["units"].clear()):
            corrupt = copy.deepcopy(raw)
            mutation(corrupt)
            with pytest.raises(ValueError):
                parse_review(json.dumps(corrupt), basis)
    finally:
        store.close()


def _run(root: Path, *, reject_forever=False, boundary="", resume=False, budget=22, native=False,
         missing_done=False, bad_ack=False, absent_assessment=False, task=TASK, provider_factory=None,
         planner_enabled=False, required_checks=()):
    import os
    from unittest.mock import patch

    from codey.durable import DurableSession, SQLiteDurableStore
    from codey.durable.checkpoint import operation_from_checkpoint
    from codey.knowledge.store import KnowledgeStore
    from codey.operations.research_flow import ResearchFlowDeps, run_research_pipeline
    from codey.policies.task_policy import TaskPolicy
    from codey.research.context import ResearchContext, ResearchPipelineConfig
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

    class Search:
        def search(self, query, limit=8):
            log(search=query)
            return [{"title": "Official example", "url": URL, "snippet": "One writer."}]

        def fetch(self, url):
            log(fetch=url)
            return {"status": "ok", "url": url, "title": "Official example",
                    "text": "Readers and a writer may run concurrently. Only one writer at a time.", "truncated": False}

        def close(self):
            pass

    class Provider:
        native_tools = native

        def new_chat(self):
            pass

        def send_turn(self, prompt, tools):
            from codey.providers.base import AssistantTurn, ProviderToolCall

            row = json.loads(self.send(prompt))
            return AssistantTurn(tool_calls=(ProviderToolCall(
                f"call-{state().writer_attempt}-{state().logical_turn}", row["tool"], row["args"]),))

        def send_tool_results(self, results, tools):
            return self.send_turn("\n".join(row.user_content for row in results), tools)

        def acknowledge_tool_results(self, results, tools, timeout=None):
            from codey.providers.base import AssistantTurn, ProviderToolCall

            log(ack=state().writer_attempt)
            if bad_ack and state().writer_attempt == 10:
                return AssistantTurn(tool_calls=(ProviderToolCall("unavailable", "unavailable_tool", {}),))
            return AssistantTurn()

        def send(self, prompt):
            position = state()
            attempt, turn = position.writer_attempt, position.logical_turn
            log(attempt=attempt, turn=turn)
            if attempt in (8, 10, 12):
                if absent_assessment:
                    return json.dumps({"tool": "unavailable_tool", "args": {}})
                if "FROZEN BASIS:\n" in prompt:
                    basis = json.JSONDecoder().raw_decode(prompt.split("FROZEN BASIS:\n", 1)[1])[0]
                    return json.dumps({"tool": "submit_research_review", "args": {
                        "basis_digest": basis["basis_digest"],
                        "assessment": assessment(basis, reject=attempt == 8 or reject_forever)}})
                return json.dumps({"tool": "unavailable_tool", "args": {}}) if missing_done else json.dumps(
                    {"tool": "done", "args": {"summary": "Assessment recorded."}})
            if attempt == 1:
                if turn == 1:
                    name, args = "research_plan", {"questions": ["What limits writer concurrency?"]}
                elif turn == 2:
                    name, args = "web_search", {"query": "official writer concurrency"}
                elif turn == 3:
                    name, args = "open_url", {"url": URL}
                elif turn == 4:
                    import hashlib

                    from codey.research.passages import passage_ref

                    text = "Readers and a writer may run concurrently. Only one writer at a time."
                    name, args = "save_finding", {"claim": "The database allows multiple concurrent writers.",
                        "source_ref": "s1", "passage_ref": passage_ref(URL, hashlib.sha256(text.encode()).hexdigest()[:16], 0, len(text))}
                else:
                    name, args = "done", answer_args("f1")
                    args["report_sections"]["coverage"][0]["answer"] = "The database allows multiple concurrent writers."
            elif turn == 1:
                name, args = "research_plan", {"questions": ["What limits writer concurrency?", "Which restrictions are historical?"]}
            elif turn == 2:
                # URL is allowed here as the resolved input; read identity is still pinned.
                import hashlib

                from codey.research.passages import passage_ref

                text = "Readers and a writer may run concurrently. Only one writer at a time."
                name, args = "save_finding", {"claim": "Only one writer can write at a time.",
                    "source_ref": "s1", "passage_ref": passage_ref(URL, hashlib.sha256(text.encode()).hexdigest()[:16], 0, len(text))}
            else:
                name, args = "done", answer_args("f2")
                args["report_sections"]["coverage"].append({"question_ref": "q2", "state": "not_investigated",
                    "answer": "Historical version restrictions remain unknown.", "findings": [], "investigations": []})
            from codey.research.questions import QUESTION_BOUND_TOOLS

            if name in QUESTION_BOUND_TOOLS:
                args["question_ref"] = "q1"
            return json.dumps({"tool": name, "args": args})

    store = KnowledgeStore(root / "knowledge")
    original_publish = store.publish_note

    def publish(note, *args, **kwargs):
        value = original_publish(note, *args, **kwargs)
        if note.type == "synthesis":
            log(publication=note.id)
        if boundary == "publication_committed" and note.type == "synthesis":
            os._exit(74)
        return value

    store.publish_note = publish
    if boundary:
        def committed(*_):
            position = state()
            if position.writer_attempt == 8 and position.leaf == boundary:
                os._exit(74)
            if (boundary == "publication_receipt" and position.writer_attempt == 10
                    and position.leaf == "tool_delivery_pending"):
                os._exit(74)
            if (boundary == "review_batch_committed" and position.writer_attempt == 8
                    and position.leaf == "tool_delivery_pending" and position.logical_turn == 1):
                os._exit(74)
        durable.subscribe_commits(committed)
    policy = TaskPolicy(frozenset({"control", "web.read", "knowledge.read", "knowledge.write"}), strict_research=True,
                        required_checks=required_checks)
    deps = ResearchFlowDeps(state=SimpleNamespace(durable=durable), knowledge_store=store, evidence_ledgers=None,
        search_factory=Search, run_research_advisors=None, ghost_continuity=lambda **_: None,
        managed_outputs=ManagedOutputStore(root), runtime_mutations=line)
    frame = SimpleNamespace(provider=provider_factory(Provider()) if provider_factory else Provider(),
        provider_id="local", project_text="", trace=None, run_id="r",
        research_handoff="", entry_policy=policy, request=SimpleNamespace(task=task, session_id="s", requested_capabilities=()))
    try:
        with patch("codey.operations.research_flow.ResearchPipelineConfig", return_value=ResearchPipelineConfig(enabled=planner_enabled)):
            result = run_research_pipeline(deps, frame, SimpleNamespace(on_event=lambda _: None), max_turns=budget,
                build_context=lambda *a, **kw: ResearchContext(question=task, session_id="s", run_id="r", max_turns=budget))
        return result.final_result
    finally:
        store.close()
        durable.close()


@pytest.mark.usefixtures("public_source_dns")
def test_strict_research_uses_semantic_gaps_as_its_only_followup_owner(tmp_path):
    result = _run(tmp_path, planner_enabled=True)
    assert result.stop_reason == "done" and result.semantic_review["status"] == "passed"
    calls = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert {row["attempt"] for row in calls if "attempt" in row} == {1, 8, 9, 10}
    assert len([row for row in calls if "search" in row]) == 1
    assert len([row for row in calls if "fetch" in row]) == 1
    assert len([row for row in calls if "publication" in row]) == 1


@pytest.mark.usefixtures("public_source_dns")
def test_repair_reuses_original_required_search_receipt_and_keeps_its_identity(tmp_path):
    from codey.operations.task_session import turn_effect_id
    from codey.policies.tool_requirement import require_tool_success

    checks = (require_tool_success("web_search", arguments={"query": "official writer concurrency"}),)
    result = _run(tmp_path, required_checks=checks, planner_enabled=True)
    assert result.stop_reason == "done", result.summary
    assert turn_effect_id("r:research:1", 2, 0) in result.completion_proof.evidence_refs
    calls = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert sum("search" in row for row in calls) == 1
    assert sum("publication" in row for row in calls) == 1
    before = (tmp_path / "calls.jsonl").read_bytes()
    assert _run(tmp_path, required_checks=checks, planner_enabled=True, resume=True) == result
    assert (tmp_path / "calls.jsonl").read_bytes() == before


@pytest.mark.usefixtures("public_source_dns")
@pytest.mark.parametrize("native", [False, True])
def test_shared_driver_repairs_coverage_and_support_before_publication(tmp_path, native):
    from codey.knowledge.store import KnowledgeStore

    result = _run(tmp_path, native=native)
    assert result.stop_reason == "done", result.summary
    assert result.semantic_review["status"] == "passed"
    assert result.semantic_review["kind"] == "same_provider_model_assessment"
    assert {"task_coverage_assessed", "claim_support_assessed", "report_scope_assessed"} <= {
        row.check_id for row in result.completion_proof.checks}
    assert "Only one writer" in result.summary and result.synthesis_id
    requests = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert result.turns == len([row for row in requests if "attempt" in row])
    store = KnowledgeStore(tmp_path / "knowledge")
    try:
        assert store.index._read_conn().execute("SELECT COUNT(*) FROM knowledge_versions WHERE kind='answer'").fetchone()[0] == 1
    finally:
        store.close()
    before = (tmp_path / "calls.jsonl").read_bytes()
    again = _run(tmp_path, resume=True, native=native)
    assert again == result
    assert (tmp_path / "calls.jsonl").read_bytes() == before
    from codey.research.evidence_ledger import EvidenceLedgerStore
    from codey.research.report_projection import validate_accepted_projection

    validate_accepted_projection(result, run_id="r", session_id="s", project="")
    from dataclasses import replace

    for corrupt in (
        replace(result, completion_proof=None),
        replace(result, synthesis_id="another-answer"),
        replace(result, semantic_review={**result.semantic_review, "assessment": assessment(
            review_scope(result.accepted_report, result.question), reject=True)}),
        replace(result, semantic_review={**result.semantic_review, "result_ref": "another-result"}),
    ):
        with pytest.raises(ValueError):
            validate_accepted_projection(corrupt, run_id="r", session_id="s", project="")
    ledgers = EvidenceLedgerStore(tmp_path / "history")
    def append():
        return ledgers.append_accepted_report(result.accepted_report.to_payload(), question=TASK,
            run_id="r", session_id="s", project="", synthesis_id=result.synthesis_id)
    assert append().ok
    original = ledgers.load(session_id="s", project="").payload
    path = ledgers.path_for("s", "")
    path.unlink()
    assert append().ok
    reconstructed = ledgers.load(session_id="s", project="").payload
    for key in ("sources", "evidence", "claims", "relations"):
        assert reconstructed[key] == original[key]
    assert reconstructed["records"][0]["record_digest"] == original["records"][0]["record_digest"]
    assert (tmp_path / "calls.jsonl").read_bytes() == before


@pytest.mark.usefixtures("public_source_dns")
@pytest.mark.parametrize("budget", [6, 22])
def test_failed_or_exhausted_assessment_never_returns_prior_draft_as_done(tmp_path, budget):
    result = _run(tmp_path, reject_forever=True, budget=budget)
    assert result.stop_reason != "done"
    assert result.completion_proof is None
    assert not result.synthesis_id


@pytest.mark.usefixtures("public_source_dns")
def test_last_budget_turn_completes_the_settled_assessment(tmp_path):
    from codey.knowledge.store import KnowledgeStore

    result = _run(tmp_path, budget=14)
    assert result.stop_reason == "done" and result.completion_proof.satisfied
    assert result.turns == 14
    requests = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert len([row for row in requests if "attempt" in row]) == 14
    store = KnowledgeStore(tmp_path / "knowledge")
    try:
        assert store.index._read_conn().execute("SELECT COUNT(*) FROM knowledge_versions WHERE kind='answer'").fetchone()[0] == 1
    finally:
        store.close()


@pytest.mark.usefixtures("public_source_dns")
@pytest.mark.parametrize("native", [False, True])
def test_settled_assessment_completes_without_another_model_done(tmp_path, native):
    from codey.knowledge.store import KnowledgeStore

    result = _run(tmp_path, native=native, missing_done=True)
    assert result.stop_reason == "done", result.semantic_review
    assert result.semantic_review["status"] == "passed" and result.synthesis_id
    requests = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert [(row["attempt"], row["turn"]) for row in requests if row.get("attempt") in (8, 10, 12)] == [
        (attempt, turn) for attempt in (8, 10) for turn in range(1, 4)]
    store = KnowledgeStore(tmp_path / "knowledge")
    try:
        assert store.index._read_conn().execute("SELECT COUNT(*) FROM knowledge_versions WHERE kind='answer'").fetchone()[0] == 1
    finally:
        store.close()
    before = (tmp_path / "calls.jsonl").read_bytes()
    assert _run(tmp_path, native=native, missing_done=True, resume=True) == result
    assert (tmp_path / "calls.jsonl").read_bytes() == before


@pytest.mark.usefixtures("public_source_dns")
def test_unfinished_protocol_preserves_published_judgment_without_semantic_repair(tmp_path):
    from codey.knowledge.store import KnowledgeStore

    result = _run(tmp_path, native=True, bad_ack=True)
    assert result.stop_reason == "research_review_incomplete"
    assert result.completion_proof is None
    assert result.semantic_review["status"] == "passed"
    assert result.semantic_review["completion_status"] == "incomplete"
    assert result.semantic_review["synthesis_id"] == result.synthesis_id
    requests = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert len([row for row in requests if "publication" in row]) == 1
    assert not any(row.get("attempt") in (11, 12) for row in requests)
    store = KnowledgeStore(tmp_path / "knowledge")
    try:
        assert store.index._read_conn().execute("SELECT COUNT(*) FROM knowledge_versions WHERE kind='answer'").fetchone()[0] == 1
    finally:
        store.close()
    before = (tmp_path / "calls.jsonl").read_bytes()
    assert _run(tmp_path, native=True, bad_ack=True, resume=True) == result
    assert (tmp_path / "calls.jsonl").read_bytes() == before


@pytest.mark.usefixtures("public_source_dns")
def test_no_settled_assessment_is_incomplete_and_cannot_start_semantic_repair(tmp_path):
    result = _run(tmp_path, absent_assessment=True)
    assert result.stop_reason == "research_review_incomplete" and result.completion_proof is None
    assert result.semantic_review["status"] == "incomplete"
    requests = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert not any(row.get("attempt") in (9, 10, 11, 12) or "publication" in row for row in requests)


@pytest.mark.usefixtures("public_source_dns")
@pytest.mark.parametrize("boundary", ["provider_response_ready", "kernel_settled", "publication_committed", "publication_receipt"])
def test_real_subprocess_review_restart_reuses_original_assessment(tmp_path, boundary):
    source = ("from pathlib import Path; from tests.test_research_semantic_review import _run; import sys; "
              "_run(Path(sys.argv[1]), boundary=sys.argv[2])")
    child = subprocess.run([sys.executable, "-c", source, str(tmp_path), boundary], capture_output=True, timeout=50)
    assert child.returncode == 74, child.stderr.decode(errors="replace")
    result = _run(tmp_path, resume=True)
    assert result.stop_reason == "done", result.summary
    calls = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert len([row for row in calls if row.get("attempt") == 8]) == 3
    assert len([row for row in calls if "publication" in row]) == 1
    assert len([row for row in calls if "fetch" in row]) == 1
