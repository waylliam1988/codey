from __future__ import annotations

import json


from tests.manual import research_comparison_benchmark_ab as benchmark


def test_comparison_benchmark_self_test() -> None:
    benchmark._self_test()


def test_deterministic_scores_order_codey_above_style_above_baseline() -> None:
    by_arm = {result.arm: result for result in benchmark.run_deterministic_arms()}
    assert set(by_arm) == set(benchmark.DETERMINISTIC_ARMS)
    assert by_arm[benchmark.ARM_BASELINE].score == 0.0
    assert (
        by_arm[benchmark.ARM_CODEY].score
        >= by_arm[benchmark.ARM_OPENSOURCE_STYLE].score
        > by_arm[benchmark.ARM_BASELINE].score
    )


def test_baseline_arm_anchors_nothing_and_hides_nothing() -> None:
    baseline = benchmark._baseline_arm()
    assert baseline.report is None
    assert baseline.reason_codes == ("no_structured_record",)
    payload = baseline.to_payload()
    assert payload["anchored"] is False


def test_missing_arm_fails_the_matrix_gate() -> None:
    results = benchmark.run_deterministic_arms()
    incomplete = [result for result in results if result.arm != benchmark.ARM_OPENSOURCE_STYLE]
    verdict = benchmark.compare_verdict(incomplete)
    assert verdict["ok"] is False
    assert verdict["criteria"]["matrix_complete"] is False
    assert "matrix_complete_failed" in verdict["reason_codes"]


def test_superiority_wording_is_gated_by_a_real_artifact() -> None:
    results = benchmark.run_deterministic_arms()

    plain = json.dumps(benchmark.build_summary(results=results))
    assert benchmark.STYLE_CLAIM in plain
    assert benchmark.SUPERIORITY_PHRASE not in plain

    unbacked = benchmark.compare_verdict(
        results,
        real_artifact_digest="",
        superiority_claimed=True,
    )
    assert unbacked["ok"] is False
    assert unbacked["criteria"]["superiority_claim_backed_by_artifact"] is False

    backed = benchmark.build_summary(
        results=results,
        real_artifact_digest="sha256:" + "cd" * 32,
        superiority_claimed=True,
    )
    serialized = json.dumps(backed)
    assert benchmark.SUPERIORITY_PHRASE in serialized
    assert backed["verdict"]["criteria"]["superiority_claim_backed_by_artifact"] is True


def test_summary_carries_only_bounded_projection_material() -> None:
    summary = benchmark.build_summary(results=benchmark.run_deterministic_arms())
    serialized = json.dumps(summary).lower()
    for banned in ('"prompt"', '"reply"', '"transcript"', '"webpage"'):
        assert banned not in serialized, banned
    assert summary["real_openscience"]["skipped_reason"]
