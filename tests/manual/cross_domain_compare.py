"""Pair complete fixed-suite results and reject mismatched comparison inputs."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def load_report(directory: Path):
    report = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    rows = {(row["protocol"], row["id"]): row for row in report["results"]}
    expected = {(protocol, scenario["id"]) for protocol in report["protocols"] for scenario in report["scenarios"]}
    if len(rows) != len(report["results"]) or rows.keys() != expected:
        raise ValueError("incomplete or duplicate evaluation cases")
    settings = set()
    for protocol, case in rows:
        result = json.loads((directory / f"{protocol}-{case}" / "result.json").read_text(encoding="utf-8"))
        if {key: value for key, value in result.items() if key != "requests"} != rows[(protocol, case)]:
            raise ValueError("summary differs from the retained case result")
        for request in result["requests"]:
            if not request.get("response_complete") or not request.get("path", "").endswith("/chat/completions"):
                continue
            payload = request["request"]
            settings.add(json.dumps({key: payload.get(key) for key in
                ("model", "temperature", "max_tokens", "chat_template_kwargs", "seed")}, sort_keys=True))
    if len(settings) != 1:
        raise ValueError("missing or inconsistent actual generation settings")
    return report, rows, json.loads(next(iter(settings)))


def compare(baseline: Path, candidate: Path):
    old, old_rows, old_settings = load_report(baseline)
    new, new_rows, new_settings = load_report(candidate)
    for key in ("model", "suite_sha256", "harness_sha256", "protocols"):
        if old[key] != new[key]:
            raise ValueError(f"evaluation inputs differ: {key}")
    if old_rows.keys() != new_rows.keys() or old_settings != new_settings:
        raise ValueError("evaluation cases or actual generation parameters differ")
    regressions, gains = [], []
    for identity, prior in old_rows.items():
        current = new_rows[identity]
        for metric in ("answer_ok", "citation_ok", "recovery_ok"):
            if prior[metric] and not current[metric]:
                regressions.append({"case": identity, "metric": metric})
            if not prior[metric] and current[metric]:
                gains.append({"case": identity, "metric": metric})
        if not prior["unnecessary_source_calls"] and current["unnecessary_source_calls"]:
            regressions.append({"case": identity, "metric": "unnecessary_source_calls"})

    def totals(rows):
        return {"cases": len(rows), "answers_passed": sum(row["answer_ok"] for row in rows.values()),
                "recoveries_passed": sum(row["recovery_ok"] for row in rows.values()),
                "unexpected_source_calls": sum(row["unnecessary_source_calls"] for row in rows.values()),
                "not_completed": sum(not row["completed"] for row in rows.values())}

    return {"baseline_revision": old["revision"], "candidate_revision": new["revision"],
            "suite_sha256": old["suite_sha256"], "harness_sha256": old["harness_sha256"],
            "generation_settings": old_settings, "baseline": totals(old_rows), "candidate": totals(new_rows),
            "regressions": regressions, "gains": gains,
            "accepted": bool(gains and not regressions)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = compare(args.baseline, args.candidate)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
