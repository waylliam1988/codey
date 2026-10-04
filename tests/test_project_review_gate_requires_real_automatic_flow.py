"""The project-review live gate requires automatic lifecycle facts, not model claims."""
from copy import deepcopy

import pytest

from tools import local_model_release_gate as gate
from tools.local_model_gate_project_review import check_project_review_flow


def facts():
    review = {"status": "complete", "verdict": "approved", "origin": "fresh", "finding_count": 0,
              "attempt_id": "a", "artifact_sha256": "d"}
    rows = [
        {"type": "task_start", "run_id": "r", "session_id": "s"},
        {"type": "tool", "tool_name": "edit", "ok": True, "run_id": "r", "session_id": "s"},
        {"type": "tool", "tool_name": "run", "ok": True, "exit_code": 0, "run_id": "r", "session_id": "s"},
        {"type": "review", "review": review, "run_id": "r", "session_id": "s"},
        {"type": "task_done", "stop_reason": "done", "review": review, "run_id": "r", "session_id": "s",
         "receipt": {"verification": {"checks_passed": True}}},
    ]
    requests = [{"recorder_id": "writer", "payload": {"model": "m"}},
                {"recorder_id": "reviewer", "payload": {"model": "m"}}]
    return dict(rows=rows, requests=requests, model="m", reviewer_ids=("reviewer",),
                review_read_only=True, persisted_review={**review, "self_review": True, "model_id": "known"}, independent_ok=True, exit_code=0)


def test_project_review_case_is_in_default_release_matrix():
    assert "project_review" in gate.DEFAULT_CASES


def test_complete_automatic_flow_passes():
    assert all(check_project_review_flow(**facts()).values())


@pytest.mark.parametrize("defect", ["no_request", "wrong_model", "no_review", "partial_review",
                                    "mutated", "no_artifact", "no_verification", "duplicate_terminal",
                                    "other_run", "bad_files", "review_before_verify", "failed_exit"])
def test_missing_or_conflicting_project_review_facts_fail(defect):
    data = deepcopy(facts())
    if defect == "no_request":
        data["requests"] = data["requests"][:1]
    elif defect == "wrong_model":
        data["requests"][1]["payload"]["model"] = "other"
    elif defect == "no_review":
        del data["rows"][3]
    elif defect == "partial_review":
        data["rows"][3]["review"]["status"] = "incomplete"
    elif defect == "mutated":
        data["review_read_only"] = False
    elif defect == "no_artifact":
        data["persisted_review"] = None
    elif defect == "no_verification":
        data["rows"][2]["exit_code"] = None
    elif defect == "duplicate_terminal":
        data["rows"].append(data["rows"][-1])
    elif defect == "other_run":
        data["rows"][3]["run_id"] = "other"
    elif defect == "bad_files":
        data["independent_ok"] = False
    elif defect == "review_before_verify":
        data["rows"][2], data["rows"][3] = data["rows"][3], data["rows"][2]
    elif defect == "failed_exit":
        data["exit_code"] = 1
    assert not all(check_project_review_flow(**data).values())


@pytest.mark.parametrize("defect", ["no_writer_request", "early_terminal"])
def test_review_only_or_early_terminal_cannot_pass_automatic_project_gate(defect):
    data = facts()
    if defect == "no_writer_request":
        data["requests"] = data["requests"][1:]
    else:
        data["rows"][3], data["rows"][4] = data["rows"][4], data["rows"][3]
    assert not all(check_project_review_flow(**data).values())


@pytest.mark.parametrize("unknown", [None, False, True, "0", 0.0])
def test_project_review_gate_requires_exact_zero_verification_exit(unknown):
    data = facts()
    data["rows"][2]["exit_code"] = unknown
    assert not all(check_project_review_flow(**data).values())
