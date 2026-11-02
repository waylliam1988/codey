"""Fresh read material and explicitly authored knowledge share one publication."""

import copy
import json
import subprocess
import sys
from dataclasses import replace

import pytest

from codey.knowledge.model import KnowledgeMutation, KnowledgeObject
from codey.knowledge.note import KnowledgeNote
from codey.knowledge.store import KnowledgeStore
from codey.policies.task_policy import RESEARCH_ANSWER_REQUIRED_CHECKS
from codey.research.answer_archive import publication_command
from codey.research.answer_contract import AnswerScope, submit_answer, validate_submission
from codey.research.answer_report import AnswerReport, compile_answer_report
from codey.research.finding_report import FindingReport
from tests.test_research_deterministic_completion import _run
from tests.test_research_findings import URL, execute, setup
from tests.test_research_whole_answer import record_submission


def authored_updates(label="e1"):
    return [
        {"key": "k1", "kind": "claim", "content": "Only one writer can write at a time.",
         "conditions": {"database": {"state": "reported", "value": "same database"},
                        "throughput": {"state": "not_reported"}},
         "references": [{"ref": label, "relation": "supported_by"}]},
        {"key": "k2", "kind": "inference", "content": "Concurrent readers do not establish parallel writers.",
         "references": [{"ref": "k1", "relation": "depends_on"}]},
    ]


def answer_args(updates=None):
    return {"state": "answered", "markdown": "Readers and a writer may overlap; only one writer at a time [e1].",
            "knowledge_updates": authored_updates() if updates is None else updates}


@pytest.mark.usefixtures("public_source_dns")
def test_new_evidence_and_explicit_candidates_publish_atomically_and_rollback(tmp_path, monkeypatch):
    store, tools, session, delegate = setup(tmp_path)
    try:
        session._memory_results["read"] = execute(delegate, tools, session, "open_url", {"url": URL})
        record_submission(session, answer_args())
        report = compile_answer_report(session, tools.ledger, store, project="", session_id="first")
        assert report.to_payload()["schema"] == "research_answer_v2"
        assert FindingReport.from_payload(report.to_payload()) == report
        assert store.index._read_conn().execute("SELECT COUNT(*) FROM knowledge_versions").fetchone()[0] == 0
        command = publication_command(report, task=session.task_text, run_id="first", projection="p",
                                      note_id="first-note", session_id="first", project="", store=store)
        mutations = [KnowledgeMutation(KnowledgeObject.from_dict(row["value"]), row["expected_revision"])
                     for row in command["mutations"]]
        note = KnowledgeNote(**command["note"])
        write_note = store.write_note

        def fail_after_knowledge(*args, **kwargs):
            assert store.repository.read(mutations[-2].value.id) is not None
            raise OSError("display write failed")

        with monkeypatch.context() as patcher:
            patcher.setattr(store, "write_note", fail_after_knowledge)
            with pytest.raises(OSError, match="display write"):
                store.publish_note(note, command["commit_id"], mutations)
        assert store.index._read_conn().execute("SELECT COUNT(*) FROM knowledge_versions").fetchone()[0] == 0
        assert store.read_note(note.id) is None and store.write_note == write_note
        store.publish_note(note, command["commit_id"], mutations)
        before = store.index._read_conn().execute("SELECT COUNT(*) FROM knowledge_versions").fetchone()[0]
        store.publish_note(note, command["commit_id"], mutations)
        assert store.index._read_conn().execute("SELECT COUNT(*) FROM knowledge_versions").fetchone()[0] == before == 5
        claim = store.repository.read(mutations[2].value.id)
        inference = store.repository.read(mutations[3].value.id)
        assert claim.value.status == "candidate" and claim.value.origin == "model_extraction"
        assert claim.interpretation_review == inference.interpretation_review == "unreviewed"
        assert claim.value.conditions["throughput"] == {"state": "not_reported"}
        assert claim.value.references[0].target == report.evidence_bindings[0]["evidence_id"]
        assert inference.value.references[0].target == claim.value.id
        assert inference.value.references[0].revision == 1
        assert {row.value.scope for row in mutations} == {"personal"}
        assert any(hit.version.value.id == claim.value.id for hit in store.search("writer", session_id="later"))
    finally:
        store.close()


