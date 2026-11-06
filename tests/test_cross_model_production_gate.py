from copy import deepcopy

import pytest

from tests.manual.cross_model_production_gate import gate_checks


def evidence():
    review = {"verdict": "approved", "status": "complete", "origin": "fresh",
              "attempt_id": "attempt", "artifact_sha256": "artifact"}
    return {
        "writer_model": "local-model", "reviewer_model": "online-model", "expected_review_identity": "identity",
        "result": {"exit_code": 0, "stop_reason": "done", "run_id": "r", "session_id": "s"},
        "events": [
            {"type": "tool", "tool_name": "edit", "ok": True},
            {"type": "tool", "tool_name": "run", "ok": True, "exit_code": 0},
            {"type": "review", "review": review},
            {"type": "task_done", "run_id": "r", "session_id": "s", "review": review,
             "receipt": {"verification": {"checks_passed": True}}},
        ],
        "persisted_review": {**review, "self_review": False, "model_id": "identity"},
        "requests": [{"model": "local-model", "model_identity": "writer"},
                     {"model": "online-model", "model_identity": "identity"}],
        "review_snapshots": [{"before": {"pricing.py": "hash"}, "after": {"pricing.py": "hash"}}],
        "quality": {"quality_passed": True, "scope_ok": True},
    }


def test_actual_unavailable_shape_cannot_pass_as_one_review_event():
    data = evidence()
    data["events"][2] = {"type": "review", "text": "Unavailable. Continued with one model."}
    data["persisted_review"] = {}
    # This is the false-positive predicate in the previous manual gate.
    assert data["result"]["exit_code"] == 0
    assert len([e for e in data["events"] if e["type"] == "review"]) == 1
    checks = gate_checks(data)
    assert not checks["complete_review"] and not checks["persisted_independent_review"]
    assert not all(checks.values())


def test_complete_independent_lifecycle_passes():
    assert all(gate_checks(evidence()).values())


@pytest.mark.parametrize("defect", ["self_review", "missing_receipt", "wrong_model", "no_http",
                                    "mutated", "bad_product", "scope", "no_fresh_check", "duplicate_terminal"])
def test_gate_rejects_missing_or_conflicting_proof(defect):
    data = deepcopy(evidence())
    if defect == "self_review":
        data["persisted_review"]["self_review"] = True
    elif defect == "missing_receipt":
        data["persisted_review"] = {}
    elif defect == "wrong_model":
        data["requests"][1]["model_identity"] = "other-model"
    elif defect == "no_http":
        data["requests"] = data["requests"][:1]
    elif defect == "mutated":
        data["review_snapshots"][0]["after"] = {"pricing.py": "different"}
    elif defect == "bad_product":
        data["quality"]["quality_passed"] = False
    elif defect == "scope":
        data["quality"]["scope_ok"] = False
    elif defect == "no_fresh_check":
        data["events"].insert(-1, {"type": "tool", "tool_name": "edit", "ok": True})
    else:
        data["events"].append(data["events"][-1])
    assert not all(gate_checks(data).values())
