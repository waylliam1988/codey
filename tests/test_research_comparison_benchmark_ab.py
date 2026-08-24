from __future__ import annotations

import json
from pathlib import Path

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


def test_superiority_wording_is_gated_by_a_validated_artifact(tmp_path: Path) -> None:
    results = benchmark.run_deterministic_arms()

    plain = json.dumps(benchmark.build_summary(results=results))
    assert benchmark.STYLE_CLAIM in plain
    assert benchmark.SUPERIORITY_PHRASE not in plain

    # No artifact at all: claiming superiority fails the verdict.
    unbacked = benchmark.compare_verdict(results, superiority_claimed=True)
    assert unbacked["ok"] is False
    assert unbacked["criteria"]["superiority_claim_backed_by_artifact"] is False

    # A bare digest with no payload is not a validated record.
    digest_only = benchmark.HeadToHeadArtifact(
        digest="sha256:" + "cd" * 32, payload={}, errors=()
    )
    digest_locked = json.dumps(
        benchmark.build_summary(
            results=results,
            head_to_head=digest_only,
            superiority_claimed=True,
        )
    )
    assert benchmark.SUPERIORITY_PHRASE not in digest_locked

    # An arbitrary local file (not a metadata artifact) stays locked.
    junk_path = tmp_path / "random.bin"
    junk_path.write_bytes(b"not a head-to-head record")
    junk = benchmark.load_head_to_head_artifact(junk_path)
    assert not junk.valid
    junk_summary = benchmark.build_summary(
        results=results, head_to_head=junk, superiority_claimed=True
    )
    assert benchmark.SUPERIORITY_PHRASE not in json.dumps(junk_summary)
    assert junk_summary["real_openscience"]["errors"]

    # A schema-valid artifact whose recorded result supports Codey unlocks
    # the wording and records its metadata.
    valid_payload = benchmark._sample_head_to_head_payload()
    valid_path = tmp_path / "head_to_head.json"
    valid_path.write_text(json.dumps(valid_payload), encoding="utf-8")
    valid = benchmark.load_head_to_head_artifact(valid_path)
    assert valid.valid
    assert valid.supports_superiority()
    backed = benchmark.build_summary(
        results=results,
        head_to_head=valid,
        superiority_claimed=True,
    )
    serialized = json.dumps(backed)
    assert benchmark.SUPERIORITY_PHRASE in serialized
    assert backed["verdict"]["criteria"]["superiority_claim_backed_by_artifact"] is True
    metadata = backed["real_openscience"]["metadata"]
    assert metadata["openscience"]["commit"] == valid_payload["openscience"]["commit"]
    assert metadata["rubric"] == "research_benchmark_v1"
    assert metadata["task_inputs"]
    assert metadata["winner"] == "codey"
    assert backed["real_openscience"]["supports_superiority"] is True


def test_superiority_requires_the_result_to_support_it(tmp_path: Path) -> None:
    results = benchmark.run_deterministic_arms()

    # The reviewer exploit: schema-valid-looking metadata with an
    # editorialized result_source. Without the structured result fields the
    # artifact is invalid; even with them, only winner/count/gates decide.
    editorial = benchmark._sample_head_to_head_payload()
    for field in ("winner", "strictly_better_metric_count", "regression_gates_passed"):
        editorial.pop(field)
    editorial["result_source"] = "OpenScience wins by all metrics"
    editorial_path = tmp_path / "editorial.json"
    editorial_path.write_text(json.dumps(editorial), encoding="utf-8")
    recorded = benchmark.load_head_to_head_artifact(editorial_path)
    assert not recorded.valid
    assert not recorded.supports_superiority()
    locked = json.dumps(
        benchmark.build_summary(
            results=results, head_to_head=recorded, superiority_claimed=True
        )
    )
    assert benchmark.SUPERIORITY_PHRASE not in locked

    # A fully valid record that says OpenScience won also stays locked.
    for index, (field, value) in enumerate((
        ("winner", "openscience"),
        ("winner", "tie"),
        ("strictly_better_metric_count", benchmark.MIN_STRICTLY_BETTER_METRICS - 1),
        ("regression_gates_passed", False),
    )):
        opposing = json.loads(json.dumps(benchmark._sample_head_to_head_payload()))
        opposing[field] = value
        path = tmp_path / f"opposing-{index}.json"
        path.write_text(json.dumps(opposing), encoding="utf-8")
        artifact = benchmark.load_head_to_head_artifact(path)
        if isinstance(value, str) and value in benchmark.WINNER_VALUES:
            # Different winners are still schema-valid records of a loss/tie.
            assert artifact.valid, (field, artifact.errors)
        else:
            assert not artifact.valid or not artifact.supports_superiority(), field
        assert not artifact.supports_superiority(), field
        summary = benchmark.build_summary(
            results=results, head_to_head=artifact, superiority_claimed=True
        )
        assert summary["verdict"]["ok"] is False, field
        assert benchmark.SUPERIORITY_PHRASE not in json.dumps(summary), field