@pytest.mark.usefixtures("public_source_dns")
def test_update_only_evidence_is_saved_without_becoming_a_body_citation(tmp_path):
    store, tools, session, delegate = setup(tmp_path)
    try:
        session._memory_results["read1"] = execute(delegate, tools, session, "open_url", {"url": URL})
        tools.search.text = "Only one writer writes at a time in the second material."
        session._memory_results["read2"] = execute(delegate, tools, session, "open_url", {"url": URL + "-second"})
        record_submission(session, answer_args(authored_updates("e2")))
        report = compile_answer_report(session, tools.ledger, store, project="", session_id="first")
        assert {row["label"] for row in report.evidence_bindings} == {"e1", "e2"}
        assert len(report.knowledge_refs) == 1 and report.knowledge_refs[0]["id"] == report.evidence_bindings[0]["evidence_id"]
        assert "[e2]" not in report.text and "-second" not in report.text
        from codey.research.report_projection import project_accepted_report

        record = project_accepted_report(report, question=session.task_text, run_id="r", session_id="first",
                                         project="", synthesis_id="n")
        assert len(record.sources) == len(record.evidence) == 1
        claim = next(row["value"] for row in report.knowledge_mutations if row["value"]["kind"] == "claim")
        assert claim["references"][0]["target"] == report.evidence_bindings[1]["evidence_id"]
    finally:
        store.close()


@pytest.mark.parametrize("failure", ["unknown", "duplicate", "cycle", "relation", "identity", "scope", "status",
                                    "empty_claim", "empty_inference", "premise_kind", "condition", "too_many"])
def test_invalid_batch_rejects_the_entire_submission(failure):
    rows = authored_updates()
    if failure == "unknown":
        rows[0]["references"][0]["ref"] = "e99"
    elif failure == "duplicate":
        rows[1]["key"] = "k1"
    elif failure == "cycle":
        rows[1]["references"][0]["ref"] = "k2"
    elif failure == "relation":
        rows[0]["references"][0]["relation"] = "uses"
    elif failure in {"identity", "scope", "status"}:
        rows[0][{"identity": "id", "scope": "scope", "status": "status"}[failure]] = "active"
    elif failure in {"empty_claim", "empty_inference"}:
        rows[failure == "empty_inference"]["references"] = []
    elif failure == "premise_kind":
        rows[0]["kind"] = "question"
        rows[0]["references"][0]["relation"] = "motivated_by"
    elif failure == "condition":
        rows[0]["conditions"]["database"] = {"state": "reported"}
    else:
        rows *= 17
    with pytest.raises(ValueError):
        submit_answer(AnswerScope("a" * 64, (("e1", "p" + "b" * 12),), ("read",)), answer_args(rows))


def test_omitted_or_empty_batch_keeps_v1_and_old_submissions_readable():
    scope = AnswerScope("a" * 64, (("e1", "p" + "b" * 12),), ("read",))
    args = answer_args([])
    assert submit_answer(scope, args)["schema_version"] == 1
    empty = submit_answer(scope, args)
    args.pop("knowledge_updates")
    assert submit_answer(scope, args) == empty


def test_batch_forward_premises_questions_and_replay_preserve_original_arguments():
    from codey.runtime.effects.replay_args import validate_replay_args_shape

    rows = authored_updates()
    rows.reverse()
    rows.append({"key": "k3", "kind": "question", "content": "Which workload changes this writer bottleneck?",
                 "references": [{"ref": "k2", "relation": "motivated_by"}]})
    args = answer_args(rows)
    original = copy.deepcopy(args)
    scope = AnswerScope("a" * 64, (("e1", "p" + "b" * 12),), ("read",))
    assert submit_answer(scope, args)["knowledge_updates"][0]["key"] == "k2"
    assert validate_replay_args_shape("submit_research_answer", args) == original == args
    for bad in (None, {}, ["k1"], [{}] * 17):
        with pytest.raises(ValueError):
            validate_replay_args_shape("submit_research_answer", {**args, "knowledge_updates": bad})


def test_descriptive_batch_keys_and_bracketed_refs_resolve_to_the_same_frozen_targets():
    scope = AnswerScope("a" * 64, (("e1", "p" + "b" * 12),), ("read",))
    rows = authored_updates()
    rows[0]["key"] = "evidence_summary"
    rows[1]["key"] = "writer_inference"
    rows[1]["references"][0]["ref"] = "evidence_summary"
    bare = submit_answer(scope, answer_args(rows))
    rows[0]["references"][0]["ref"] = "[e1]"
    rows[1]["references"][0]["ref"] = "[evidence_summary]"
    assert submit_answer(scope, answer_args(rows)) == bare


@pytest.mark.parametrize("bad", ["e99", "[e99]", "pbbbbbbbbbbbb", "evidence-unknown", "[[e1]]"])
def test_flexible_ref_spelling_never_selects_hidden_passages_or_database_identities(bad):
    rows = authored_updates()
    rows[0]["references"][0]["ref"] = bad
    scope = AnswerScope("a" * 64, (("e1", "p" + "b" * 12),), ("read",))
    with pytest.raises(ValueError):
        submit_answer(scope, answer_args(rows))


