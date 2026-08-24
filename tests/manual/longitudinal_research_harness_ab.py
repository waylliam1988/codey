"""Longitudinal research harness: same topic across rounds, gated locally.

v1 scope (0.4.11, deterministic-only):

- Staged records for every development benchmark case run through the
  production projection stack -- proof review, evidence runtime snapshot,
  review findings, planner gaps, brief projection, impact contract,
  reproducibility capsule -- and each final round is judged by the shared
  regression gate against the frozen suite's expected observables.
- Cross-round invariants are asserted directly: superseded conclusions keep
  their content-addressed claim ids across rounds while revisions arrive as
  distinct claims linked by explicit refutes relations, stale sources get
  flagged before the revised conclusion counts, and injected unsupported
  claims stay out of implementation constraints.

The harness measures; it does not enforce. Nothing here changes production
Research behavior, prompts, or tool results. Live provider smoke rides on top
of the deterministic baseline once it stabilizes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from codey.research.analysis_run import analysis_run_record
from codey.research.brief_projection import (
    build_impact_contract,
    project_research_brief,
    render_handoff,
)
from codey.research.evidence_runtime import snapshot_from_research_record
from codey.research.proof_quality import review_research_proof
from codey.research.regression_gate import build_regression_report
from codey.research.reproducibility import build_reproducibility_capsule
from codey.research.review_finding import (
    findings_from_proof_review,
    planner_gaps_from_findings,
)
from tests.manual.research_benchmark_suite import load_suite

PROBE = "longitudinal_research_harness_ab"
FIXTURE_FILES_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "research_benchmark"


def _hex16(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def _ref(kind: str, seed: str) -> str:
    return f"{kind}:{_hex16(kind + ':' + seed)}"


QUESTION = (
    "Track the currently recommended Widget Storage endpoint across rounds "
    "and revise the conclusion when fresher primary evidence appears."
)


def _endpoint_claim_text(endpoint: str) -> str:
    return (
        f"The recommended Widget Storage endpoint is {endpoint} per the "
        "current primary source guidance."
    )


@dataclass(frozen=True)
class RoundOutcome:
    case_id: str
    round_name: str
    review_ok: bool
    report: object | None
    brief_render: str

    def passed(self) -> bool:
        return bool(self.report is not None and self.report.verdict.ok)


def _record_payload(
    *,
    seed: str,
    sources: list[dict[str, Any]],
    claims: list[dict[str, Any]],
    relations: list[dict[str, Any]] | None = None,
    answer_status: str = "answered",
    unsupported_claim_count: int = 0,
) -> dict[str, Any]:
    """Build a minimal ResearchRecord-shaped mapping with valid runtime ids.

    Evidence-backed claims automatically gain one supporting evidence row
    (with a valid locator) plus one ``supports`` relation, mirroring what the
    real research loop records. Unsupported claims pass through untouched.
    """

    evidence_rows: list[dict[str, Any]] = []
    relation_rows: list[dict[str, Any]] = list(relations or ())
    prepared_claims: list[dict[str, Any]] = []
    for index, claim in enumerate(claims):
        row = {
            "claim_id": claim["claim_id"],
            "claim_section": claim.get("claim_section", "conclusion"),
            "claim_text": claim["claim_text"],
            "status": claim.get("status", "unsupported"),
            "citation_numbers": claim.get(
                "citation_numbers", [min(index + 1, max(1, len(sources)))]
            ),
            "assumption_refs": [],
        }
        if row["status"] == "evidence_backed":
            source_ref = str(claim.get("source_ref") or "")
            evidence_id = _ref("evidence", f"{seed}:{claim['claim_id']}")
            evidence_rows.append({
                "evidence_id": evidence_id,
                "source_id": source_ref,
                "stance": "supports",
                "bounded_excerpt": str(claim.get("excerpt") or claim["claim_text"]),
                "locator": {
                    "source_id": source_ref,
                    "kind": "char_span",
                    "char_start": 40,
                    "char_end": 100,
                },
            })
            row["evidence_refs"] = [evidence_id]
            relation_rows.append({
                "relation_id": _ref("relation", f"{seed}:{claim['claim_id']}:supports"),
                "relation_kind": "supports",
                "from_ref": claim["claim_id"],
                "to_ref": evidence_id,
            })
        else:
            row["evidence_refs"] = []
        prepared_claims.append(row)

    return {
        "record_id": _ref("research_record", seed),
        "answer_status": answer_status,
        "question": {"digest_chars": len(QUESTION)},
        "sources": sources,
        "evidence": evidence_rows,
        "claims": prepared_claims,
        "assumptions": [],
        "relations": relation_rows,
        "unsupported_claim_count": unsupported_claim_count,
    }


def _source(source_seed: str, *, freshness: str, level: str = "primary") -> dict[str, Any]:
    return {
        "source_id": _ref("source", source_seed),
        "title": f"Fixture source {source_seed}",
        "quality": {"level": level, "kind": "", "freshness": freshness},
    }


def evaluate_round(
    *,
    case_id: str,
    round_name: str,
    question: str,
    record_payload: Mapping[str, Any],
    expectations: Mapping[str, bool] | None = None,
    model_said_done: bool = False,
    pipeline_payload: Mapping[str, object] | None = None,
    analysis_run_payload: Mapping[str, object] | None = None,
) -> RoundOutcome:
    """Run one staged record through the production projection stack."""

    analysis_runs = []
    if analysis_run_payload is not None:
        record = analysis_run_record(dict(analysis_run_payload))
        if record is not None:
            analysis_runs.append(record)
    review = review_research_proof(dict(record_payload), question=question)
    snapshot = snapshot_from_research_record(
        dict(record_payload),
        proof_review=review,
        analysis_runs=analysis_runs,
    )
    findings = findings_from_proof_review(review, snapshot)
    gaps = planner_gaps_from_findings(findings)
    brief = project_research_brief(
        dict(record_payload),
        snapshot=snapshot,
        findings=findings,
        planner_gaps=gaps,
        profile_id=case_id,
    )
    impact = build_impact_contract(claims=brief.claims if brief is not None else ())
    capsule = None
    if analysis_runs:
        capsule = build_reproducibility_capsule(
            run_id=f"{PROBE}:{case_id}:{round_name}",
            analysis_runs=[run.to_payload() for run in analysis_runs],
        )
    report = build_regression_report(
        case_id=f"{case_id}:{round_name}",
        snapshot=snapshot,
        proof_review=review,
        brief=brief,
        impact=impact,
        findings=findings,
        planner_gaps=gaps,
        capsule=capsule,
        sources=list(record_payload.get("sources") or ()),
        relations=list(record_payload.get("relations") or ()),
        pipeline_payload=pipeline_payload,
        expectations=dict(expectations or {}),
        model_said_done=model_said_done,
    )
    return RoundOutcome(
        case_id=case_id,
        round_name=round_name,
        review_ok=bool(review.ok),
        report=report,
        brief_render=render_handoff(brief, impact) if brief is not None else "",
    )


def scenario_stale_claim_refresh() -> tuple[list[RoundOutcome], list[str]]:
    """R1 baseline; R2 marks the old source stale and revises the conclusion.

    Production claim ids are content-addressed (section + text + citations),
    so the old stable-v2 conclusion keeps its own id across rounds and the
    stable-v3 revision arrives as a *different* claim with a *different* id,
    tied back to the superseded evidence through an explicit ``refutes``
    relation. Both stay fully supported so support relations still verify.
    """

    old_source_fresh = _source("widget-docs-v2", freshness="fresh")
    old_source_stale = _source("widget-docs-v2", freshness="stale")
    new_source = _source("widget-docs-v3-release", freshness="fresh")

    endpoint_v2_claim_id = _ref("claim", "widget:endpoint:v2")
    endpoint_v3_claim_id = _ref("claim", "widget:endpoint:v3")

    r1_record = _record_payload(
        seed="stale:r1",
        sources=[old_source_fresh],
        claims=[{
            "claim_id": endpoint_v2_claim_id,
            "claim_text": _endpoint_claim_text("stable-v2"),
            "status": "evidence_backed",
            "source_ref": old_source_fresh["source_id"],
            "excerpt": "stable-v2 remains the recommended endpoint.",
        }],
    )
    r2_record = _record_payload(
        seed="stale:r2",
        sources=[old_source_stale, new_source],
        claims=[
            {
                # Same content as R1's conclusion, therefore the same id.
                "claim_id": endpoint_v2_claim_id,
                "claim_text": _endpoint_claim_text("stable-v2"),
                "status": "evidence_backed",
                "source_ref": old_source_stale["source_id"],
                "excerpt": "stable-v2 was the recommended endpoint.",
            },
            {
                # Different content, therefore a genuinely different claim.
                "claim_id": endpoint_v3_claim_id,
                "claim_text": _endpoint_claim_text("stable-v3"),
                "status": "evidence_backed",
                "source_ref": new_source["source_id"],
                "excerpt": "stable-v3 supersedes stable-v2 as of this release.",
            },
        ],
    )
    # The revision refutes the superseded conclusion's evidence base; the
    # builder emits supporting evidence rows in claim order, so index 0 is
    # the stable-v2 evidence.
    superseded_evidence_id = r2_record["evidence"][0]["evidence_id"]
    r2_record["relations"].append({
        "relation_id": _ref("relation", "stale:r2:v3-refutes-v2-evidence"),
        "relation_kind": "refutes",
        "from_ref": endpoint_v3_claim_id,
        "to_ref": superseded_evidence_id,
    })

    suite = load_suite()
    final_expectations = suite.case_expectations("stale_claim_refresh")
    outcomes = [
        evaluate_round(
            case_id="stale_claim_refresh",
            round_name="r1_baseline",
            question=QUESTION,
            record_payload=r1_record,
        ),
        evaluate_round(
            case_id="stale_claim_refresh",
            round_name="r2_refresh",
            question=QUESTION,
            record_payload=r2_record,
            expectations=final_expectations,
        ),
    ]
    checks = [
        outcomes[0].report is not None and outcomes[1].report is not None,
        # Relocation: the old conclusion keeps its content-addressed id.
        r1_record["claims"][0]["claim_id"] == r2_record["claims"][0]["claim_id"],
        # Revision: the new conclusion is a distinct claim, linked to the
        # superseded evidence instead of reusing the old slot.
        r2_record["claims"][1]["claim_id"] == endpoint_v3_claim_id,
        any(relation.get("relation_kind") == "refutes" for relation in r2_record["relations"]),
        outcomes[0].report is not None and not outcomes[0].report.observable("stale_source_flagged"),
        outcomes[1].report is not None and outcomes[1].report.observable("stale_source_flagged"),
        outcomes[1].passed(),
    ]
    reasons = [f"stale_check_{index}" for index, ok in enumerate(checks) if not ok]
    return outcomes, reasons


def scenario_conflicting_evidence_gap() -> tuple[list[RoundOutcome], list[str]]:
    """A verified rate-limit conclusion, a contradicting vendor source, and an
    unsupported strong claim must surface conflict facts and a planner gap."""

    vendor_a = _source("vendor-a-docs", freshness="fresh")
    vendor_b = _source("vendor-b-blog", freshness="fresh", level="secondary")
    limit_claim_id = _ref("claim", "conflict:rate-limit")
    counter_evidence_id = _ref("evidence", "conflict:vendor-b-counter")

    record = _record_payload(
        seed="conflict:r1",
        sources=[vendor_a, vendor_b],
        claims=[
            {
                "claim_id": limit_claim_id,
                "claim_text": (
                    "Vendor A documents a Widget Storage API limit of 1000 requests "
                    "per minute."
                ),
                "status": "evidence_backed",
                "source_ref": vendor_a["source_id"],
                "excerpt": "Rate limit: 1000 requests per minute.",
            },
            {
                "claim_id": _ref("claim", "conflict:unlimited"),
                "claim_text": "Vendor B guarantees unlimited Widget Storage scale.",
                "status": "unsupported",
                "citation_numbers": [2],
            },
        ],
        relations=[
            {
                "relation_id": _ref("relation", "conflict:refutes"),
                "relation_kind": "refutes",
                "from_ref": limit_claim_id,
                "to_ref": counter_evidence_id,
            }
        ],
    )
    record["unsupported_claim_count"] = 1
    # The refutes relation needs its target to exist as located evidence.
    record["evidence"].append({
        "evidence_id": counter_evidence_id,
        "source_id": vendor_b["source_id"],
        "stance": "refutes",
        "bounded_excerpt": "Vendor B marketing page claims unlimited scale.",
        "locator": {
            "source_id": vendor_b["source_id"],
            "kind": "char_span",
            "char_start": 30,
            "char_end": 95,
        },
    })

    suite = load_suite()
    expectations = suite.case_expectations("conflicting_evidence_gap")
    outcome = evaluate_round(
        case_id="conflicting_evidence_gap",
        round_name="r1_conflict",
        question=QUESTION,
        record_payload=record,
        expectations=expectations,
    )
    report = outcome.report
    checks = [
        report is not None,
        report is not None and report.observable("counterevidence_checked"),
        report is not None and report.observable("conflicting_evidence_finding"),
        report is not None and report.observable("planner_gap_created"),
        outcome.passed(),
    ]
    reasons = [f"conflict_check_{index}" for index, ok in enumerate(checks) if not ok]
    return [outcome], reasons


def scenario_unsupported_claim_injection() -> tuple[list[RoundOutcome], list[str]]:
    """An injected forum claim stays visible as unsupported but never reaches
    implementation constraints; the earlier verified claim stays relocatable."""

    primary = _source("widget-docs-stable", freshness="fresh")
    forum = _source("forum-rumor", freshness="undated", level="secondary")
    endpoint_claim_id = _ref("claim", "injection:endpoint")

    r1_record = _record_payload(
        seed="inject:r1",
        sources=[primary],
        claims=[{
            "claim_id": endpoint_claim_id,
            "claim_text": _endpoint_claim_text("stable-v2"),
            "status": "evidence_backed",
            "source_ref": primary["source_id"],
            "excerpt": "stable-v2 remains the recommended endpoint.",
        }],
    )
    r2_record = _record_payload(
        seed="inject:r2",
        sources=[primary, forum],
        claims=[
            {
                "claim_id": endpoint_claim_id,
                "claim_text": _endpoint_claim_text("stable-v2"),
                "status": "evidence_backed",
                "source_ref": primary["source_id"],
                "excerpt": "stable-v2 remains the recommended endpoint.",
            },
            {
                "claim_id": _ref("claim", "injection:migration-deadline"),
                "claim_section": "risk",
                "claim_text": "A forum post claims stable-v2 dies next quarter.",
                "status": "unsupported",
            },
        ],
        unsupported_claim_count=1,
    )

    suite = load_suite()
    final_expectations = suite.case_expectations("unsupported_claim_injection")
    outcomes = [
        evaluate_round(
            case_id="unsupported_claim_injection",
            round_name="r1_verified",
            question=QUESTION,
            record_payload=r1_record,
        ),
        evaluate_round(
            case_id="unsupported_claim_injection",
            round_name="r2_injected",
            question=QUESTION,
            record_payload=r2_record,
            expectations=final_expectations,
        ),
    ]
    final_report = outcomes[1].report
    checks = [
        outcomes[0].report is not None and outcomes[1].report is not None,
        r1_record["claims"][0]["claim_id"] == r2_record["claims"][0]["claim_id"],
        final_report is not None and final_report.observable("unsupported_claim_present"),
        final_report is not None and not final_report.observable("unsupported_in_constraints"),
        outcomes[1].passed(),
    ]
    reasons = [f"injection_check_{index}" for index, ok in enumerate(checks) if not ok]
    return outcomes, reasons


def scenario_local_csv_pdf_analysis(*, command_failed: bool = False) -> tuple[list[RoundOutcome], list[str]]:
    """Local CSV/PDF conclusions must cite an actually captured AnalysisRun;
    a failed run can never be reported as reproduced."""

    csv_path = FIXTURE_FILES_ROOT / "files" / "local_table.csv"
    csv_bytes = csv_path.read_bytes()
    exit_code = 1 if command_failed else 0
    analysis_run_payload = {
        "command": (
            "python -B tools/benchmark_sum.py "
            "tests/fixtures/research_benchmark/files/local_table.csv"
        ),
        "tool_id": "0:0",
        "tool_name": "run",
        "started_at": "2026-08-24T00:00:00Z",
        "finished_at": "2026-08-24T00:00:01Z",
        "duration_ms": 850,
        "exit_code": exit_code,
        "ok": not command_failed,
        "managed_output": {
            "handle": f"artifact-{_hex16('local-analysis')}",
            "sha256": hashlib.sha256(csv_bytes).hexdigest(),
            "stored_truncated": False,
        },
        "project": "",
        "cwd": ".",
    }

    csv_source = _source("local-table-csv", freshness="fresh", level="primary")
    report_source = _source("local-report-pdf", freshness="fresh", level="secondary")
    revenue_claim_id = _ref("claim", "analysis:q4-revenue")
    record = _record_payload(
        seed="analysis:r1",
        sources=[csv_source, report_source],
        claims=[{
            "claim_id": revenue_claim_id,
            "claim_text": (
                "Q4 revenue reached 141250 USD while churn stayed at 4.7 percent."
            ),
            "status": "evidence_backed",
            "source_ref": csv_source["source_id"],
            "excerpt": "2025-Q4,141250,0.047",
        }],
    )

    suite = load_suite()
    expectations = suite.case_expectations("local_csv_pdf_analysis")
    outcome = evaluate_round(
        case_id="local_csv_pdf_analysis",
        round_name="r1_analysis",
        question=QUESTION,
        record_payload=record,
        expectations={} if command_failed else expectations,
        analysis_run_payload=analysis_run_payload,
    )
    report = outcome.report
    checks = [
        report is not None and report.observable("analysis_run_observed"),
        report is not None and (
            report.metrics["capsule_reproduction_status"]
            == ("failed" if command_failed else "output_captured")
        ),
        report is not None and (
            not report.observable("reproducible_analysis")
            if command_failed
            else report.observable("reproducible_analysis")
        ),
    ]
    if not command_failed:
        checks.append(outcome.passed())
    else:
        # Honesty gate: expecting reproduction from a failed run must fail.
        failed_expectation = evaluate_round(
            case_id="local_csv_pdf_analysis",
            round_name="r1_analysis_failed_overclaim",
            question=QUESTION,
            record_payload=record,
            expectations={"reproducible_analysis": True},
            analysis_run_payload=analysis_run_payload,
        )
        checks.append(failed_expectation.report is not None and not failed_expectation.passed())
    reasons = [f"analysis_check_{index}" for index, ok in enumerate(checks) if not ok]
    return [outcome], reasons


SCENARIOS = {
    "stale_claim_refresh": scenario_stale_claim_refresh,
    "conflicting_evidence_gap": scenario_conflicting_evidence_gap,
    "unsupported_claim_injection": scenario_unsupported_claim_injection,
    "local_csv_pdf_analysis": scenario_local_csv_pdf_analysis,
}


def development_case_ids() -> tuple[str, ...]:
    return load_suite().development_case_ids()


def run_deterministic(cases: Sequence[str]) -> int:
    failures: list[str] = []
    summary: dict[str, Any] = {}
    for case_id in cases:
        scenario = SCENARIOS.get(case_id)
        if scenario is None:
            failures.append(f"{case_id}: no deterministic scenario")
            continue
        outcomes, reasons = scenario()
        summary[case_id] = {
            "rounds": [
                {
                    "round": outcome.round_name,
                    "gate_ok": outcome.passed(),
                    "verdict": (
                        outcome.report.verdict.to_payload()
                        if outcome.report is not None
                        else None
                    ),
                }
                for outcome in outcomes
            ],
            "reason_codes": reasons,
        }
        failures.extend(f"{case_id}: {reason}" for reason in reasons)
    print(json.dumps({"probe": PROBE, "mode": "deterministic", "summary": summary}, indent=2))
    if failures:
        for failure in failures:
            print(f"GATE FAIL: {failure}", file=sys.stderr)
        return 1
    print("deterministic longitudinal gate passed")
    return 0


def _self_test() -> None:
    suite = load_suite()
    dev_cases = suite.development_case_ids()
    assert set(SCENARIOS) <= set(dev_cases), "scenarios must map to development cases"

    exit_code = run_deterministic(list(dev_cases))
    assert exit_code == 0

    # Scenario-level invariants beyond the gate itself.
    stale_outcomes, stale_reasons = scenario_stale_claim_refresh()
    assert not stale_reasons, stale_reasons
    assert "stable-v3" in stale_outcomes[1].brief_render

    injection_outcomes, injection_reasons = scenario_unsupported_claim_injection()
    assert not injection_reasons, injection_reasons
    assert "[unsupported]" in injection_outcomes[1].brief_render

    conflict_outcomes, conflict_reasons = scenario_conflicting_evidence_gap()
    assert not conflict_reasons, conflict_reasons

    _, failed_reasons = scenario_local_csv_pdf_analysis(command_failed=True)
    assert not failed_reasons, failed_reasons

    # No round may serialize raw material into its report payload.
    for outcomes in (stale_outcomes, injection_outcomes, conflict_outcomes):
        for outcome in outcomes:
            serialized = json.dumps(outcome.report.to_payload()).lower()
            for banned in ('"prompt"', '"reply"', '"transcript"', '"webpage"'):
                assert banned not in serialized, banned


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument(
        "--case",
        action="append",
        help="case name or comma list; defaults to all development cases",
    )
    args = parser.parse_args(argv)
    if args.self_test:
        _self_test()
        print("self-test ok")
        return 0
    cases = development_case_ids()
    if args.case:
        selected: list[str] = []
        for raw in args.case:
            selected.extend(part.strip() for part in raw.split(",") if part.strip())
        unknown = [name for name in selected if name not in SCENARIOS]
        if unknown:
            raise SystemExit(f"unknown cases: {unknown}")
        cases = tuple(dict.fromkeys(selected))
    return run_deterministic(list(cases))


if __name__ == "__main__":
    raise SystemExit(main())
