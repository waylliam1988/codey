"""Export separately reviewed retention results and verify lossless evidence."""

from __future__ import annotations

import hashlib
import json
import subprocess
import zipfile
from pathlib import Path

from tests.manual import research_window_retention_ab as ab
from tests.manual import research_window_retention_report as report

REVIEW = Path("docs/reports/research-window-retention-review-20261106.json")
REPORT = Path("docs/reports/research-window-retention-ab-20261106.json")
MANIFEST = Path("docs/reports/research-window-retention-evidence-manifest-20261106.json")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_export(path, value):
    # Tracked manifest identity must survive Git's Windows newline conversion.
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def delivered(row):
    return row.get("result", {}).get("exit_code") == 0


def summary(rows):
    keys = [key for row in rows for exposure in row["actual_request_exposures"] for key in exposure["keys"]
            if key["read"]]
    return {"runs": len(rows), "delivered": sum(delivered(row) for row in rows),
        "strict_task_pass": sum(row["independent_review"]["complete_task_pass"] for row in rows),
        "all_rubric_items_pass": sum(bool(row["independent_review"]["rubric_pass"])
                                    and all(row["independent_review"]["rubric_pass"]) for row in rows),
        "delivered_reports_with_supported_citations": sum(
            row["independent_review"]["citation_supported"] is True for row in rows),
        "overflow_runs": sum(row["overflow_views"] > 0 for row in rows),
        "read_critical_quote_request_events": len(keys),
        "visible_read_critical_quote_request_events": sum(key["visible_in_actual_request"] for key in keys),
        "critical_removed_request_events": sum(row["critical_removed_in_actual_requests"] for row in rows),
        "requests": sum(row["request_count"] for row in rows),
        "tool_calls": sum(len(row["tool_calls"]) for row in rows),
        "input_tokens": sum(row["model_input_tokens"] for row in rows),
        "output_tokens": sum(row["model_output_tokens"] for row in rows),
        "seconds": round(sum(row["seconds"] for row in rows), 3)}


def main():
    end = ab.read(ab.ROOT / "source-end-verification.json")
    if not end["verified"]:
        raise RuntimeError("Missing successful frozen end-of-run verification")
    reviews = ab.read(REVIEW)
    used = set()
    for phase, expected in (("dev", 4), ("holdout", 24)):
        root = ab.phase_root(phase)
        paths = sorted((root / "results").glob("*.json"))
        if len(paths) != expected:
            raise RuntimeError(f"Incomplete {phase}: {len(paths)}/{expected}")
        grades = {}
        for path in paths:
            row = ab.read(path)
            if delivered(row):
                grades[path.stem] = reviews["reviews"][path.stem]
                used.add(path.stem)
            else:
                grades[path.stem] = {"rubric_pass": None, "citation_supported": None,
                    "complete_task_pass": False,
                    "notes": ["No published answer; failure retained in strict task denominator.",
                              row.get("result", {}).get("stop_reason", "process_failure")]}
        ab.write(root / "grades.json", grades)
    if used != set(reviews["reviews"]):
        raise RuntimeError("Unexpected or unconsumed manual review")
    report.main()
    result = ab.read(REPORT)
    held = [row for row in result["runs"] if row["phase"] == "holdout"]
    pairs = []
    for case, seed in sorted({(row["case"], row["seed"]) for row in held}):
        pair = {row["arm"]: row for row in held if (row["case"], row["seed"]) == (case, seed)}
        a, b = pair["baseline"], pair["relevance"]
        pairs.append({"case": case, "seed": seed,
            "both_overflow": a["overflow_views"] > 0 and b["overflow_views"] > 0,
            "baseline_critical_loss": a["critical_removed_in_actual_requests"] > 0,
            "baseline_pass": a["independent_review"]["complete_task_pass"],
            "candidate_pass": b["independent_review"]["complete_task_pass"]})
    roots = [Path(".e2e-artifacts") / name for name in (
        "research-window-retention-20261106", "research-window-retention-v2-20261106",
        "research-window-retention-v3-20261106")]
    source = Path(f".e2e-artifacts/research-window-retention-frozen-source-{end['commit'][:8]}.zip")
    subprocess.run(["git", "archive", "--format=zip", f"--output={source}", end["commit"],
                    "codey", "tests/manual"], check=True)
    files = sorted({p for root in roots for p in root.rglob("*") if p.is_file()} | {
        REVIEW, source, Path("tests/manual/research_window_retention_export.py"),
        Path("tests/test_research_window_retention_ab.py")})
    manifest = [{"path": p.as_posix(), "bytes": p.stat().st_size, "sha256": sha(p)} for p in files]
    archive = Path(".e2e-artifacts/research-window-retention-20261106-evidence.zip")
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        for path in files:
            output.write(path, path.as_posix())
    with zipfile.ZipFile(archive) as evidence:
        if evidence.testzip() is not None:
            raise RuntimeError("Evidence ZIP CRC failure")
        for item in manifest:
            if hashlib.sha256(evidence.read(item["path"])).hexdigest() != item["sha256"] \
                    or sha(Path(item["path"])) != item["sha256"]:
                raise RuntimeError("Evidence changed during archive: " + item["path"])
    write_export(MANIFEST, {"files": manifest, "archive_verified": True})
    pilot_rows = [ab.read(p) for root in roots[:2] for p in root.glob("dev/results/*.json")]
    if len(pilot_rows) != 8:
        raise RuntimeError("Missing retained pilot runs")
    result.update({"decision": "do_not_integrate_retention_candidate", "review_method": reviews["review_method"],
        "end_of_run_source_verification": end,
        "summary": {arm: summary([row for row in held if row["arm"] == arm]) for arm in ab.ARMS},
        "pairs": pairs,
        "gate": {"met": False, "effective_both_overflow_pairs": sum(p["both_overflow"] for p in pairs),
            "note": "One extra strict pass occurs without overflow; cannot attribute it to treatment. "
                    "No default-budget confirmation: stress experiment failed promotion prerequisites."},
        "retained_pilots": {"runs": len(pilot_rows), "delivered": sum(delivered(row) for row in pilot_rows),
                            "included_in_primary_denominator": False},
        "total_research_runs": len(pilot_rows) + len(result["runs"]),
        "metric_limits": "Tokens are backend-reported totals, not billing; visibility counts quote/request events, "
                         "not independent claims. Generation failures before overflow do not identify treatment effects.",
        "evidence_archive": {"path": archive.as_posix(), "bytes": archive.stat().st_size, "sha256": sha(archive)},
        "evidence_manifest": {"path": MANIFEST.as_posix(), "sha256": sha(MANIFEST), "files": len(files)}})
    write_export(REPORT, result)
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
    print("VERIFIED", len(files), "files;", result["total_research_runs"], "research runs")


if __name__ == "__main__":
    main()
