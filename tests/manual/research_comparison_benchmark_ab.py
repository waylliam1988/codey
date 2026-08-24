"""Comparison benchmark v1: Codey evidence loop vs fixed reference arms.

Deterministic by default. Three arms run over staged records through the same
production projection stack and are scored with the frozen benchmark rubric:

- ``baseline_web_report``: an unstructured web-model report -- no research
  record exists, so nothing can anchor, verify, or reproduce.
- ``openscience_style_fixture``: OpenScience-*style* discipline (verified
  citation locators, closed support relations) but no counterevidence pass
  and no local reproducible analysis.
- ``codey_evidence_loop``: the current evidence loop end to end.

Claiming ``surpassed OpenScience`` requires a recorded real head-to-head
artifact (``--openscience-artifact``) that validates against the roadmap's
metadata schema -- both sides' version/commit, provider/model, task inputs,
run date, result source, and the scoring rubric. A bare file hash or an
incomplete artifact never lifts the wording guard; without one the summary
may only say ``OpenScience-style regression passed``. The guard is enforced
in code, not just documentation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from codey.research.regression_gate import ResearchRegressionReport
from tests.manual.ab_harness_common import write_json_atomic
from tests.manual.longitudinal_research_harness_ab import (
    QUESTION,
    _hex16,
    _record_payload,
    _ref,
    _source,
    evaluate_round,
)
from tests.manual.research_benchmark_suite import load_suite

PROBE = "research_comparison_benchmark_ab"
RESULTS_DIR = Path(__file__).resolve().parent / "results"
DEFAULT_OUTPUT = RESULTS_DIR / "research_comparison_benchmark_deterministic.json"
ARM_BASELINE = "baseline_web_report"
ARM_OPENSOURCE_STYLE = "openscience_style_fixture"
ARM_CODEY = "codey_evidence_loop"
DETERMINISTIC_ARMS = (ARM_BASELINE, ARM_OPENSOURCE_STYLE, ARM_CODEY)

STYLE_CLAIM = "OpenScience-style regression passed"
SUPERIORITY_PHRASE = "surpassed OpenScience"

# Roadmap metadata contract for a real OpenScience head-to-head record. Every
# field must be present, non-empty, and bounded; an artifact that fails any
# check can never unlock the superiority wording.
MAX_ARTIFACT_FIELD_CHARS = 120
MAX_TASK_INPUTS = 16
MAX_TASK_INPUT_CHARS = 200
MAX_ARTIFACT_ERRORS = 8
REQUIRED_HEAD_TO_HEAD_FIELDS: tuple[tuple[str, ...], ...] = (
    ("openscience", "version"),
    ("openscience", "commit"),
    ("codey", "version"),
    ("codey", "commit"),
    ("provider",),
    ("model",),
    ("run_date",),
    ("result_source",),
    ("rubric",),
)


@dataclass(frozen=True)
class HeadToHeadArtifact:
    """One recorded real head-to-head run plus its schema validation result."""

    digest: str
    payload: Mapping[str, Any]
    errors: tuple[str, ...]

    @property
    def valid(self) -> bool:
        # Single source of truth is the payload itself: a bare digest or a
        # hand-assembled wrapper can never count as a validated record.
        return (
            bool(self.digest)
            and bool(self.payload)
            and not head_to_head_artifact_errors(self.payload)
        )

    def metadata(self) -> dict[str, Any]:
        """Bounded copy of the validated roadmap fields for the summary."""

        if not self.valid:
            return {}

        def _text(path: tuple[str, ...]) -> str:
            node: Any = self.payload
            for key in path:
                node = node.get(key) if isinstance(node, Mapping) else None
            return str(node or "")[:MAX_ARTIFACT_FIELD_CHARS]

        tasks = [
            str(item)[:MAX_TASK_INPUT_CHARS]
            for item in (self.payload.get("task_inputs") or ())[:MAX_TASK_INPUTS]
            if isinstance(item, str) and item.strip()
        ]
        nested = {
            "openscience": {
                "version": _text(("openscience", "version")),
                "commit": _text(("openscience", "commit")),
            },
            "codey": {
                "version": _text(("codey", "version")),
                "commit": _text(("codey", "commit")),
            },
            "provider": _text(("provider",)),
            "model": _text(("model",)),
            "task_inputs": tasks,
            "run_date": _text(("run_date",)),
            "result_source": _text(("result_source",)),
            "rubric": _text(("rubric",)),
        }
        return {key: value for key, value in nested.items() if value}


def head_to_head_artifact_errors(payload: Mapping[str, Any]) -> tuple[str, ...]:
    errors: list[str] = []

    def add(error: str) -> bool:
        if len(errors) >= MAX_ARTIFACT_ERRORS:
            errors.append("artifact_errors_truncated")
            return False
        errors.append(error)
        return True

    for path in REQUIRED_HEAD_TO_HEAD_FIELDS:
        node: Any = payload
        for key in path:
            if not isinstance(node, Mapping) or key not in node:
                if not add(f"artifact_missing:{'.'.join(path)}"):
                    return tuple(errors)
                node = None
                break
            node = node[key]
        else:
            if not isinstance(node, str) or not node.strip():
                add(f"artifact_missing:{'.'.join(path)}")

    task_inputs = payload.get("task_inputs")
    rows = task_inputs if isinstance(task_inputs, (list, tuple)) else ()
    cleaned = [
        str(item).strip()
        for item in rows[:MAX_TASK_INPUTS]
        if isinstance(item, str) and str(item).strip()
    ]
    if not cleaned:
        add("artifact_missing:task_inputs")
    elif any(len(item) > MAX_TASK_INPUT_CHARS for item in cleaned):
        add("artifact_bad:task_inputs")
    return tuple(errors)


def load_head_to_head_artifact(path: Path) -> HeadToHeadArtifact:
    digest = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return HeadToHeadArtifact(digest=digest, payload={}, errors=("artifact_unreadable_json",))
    if not isinstance(payload, Mapping):
        return HeadToHeadArtifact(digest=digest, payload={}, errors=("artifact_not_object",))
    return HeadToHeadArtifact(
        digest=digest,
        payload=payload,
        errors=head_to_head_artifact_errors(payload),
    )


@dataclass(frozen=True)
class ArmResult:
    arm: str
    score: float
    report: ResearchRegressionReport | None
    reason_codes: tuple[str, ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "arm": self.arm,
            "score": self.score,
            "anchored": self.report is not None,
            "reason_codes": list(self.reason_codes),
        }


def rubric_score(report: ResearchRegressionReport | Mapping[str, Any] | None) -> float:
    """Weighted rubric score over one regression report (or its payload)."""

    payload = (
        report.to_payload() if isinstance(report, ResearchRegressionReport) else dict(report or {})
    )
    observables = {
        str(key): bool(value)
        for key, value in (payload.get("observables") or {}).items()
    }
    metrics = payload.get("metrics") or {}
    rubric = load_suite().rubric
    total = 0.0
    for row in rubric.get("metrics", ()):
        weight = float(row.get("weight") or 0.0)
        if "observable" in row:
            value = observables.get(str(row["observable"]), False)
            earned = 1.0 if value else 0.0
        elif "negated_observable" in row:
            value = observables.get(str(row["negated_observable"]), True)
            earned = 0.0 if value else 1.0
        else:
            try:
                earned = max(0.0, min(1.0, float(metrics.get(str(row.get("metric")), 0.0))))
            except (TypeError, ValueError):
                earned = 0.0
        total += weight * earned
    return round(total, 3)


def _baseline_arm() -> ArmResult:
    # A raw web-model report has no structured record to anchor: every gated
    # fact is honestly absent rather than assumed.
    return ArmResult(
        arm=ARM_BASELINE,
        score=0.0,
        report=None,
        reason_codes=("no_structured_record",),
    )


def _openscience_style_arm() -> ArmResult:
    primary = _source("style-primary-docs", freshness="fresh")
    secondary = _source("style-secondary-paper", freshness="fresh", level="secondary")
    endpoint_claim_id = _ref("claim", "style:endpoint")
    record = _record_payload(
        seed="comparison:style",
        sources=[primary, secondary],
        claims=[{
            "claim_id": endpoint_claim_id,
            "claim_text": (
                "The recommended Widget Storage endpoint is stable-v2 per the "
                "current primary source guidance."
            ),
            "status": "evidence_backed",
            "source_ref": primary["source_id"],
            "excerpt": "stable-v2 remains the recommended endpoint.",
        }],
    )
    outcome = evaluate_round(
        case_id="comparison",
        round_name=ARM_OPENSOURCE_STYLE,
        question=QUESTION,
        record_payload=record,
    )
    return ArmResult(
        arm=ARM_OPENSOURCE_STYLE,
        score=rubric_score(outcome.report),
        report=outcome.report,
        reason_codes=("no_counterevidence_pass", "no_reproducible_analysis"),
    )


def _codey_evidence_loop_arm() -> ArmResult:
    csv_path = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "research_benchmark"
        / "files"
        / "local_table.csv"
    )
    primary = _source("loop-primary-docs", freshness="fresh")
    contradicting = _source("loop-counter-blog", freshness="fresh", level="secondary")
    endpoint_claim_id = _ref("claim", "loop:endpoint")
    counter_evidence_id = _ref("evidence", "loop:counter")
    record = _record_payload(
        seed="comparison:codey",
        sources=[primary, contradicting],
        claims=[
            {
                "claim_id": endpoint_claim_id,
                "claim_text": (
                    "The recommended Widget Storage endpoint is stable-v2 per the "
                    "current primary source guidance."
                ),
                "status": "evidence_backed",
                "source_ref": primary["source_id"],
                "excerpt": "stable-v2 remains the recommended endpoint.",
            },
            {
                "claim_id": _ref("claim", "loop:revenue"),
                "claim_text": (
                    "Q4 revenue reached 141250 USD while churn stayed at 4.7 percent."
                ),
                "status": "evidence_backed",
                "source_ref": primary["source_id"],
                "excerpt": "2025-Q4,141250,0.047",
            },
        ],
        relations=[
            {
                "relation_id": _ref("relation", "loop:refutes"),
                "relation_kind": "refutes",
                "from_ref": endpoint_claim_id,
                "to_ref": counter_evidence_id,
            }
        ],
    )
    record["evidence"].append({
        "evidence_id": counter_evidence_id,
        "source_id": contradicting["source_id"],
        "stance": "refutes",
        "bounded_excerpt": "An outdated blog post disputes the stable-v2 guidance.",
        "locator": {
            "source_id": contradicting["source_id"],
            "kind": "char_span",
            "char_start": 25,
            "char_end": 90,
        },
    })
    outcome = evaluate_round(
        case_id="comparison",
        round_name=ARM_CODEY,
        question=QUESTION,
        record_payload=record,
        analysis_run_payload={
            "command": (
                "python -B tools/benchmark_sum.py "
                "tests/fixtures/research_benchmark/files/local_table.csv"
            ),
            "tool_id": "0:0",
            "tool_name": "run",
            "started_at": "2026-08-24T00:00:00Z",
            "finished_at": "2026-08-24T00:00:01Z",
            "duration_ms": 850,
            "exit_code": 0,
            "ok": True,
            "managed_output": {
                "handle": f"artifact-{_hex16('comparison-analysis')}",
                "sha256": hashlib.sha256(csv_path.read_bytes()).hexdigest(),
                "stored_truncated": False,
            },
            "project": "",
            "cwd": ".",
        },
    )
    return ArmResult(
        arm=ARM_CODEY,
        score=rubric_score(outcome.report),
        report=outcome.report,
        reason_codes=(),
    )


def run_deterministic_arms() -> list[ArmResult]:
    return [_baseline_arm(), _openscience_style_arm(), _codey_evidence_loop_arm()]


def compare_verdict(
    results: Sequence[ArmResult],
    *,
    head_to_head: HeadToHeadArtifact | None = None,
    superiority_claimed: bool = False,
) -> dict[str, Any]:
    by_arm = {result.arm: result for result in results}
    present = sorted(by_arm)
    criteria: dict[str, bool] = {
        "matrix_complete": present == sorted(DETERMINISTIC_ARMS),
        "codey_not_below_baseline": (
            by_arm[ARM_CODEY].score >= by_arm[ARM_BASELINE].score
            if ARM_CODEY in by_arm and ARM_BASELINE in by_arm
            else False
        ),
        "codey_not_below_openscience_style": (
            by_arm[ARM_CODEY].score >= by_arm[ARM_OPENSOURCE_STYLE].score
            if ARM_CODEY in by_arm and ARM_OPENSOURCE_STYLE in by_arm
            else False
        ),
    }
    if superiority_claimed:
        criteria["superiority_claim_backed_by_artifact"] = bool(
            head_to_head is not None and head_to_head.valid
        )
    return {
        "ok": all(criteria.values()),
        "criteria": criteria,
        "reason_codes": [
            f"{name}_failed" for name, passed in criteria.items() if not passed
        ],
    }


def build_summary(
    *,
    results: Sequence[ArmResult],
    head_to_head: HeadToHeadArtifact | None = None,
    superiority_claimed: bool = False,
) -> dict[str, Any]:
    verdict = compare_verdict(
        results,
        head_to_head=head_to_head,
        superiority_claimed=superiority_claimed,
    )
    configured = head_to_head is not None and bool(head_to_head.digest)
    valid = head_to_head is not None and head_to_head.valid
    summary: dict[str, Any] = {
        "probe": PROBE,
        "mode": "deterministic",
        "arms": {result.arm: result.to_payload() for result in results},
        "openscience_claim": STYLE_CLAIM,
        "real_openscience": {
            "configured": configured,
            "artifact_valid": valid,
            "artifact_digest": "" if head_to_head is None else head_to_head.digest,
            "metadata": head_to_head.metadata() if valid else {},
            "errors": [] if valid else (list(head_to_head.errors) if head_to_head else []),
            "skipped_reason": (
                ""
                if valid
                else (
                    "head-to-head artifact incomplete; see errors"
                    if configured
                    else "no real OpenScience head-to-head artifact recorded"
                )
            ),
        },
        "verdict": verdict,
    }
    if superiority_claimed and valid:
        summary["superiority_note"] = (
            f"{SUPERIORITY_PHRASE} per recorded head-to-head {head_to_head.digest}"
        )
    return summary


def _sample_head_to_head_payload() -> dict[str, Any]:
    return {
        "openscience": {"version": "v1.2.0", "commit": "abc1234"},
        "codey": {"version": "0.4.11", "commit": "b046b99"},
        "provider": "deepseek",
        "model": "deepseek-web",
        "task_inputs": ["stale_claim_refresh", "conflicting_evidence_gap"],
        "run_date": "2026-08-24",
        "result_source": "exported OpenScience run artifacts + manual scoring notes",
        "rubric": "research_benchmark_v1",
    }


def _self_test() -> None:
    results = run_deterministic_arms()
    by_arm = {result.arm: result for result in results}
    assert by_arm[ARM_BASELINE].report is None
    assert by_arm[ARM_BASELINE].score == 0.0
    assert by_arm[ARM_CODEY].score >= by_arm[ARM_OPENSOURCE_STYLE].score > by_arm[
        ARM_BASELINE
    ].score

    summary = build_summary(results=results)
    serialized = json.dumps(summary)
    assert STYLE_CLAIM in serialized
    assert SUPERIORITY_PHRASE not in serialized, (
        "superiority wording leaked without a real head-to-head artifact"
    )
    assert summary["verdict"]["ok"] is True

    with tempfile.TemporaryDirectory(prefix="comparison-benchmark-self-") as td:
        artifact_path = Path(td) / "head_to_head.json"
        artifact_path.write_text(json.dumps(_sample_head_to_head_payload()), encoding="utf-8")
        head_to_head = load_head_to_head_artifact(artifact_path)
        assert head_to_head.valid, head_to_head.errors

        backed = build_summary(
            results=results,
            head_to_head=head_to_head,
            superiority_claimed=True,
        )
        assert backed["verdict"]["criteria"]["superiority_claim_backed_by_artifact"] is True
        assert SUPERIORITY_PHRASE in json.dumps(backed)
        metadata = backed["real_openscience"]["metadata"]
        assert metadata["openscience"]["commit"] == "abc1234"
        assert metadata["codey"]["commit"] == "b046b99"

        # A digest-only or incomplete artifact must never lift the guard.
        digest_only = HeadToHeadArtifact(digest=head_to_head.digest, payload={}, errors=())
        locked = build_summary(
            results=results,
            head_to_head=digest_only,
            superiority_claimed=True,
        )
        assert locked["verdict"]["ok"] is False
        assert SUPERIORITY_PHRASE not in json.dumps(locked)

        broken_payload = _sample_head_to_head_payload()
        del broken_payload["run_date"]
        broken_payload["task_inputs"] = []
        broken_path = Path(td) / "broken.json"
        broken_path.write_text(json.dumps(broken_payload), encoding="utf-8")
        broken = load_head_to_head_artifact(broken_path)
        assert not broken.valid
        assert any("artifact_missing:run_date" in e for e in broken.errors)
        assert any("artifact_missing:task_inputs" in e for e in broken.errors)
        still_locked = build_summary(
            results=results,
            head_to_head=broken,
            superiority_claimed=True,
        )
        assert still_locked["verdict"]["ok"] is False
        assert SUPERIORITY_PHRASE not in json.dumps(still_locked)

    unbacked = compare_verdict(results, superiority_claimed=True)
    assert unbacked["ok"] is False

    incomplete = compare_verdict(results[:2])
    assert incomplete["criteria"]["matrix_complete"] is False
    assert incomplete["ok"] is False

    for result in results:
        if result.report is None:
            continue
        serialized = json.dumps(result.report.to_payload()).lower()
        for banned in ('"prompt"', '"reply"', '"transcript"', '"webpage"'):
            assert banned not in serialized, banned


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument(
        "--openscience-artifact",
        type=Path,
        help=(
            "Path to a recorded real OpenScience head-to-head artifact; must "
            "validate against the roadmap metadata schema before the "
            "superiority wording guard can be lifted."
        ),
    )
    parser.add_argument("--claim-superiority", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.self_test:
        _self_test()
        print("self-test ok")
        return 0

    head_to_head: HeadToHeadArtifact | None = None
    if args.openscience_artifact is not None:
        artifact_path = args.openscience_artifact
        if not artifact_path.is_file():
            print(f"artifact not found: {artifact_path}", file=sys.stderr)
            return 2
        head_to_head = load_head_to_head_artifact(artifact_path)
        if not head_to_head.valid:
            for error in head_to_head.errors:
                print(f"ERROR: {error}", file=sys.stderr)
            print(
                "refusing to unlock superiority wording with an incomplete "
                "head-to-head artifact",
                file=sys.stderr,
            )
            return 2

    results = run_deterministic_arms()
    summary = build_summary(
        results=results,
        head_to_head=head_to_head,
        superiority_claimed=args.claim_superiority,
    )
    output = args.output or DEFAULT_OUTPUT
    write_json_atomic(output, summary)
    print(json.dumps(summary["verdict"], indent=2))
    print(f"report: {output}")
    return 0 if summary["verdict"]["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