def test_batch_keys_cannot_shadow_evidence_and_normalized_duplicate_refs_are_rejected():
    scope = AnswerScope("a" * 64, (("e1", "p" + "b" * 12),), ("read",))
    rows = authored_updates()
    rows[0]["key"] = "e1"
    with pytest.raises(ValueError, match="reserved"):
        submit_answer(scope, answer_args(rows))


def test_public_reference_slots_derive_relations_without_model_protocol_fields():
    import jsonschema

    from codey.research.knowledge_updates import updates_schema

    rows = authored_updates()
    rows[0]["references"] = ["[e1]"]
    rows[0]["limited_by"] = ["e1"]
    rows[0]["refuted_by"] = ["e1"]
    rows[1]["references"] = ["k1", "e1"]
    jsonschema.validate(rows, updates_schema())
    scope = AnswerScope("a" * 64, (("e1", "p" + "b" * 12),), ("read",))
    submission = submit_answer(scope, answer_args(rows))
    claim, inference = submission["knowledge_updates"]
    assert [ref["relation"] for ref in claim["references"]] == ["supported_by", "limited_by", "refuted_by"]
    assert inference["references"] == [{"ref": "k1", "relation": "depends_on"}, {"ref": "e1", "relation": "depends_on"}]
    rows[1]["limited_by"] = ["e1"]
    with pytest.raises(ValueError, match="on a claim"):
        submit_answer(scope, answer_args(rows))
    rows = authored_updates()
    rows[0]["references"].append({"ref": "[e1]", "relation": "supported_by"})
    with pytest.raises(ValueError, match="duplicate"):
        submit_answer(scope, answer_args(rows))


@pytest.mark.parametrize("mixed", [False, True])
@pytest.mark.usefixtures("public_source_dns")
def test_update_only_historical_material_preserves_scope_and_exact_old_source(tmp_path, mixed):
    from codey.knowledge.model import KnowledgeReference

    store, tools, session, delegate = setup(tmp_path)
    try:
        if mixed:
            source = KnowledgeObject("personal-source", "source", "A personal original source.", "source",
                                     scope="personal", scope_id=store.owner_id, provenance={"url": URL})
            store.commit_knowledge("personal", [KnowledgeMutation(source)])
            session._memory_results["body"] = execute(delegate, tools, session, "knowledge_read", {"id": source.id})
        else:
            session._memory_results["body"] = execute(delegate, tools, session, "open_url", {"url": URL})
        source = KnowledgeObject("session-source", "source", "Only one writer per database (old source).", "source",
                                 scope="session", scope_id="first", provenance={"url": URL + "-private"})
        store.commit_knowledge("session", [KnowledgeMutation(source)])
        session._memory_results["private"] = execute(delegate, tools, session, "knowledge_read", {"id": source.id, "revision": 1})
        store.commit_knowledge("new-head", [KnowledgeMutation(replace(source, content="Incompatible later source."), 1)])
        record_submission(session, answer_args(authored_updates("e2")))
        if mixed:
            with pytest.raises(ValueError, match="incompatible restricted scopes"):
                compile_answer_report(session, tools.ledger, store, project="", session_id="first")
            return
        report = compile_answer_report(session, tools.ledger, store, project="", session_id="first")
        assert report.knowledge_batch["scope"] == "session" and report.knowledge_batch["scope_id"] == "first"
        binding = report.evidence_bindings[1]
        evidence = next(row["value"] for row in report.knowledge_mutations if row["value"]["id"] == binding["evidence_id"])
        assert evidence["content"] == source.content
        assert evidence["references"] == [KnowledgeReference(source.id, 1, "from_source").__dict__]
        command = publication_command(report, task=session.task_text, run_id="r", projection="p", note_id="n",
                                      session_id="first", project="", store=store)
        assert {row["value"]["scope"] for row in command["mutations"]} == {"session"}
    finally:
        store.close()


