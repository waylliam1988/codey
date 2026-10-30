"""Accepted durable research keeps its completion authority at the real queue boundary."""

import copy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from codey.ghost.work_queue import GhostWorkQueueStore
from codey.ghost.work_queue_model import WORK_QUEUE_SCHEMA_VERSION, _session_ref
from codey.ghost.work_queue_sources import new_item
from codey.operations.ghost_post_turn import GhostTaskPolicyDeps, complete_or_block_work_item
from codey.research.completion_gate import ResearchCompletionGate
from tests.ghost_fixtures import seed_events
from tests.test_research_semantic_review import TASK, _run


@pytest.mark.usefixtures("public_source_dns")
@pytest.mark.parametrize("native", [False, True])
def test_accepted_durable_report_completes_claimed_queue_item_with_original_proof(tmp_path, native):
    queue = GhostWorkQueueStore(tmp_path / "queue")
    item = new_item(kind="research", status="queued", scope="session", scope_ref=_session_ref("s"), title=TASK,
        why_now="Requested evidence investigation.", priority=0.8, confidence=0.9, source="user",
        source_ref="request", evidence_refs=("research:request",), run_refs=(), now="2026-10-30T00:00:00Z")
    seed_events(queue, [{"schema_version": WORK_QUEUE_SCHEMA_VERSION, "type": "ghost_work_snapshot",
        "event_id": "seed", "ts": "2026-10-30T00:00:00Z", "reason": "test_seed", "items": [item.to_payload()]}])
    claimed = queue.claim_next(session_id="s", run_id="r", user_request="继续")
    assert claimed.ok and claimed.item is not None
    result = _run(tmp_path / "research", native=native, task=claimed.task)
    assert result.stop_reason == "done" and result.completion_proof.satisfied
    assert result.research_record.answer_status == "partial"  # An explicit, accepted historical-version gap.
    before = (tmp_path / "research/calls.jsonl").read_bytes()
    event = {"type": "task_done", "stop_reason": "done", "run_id": "r", "session_id": "s"}
    proofs = []
    warnings = []
    frame = SimpleNamespace(run_id="r", project_text="", trace=None,
        request=SimpleNamespace(task=claimed.task, session_id="s"))
    deps = GhostTaskPolicyDeps(state=SimpleNamespace(ghost_work_queue=queue, emit=warnings.append),
        record_completion_proof_trace=lambda trace, proof: proofs.append(proof))

    complete_or_block_work_item(deps, frame, event, claimed.item, research_result=result)

    final = queue.list_items(session_id="s")[0]
    assert final.status == "done", final.blocked_reason
    assert final.completed_run_id == "r"
    assert result.completion_proof.proof_id in final.proof_refs
    assert f"research:{result.synthesis_id}" in final.proof_refs
    assert proofs == [result.completion_proof] and proofs[0] is result.completion_proof
    assert not warnings
    assert (tmp_path / "research/calls.jsonl").read_bytes() == before
    queue.rebuild_from_events()
    assert queue.list_items(session_id="s")[0] == final
    complete_or_block_work_item(deps, frame, event, claimed.item, research_result=result)
    assert queue.list_items(session_id="s")[0] == final


@pytest.mark.usefixtures("public_source_dns")
def test_accepted_queue_validates_original_inputs_and_never_falls_back_to_legacy_review(tmp_path):
    result = _run(tmp_path)
    event = {"run_id": "r", "stop_reason": "done"}
    item = SimpleNamespace(kind="research", title=TASK, started_run_id="r")
    gate = ResearchCompletionGate()
    review = result.semantic_review
    corrupt_review = copy.deepcopy(review)
    corrupt_review["assessment"]["claims"][0]["verdict"] = "unsupported"
    corrupt_proofs = [replace(result.completion_proof, **mutation) for mutation in (
        {"proof_id": "completion_proof:other"}, {"contract_id": "completion_contract:other"},
        {"domain": "coding"}, {"subject_ref": "run:other:task:research"},
        {"checks": result.completion_proof.checks[1:]})]
    corrupt_results = [
        replace(result, summary="Another report."), replace(result, synthesis_id="other"),
        replace(result, semantic_review=corrupt_review),
        replace(result, semantic_review={**review, "completion_status": "incomplete"}),
        replace(result, semantic_review={**review, "result_ref": "other"}),
        replace(result, accepted_report=None),
        replace(result, completion_proof=None),
        *(replace(result, completion_proof=proof) for proof in corrupt_proofs),
    ]
    with patch("codey.research.completion_gate.review_research_proof", side_effect=AssertionError("legacy review")):
        decision = gate.evaluate(item=item, event=event, research_result=result, session_id="s", task=TASK)
        assert decision.complete and decision.proof is result.completion_proof
        for corrupt in corrupt_results:
            decision = gate.evaluate(item=item, event=event, research_result=corrupt, session_id="s", task=TASK)
            assert decision.block and decision.blocked_reason == "accepted_report_projection_invalid"
            assert decision.proof is None
        for changed in ({"event": {"run_id": "other", "stop_reason": "done"}},
                        {"session_id": "other"}, {"project": str(tmp_path)}, {"task": "Another task."},
                        {"item": SimpleNamespace(kind="research", title=TASK, started_run_id="other")}):
            args = {"item": item, "event": event, "research_result": result, "session_id": "s", "task": TASK, **changed}
            decision = gate.evaluate(**args)
            assert decision.block, changed
            assert decision.proof is None


@pytest.mark.parametrize("refs", [
    ("completion_proof:" + "a" * 16,),
    ("completion_proof:invalid", "research:answer"),
    ("completion_proof:" + "a" * 16, "research:"),
])
def test_queue_shared_proof_requires_publication_identity_and_valid_ref(tmp_path, refs):
    from tests.test_ghost_work_queue import _seed_research_work_item

    queue, item_id = _seed_research_work_item(tmp_path)
    assert queue.claim_next(session_id="s1", run_id="r", user_request="continue").ok
    result = queue.complete_item(item_id, run_id="r", proof_refs=refs)
    assert result.status == "blocked" and result.blocked_reason == "missing_proof"
