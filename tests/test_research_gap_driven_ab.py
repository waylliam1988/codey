from __future__ import annotations

from tests.manual import research_gap_driven_ab as experiment


def test_gap_experiment_has_exact_two_arms_and_bounded_candidate() -> None:
    assert experiment.ARMS == ("baseline", "gap_directive")
    assert len(experiment.GAP_DIRECTIVE) < 600
    assert "specific uncertainty" in experiment.GAP_DIRECTIVE
    assert "primary-source limitation" in experiment.GAP_DIRECTIVE


def test_check_rejects_drifted_contract(tmp_path) -> None:
    import json

    path = tmp_path / "preregistration.json"
    path.write_text(json.dumps({"arms": list(experiment.ARMS), "treatment": "changed"}), encoding="utf-8")
    try:
        experiment.validate_preregistration(json.loads(path.read_text(encoding="utf-8")))
    except ValueError as exc:
        assert "treatment" in str(exc)
    else:
        raise AssertionError("drifted treatment was accepted")