@pytest.mark.parametrize("change", ["content", "extra", "missing", "batch", "session", "scope", "tool_args"])
@pytest.mark.usefixtures("public_source_dns")
def test_explicit_batch_does_not_allow_unrequested_mutations_or_rebinding(tmp_path, change):
    store, tools, session, delegate = setup(tmp_path)
    try:
        session._memory_results["read"] = execute(delegate, tools, session, "open_url", {"url": URL})
        record_submission(session, answer_args())
        report = compile_answer_report(session, tools.ledger, store, project="", session_id="first")
        raw = copy.deepcopy(report.to_payload())
        if change == "tool_args":
            session._memory_results["submitted"].call.args["knowledge_updates"][0]["content"] = "Changed authored claim."
            with pytest.raises(ValueError, match="arguments"):
                compile_answer_report(session, tools.ledger, store, project="", session_id="first")
            return
        if change == "content":
            raw["knowledge_mutations"][-1]["value"]["content"] = "Unauthorized conclusion."
        elif change == "extra":
            raw["knowledge_mutations"].append(copy.deepcopy(raw["knowledge_mutations"][-1]))
        elif change == "missing":
            raw["knowledge_mutations"].pop()
        elif change == "batch":
            raw["knowledge_batch"]["updates"][0]["content"] = "Changed authored claim."
        elif change == "session":
            with pytest.raises(ValueError, match="session"):
                publication_command(report, task=session.task_text, run_id="r", projection="p", note_id="n",
                                    session_id="other", project="", store=store)
            return
        else:
            batch = {**report.knowledge_batch, "scope": "project", "scope_id": "unrelated"}
            report = replace(report, knowledge_batch=batch)
            with pytest.raises(ValueError):
                publication_command(report, task=session.task_text, run_id="r", projection="p", note_id="n",
                                    session_id="first", project="", store=store)
            return
        from codey.research.answer_contract import digest

        raw["compilation_digest"] = digest({k: v for k, v in raw.items() if k != "compilation_digest"})
        with pytest.raises(ValueError):
            AnswerReport.from_payload(raw)
        submission = session._memory_results["submitted"].canonical["research_submission"]
        assert validate_submission(submission, session.task_text) == submission
    finally:
        store.close()


def run_first_pass(root, *, native=False, boundary="", resume=False):
    def factory(provider):
        def send(prompt):
            calls = root / "first-pass.jsonl"
            turn = len(calls.read_text().splitlines()) + 1 if calls.exists() else 1
            with calls.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"prompt": prompt}) + "\n")
            args = answer_args()
            for row in args["knowledge_updates"]:
                row["references"] = [ref["ref"] for ref in row["references"]]
            return json.dumps({"tool": "open_url", "args": {"url": URL}} if turn == 1 else
                              {"tool": "submit_research_answer", "args": args})
        provider.send = send
        return provider

    return _run(root, native=native, boundary=boundary, resume=resume, boundary_turn=2,
                task="Explain writer concurrency and retain a candidate claim and inference for reuse.",
                compact_planning=True, required_checks=RESEARCH_ANSWER_REQUIRED_CHECKS, provider_factory=factory)


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.usefixtures("public_source_dns")
def test_first_research_can_accumulate_without_early_commit_and_resume(tmp_path, native):
    result = run_first_pass(tmp_path, native=native)
    assert result.stop_reason == "done", result.summary
    assert result.turns == 2 and result.accepted_report.knowledge_batch
    before = (tmp_path / "first-pass.jsonl").read_bytes()
    assert run_first_pass(tmp_path, native=native, resume=True) == result
    assert (tmp_path / "first-pass.jsonl").read_bytes() == before


@pytest.mark.parametrize("boundary", ["provider_request_ready", "provider_response_ready", "tool_effect_pending",
                                      "kernel_settled", "publication_admitted", "publication_committed", "publication_receipt"])
def test_first_pass_cold_restart_keeps_exact_knowledge_and_one_answer(tmp_path, boundary):
    source = ("from pathlib import Path; import sys; "
              "from tests.test_research_first_pass_knowledge import run_first_pass; "
              "run_first_pass(Path(sys.argv[1]), boundary=sys.argv[2])")
    child = subprocess.run([sys.executable, "-c", source, str(tmp_path), boundary], capture_output=True, timeout=60)
    assert child.returncode == 74, child.stderr.decode(errors="replace")
    result = run_first_pass(tmp_path, resume=True)
    assert result.stop_reason == "done", result.summary
    assert len((tmp_path / "first-pass.jsonl").read_text().splitlines()) == 2
    store = KnowledgeStore(tmp_path / "knowledge")
    try:
        counts = store.index._read_conn().execute("SELECT kind,COUNT(*) FROM knowledge_versions GROUP BY kind").fetchall()
        assert dict(counts) == {"answer": 1, "source": 1, "evidence": 1, "claim": 1, "inference": 1}
        for row in result.accepted_report.knowledge_mutations:
            assert store.repository.read(row["value"]["id"], 1).value.to_dict() == row["value"]
    finally:
        store.close()
