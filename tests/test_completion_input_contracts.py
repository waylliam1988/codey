"""Completion inputs retain authority and own their mutable boundary data."""

from dataclasses import FrozenInstanceError

import pytest

from codey.completion.verification_policy import VerificationCandidate
from codey.operations.completion_context import CompletionContext
from codey.operations.completion_gate import (
    evaluate,
    register_completion_check_provider,
    unregister_completion_check_provider,
)
from codey.operations.completion_inputs import CompletionCheckInput, ProjectCompletionFacts
from codey.operations.project_completion_checks import evaluate_project_completion
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.runtime.observe.execution_evidence import ExecutionEvidence


def session() -> TaskSession:
    return TaskSession(TaskPolicy(grants=frozenset({"control", "project.read"})))


def test_selected_candidate_survives_codec_and_conflict_blocks():
    from codey.operations.kernel_actions import _completion_context_with_ignores

    candidate = VerificationCandidate("pytest", "pkg")
    context = _completion_context_with_ignores({"selected_check": candidate}, (".cache",))
    assert context.selected_check is candidate
    facts = session()
    facts.record_edit("pkg/main.py")
    facts.selected_verification = VerificationCandidate("pytest tests/all", "pkg")
    verdict = evaluate(facts, "done", context=context)
    assert not verdict.complete
    assert verdict.proof is not None
    assert "engine_error:ValueError" in verdict.proof.reason_codes


def test_prerequisites_are_detached_from_parent_and_current_acquisition():
    from codey.runtime.core.execution_record import ExecutionRecord

    parent = session()
    parent.executed["old-search"] = ExecutionRecord(
        name="web_search", ok=True, call_id="call", excerpt="", args_digest="",
        argument_digests={"query": "abc"})
    context = CompletionContext.from_value({"research_prerequisites": parent})
    facts = context.research_prerequisites
    assert facts is not None
    parent.executed["old-search"]["argument_digests"]["query"] = "changed"
    assert facts.executed["old-search"]["argument_digests"]["query"] == "abc"
    returned = facts.executed
    returned["old-search"]["argument_digests"].clear()
    assert facts.executed["old-search"]["argument_digests"]["query"] == "abc"


def test_staged_session_does_not_share_nested_mutable_facts():
    parent = session()
    parent.last_done_args = {"knowledge_refs": [{"id": "k1", "revision": 1}]}
    parent.hit_targets["h1"] = {"url": "https://example.com", "offset": 0, "pages": ""}
    parent.record_verification("pytest", 1, True)
    staged = parent.clone_projection()
    staged.last_done_args["knowledge_refs"].clear()
    staged.hit_targets["h1"]["offset"] = 3
    staged.verifications[0]["passed"] = False
    assert parent.last_done_args["knowledge_refs"]
    assert parent.hit_targets["h1"]["offset"] == 0
    assert parent.verifications[0]["passed"] is True


@pytest.mark.parametrize("value", ["a.py", [1], {"path": "a.py"}])
def test_path_sequences_reject_malformed_context(value):
    with pytest.raises(ValueError):
        CompletionContext.from_value({"scope_files": value})


def test_changes_and_analysis_are_detached_and_have_stable_payloads():
    changes = {"files": [{"path": "a.py", "status": "M", "additions": 2}],
               "diff": "@@ test @@", "changed_count": 1, "ok": True}
    analysis = {"analysis_run_id": "a1", "command_display": "pytest", "cwd_ref": {"digest": "abc"}}
    context = CompletionContext.from_value({"changes": changes, "analysis_run_payloads": [analysis]})
    changes["files"][0]["path"] = "mutated"
    analysis["cwd_ref"]["digest"] = "mutated"
    assert context.changes.files[0].path == "a.py"
    assert context.changes.to_payload()["diff"] == "@@ test @@"
    assert context.analysis_run_payloads[0].cwd_digest == "abc"


def test_project_evaluator_neither_scans_workspace_nor_mutates_evidence(monkeypatch):
    facts = session()
    facts.record_edit("a.py")
    facts.record_verification("pytest", 1, True, exit_code=0,
                              workspace_revision=1, workspace_fingerprint="sha256:" + "a" * 64)
    facts.set_workspace_state(1, "sha256:" + "a" * 64)
    evidence = ExecutionEvidence(workspace_revision=1, workspace_fingerprint=facts.workspace_fingerprint)
    context = CompletionContext(execution_evidence=evidence)
    monkeypatch.setattr("codey.workspace.revision.workspace_fingerprint",
                        lambda *a, **kw: pytest.fail("pure evaluation scanned files"))
    snapshot = ProjectCompletionFacts.capture(facts)
    first = evaluate_project_completion(snapshot, context)
    second = evaluate_project_completion(snapshot, context)
    assert first == second
    assert evidence.checks_after_edit == []
    facts.edited_files["new.py"] = 99
    assert "new.py" not in snapshot.edited_files


def test_extension_cannot_mutate_live_session():
    facts = session()
    facts.record_edit("a.py")
    captured = []

    def extension(inputs: CompletionCheckInput):
        captured.append(inputs)
        with pytest.raises(TypeError):
            inputs.edited_files["injected.py"] = 9
        with pytest.raises(FrozenInstanceError):
            inputs.task_kind = "research"
        return []

    register_completion_check_provider("snapshot_test", extension)
    try:
        evaluate(facts, "done")
    finally:
        unregister_completion_check_provider("snapshot_test")
    assert captured
    assert facts.task_kind == "project"
    assert "injected.py" not in facts.edited_files