def test_style_claim_reflects_the_verdict_not_a_constant() -> None:
    results = benchmark.run_deterministic_arms()

    passing = benchmark.build_summary(results=results)
    assert passing["openscience_claim"] == benchmark.STYLE_CLAIM

    failing = benchmark.build_summary(results=results[:2])
    assert failing["verdict"]["ok"] is False
    assert failing["openscience_claim"] == ""
    assert benchmark.STYLE_CLAIM not in json.dumps(failing)


def test_artifact_schema_requires_every_roadmap_field() -> None:
    payload = benchmark._sample_head_to_head_payload()
    assert benchmark.head_to_head_artifact_errors(payload) == ()

    required_paths = {
        ".".join(path) for path in benchmark.REQUIRED_HEAD_TO_HEAD_TEXT_FIELDS
    }
    for dotted in sorted(required_paths):
        broken = json.loads(json.dumps(payload))
        node: dict = broken
        keys = dotted.split(".")
        for key in keys[:-1]:
            node = node[key]
        del node[keys[-1]]
        errors = benchmark.head_to_head_artifact_errors(broken)
        assert f"artifact_missing:{dotted}" in errors, dotted

    empty_tasks = json.loads(json.dumps(payload))
    empty_tasks["task_inputs"] = []
    assert "artifact_missing:task_inputs" in benchmark.head_to_head_artifact_errors(empty_tasks)

    for dotted, bad_value in (
        ("winner", "nobody"),
        ("strictly_better_metric_count", -1),
        ("strictly_better_metric_count", True),
        ("strictly_better_metric_count", 10 ** 6),
        ("regression_gates_passed", "yes"),
    ):
        malformed = json.loads(json.dumps(payload))
        malformed[dotted] = bad_value
        errors = benchmark.head_to_head_artifact_errors(malformed)
        assert f"artifact_bad:{dotted}" in errors, (dotted, bad_value)


def test_artifact_fields_are_enforced_bounded(tmp_path: Path) -> None:
    payload = benchmark._sample_head_to_head_payload()
    assert benchmark.head_to_head_artifact_errors(payload) == ()

    oversized = json.loads(json.dumps(payload))
    oversized["provider"] = "p" * (benchmark.MAX_ARTIFACT_FIELD_CHARS + 1)
    errors = benchmark.head_to_head_artifact_errors(oversized)
    assert "artifact_bad:provider" in errors
    # Validity re-derives from the payload, so oversize fails everywhere.
    bloated = benchmark.HeadToHeadArtifact(digest="sha256:" + "ab" * 32, payload=oversized, errors=())
    assert not bloated.valid

    too_many_tasks = json.loads(json.dumps(payload))
    too_many_tasks["task_inputs"] = [
        f"task-{index}" for index in range(benchmark.MAX_TASK_INPUTS + 1)
    ]
    assert "artifact_bad:task_inputs" in benchmark.head_to_head_artifact_errors(too_many_tasks)

    long_task = json.loads(json.dumps(payload))
    long_task["task_inputs"] = ["t" * (benchmark.MAX_TASK_INPUT_CHARS + 1)]
    assert "artifact_bad:task_inputs" in benchmark.head_to_head_artifact_errors(long_task)


def test_unreadable_or_non_object_artifacts_fail_closed(tmp_path: Path) -> None:
    broken_json = tmp_path / "broken.json"
    broken_json.write_text("{not json", encoding="utf-8")
    unreadable = benchmark.load_head_to_head_artifact(broken_json)
    assert not unreadable.valid
    assert unreadable.errors == ("artifact_unreadable_json",)

    list_file = tmp_path / "list.json"
    list_file.write_text("[1, 2, 3]", encoding="utf-8")
    not_object = benchmark.load_head_to_head_artifact(list_file)
    assert not not_object.valid
    assert not_object.errors == ("artifact_not_object",)

    # A directory (or any unreadable path) fails closed instead of raising.
    directory = benchmark.load_head_to_head_artifact(tmp_path)
    assert not directory.valid
    assert not directory.supports_superiority()
    assert directory.errors == ("artifact_unreadable_file",)
    assert directory.digest == ""


def test_summary_carries_only_bounded_projection_material() -> None:
    summary = benchmark.build_summary(results=benchmark.run_deterministic_arms())
    serialized = json.dumps(summary).lower()
    for banned in ('"prompt"', '"reply"', '"transcript"', '"webpage"'):
        assert banned not in serialized, banned
    assert summary["real_openscience"]["skipped_reason"]
