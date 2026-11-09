"""Read saved experiment artifacts; do not run a model or certify answer quality."""
from __future__ import annotations

import argparse
import json
import re
from collections.abc import Iterator
from pathlib import Path

from codey.types.json_value import JsonObject, JsonValue, decode_json_object
from tools.browser_policy_options import GUIDANCE


def request_messages(path: Path) -> Iterator[tuple[str, str]]:
    for line in path.read_text(encoding="utf-8").splitlines():
        row = decode_json_object(json.loads(line))
        payload = row.get("payload")
        if row.get("type") != "request" or not isinstance(payload, dict):
            continue
        messages = payload.get("messages")
        if not isinstance(messages, list):
            continue
        for message in messages:
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if isinstance(content, str):
                yield str(message.get("role") or ""), content


def inspect_run(path: Path) -> JsonObject:
    report = decode_json_object(json.loads(path.read_text(encoding="utf-8")))
    messages = tuple(request_messages(path.parent / "provider.jsonl"))
    tools = report.get("tools")
    sequence: list[JsonValue] = ([str(row.get("tool_name") or "") for row in tools if isinstance(row, dict)]
                                 if isinstance(tools, list) else [])
    originals = report.get("original_passages")
    evidence: JsonObject = {}
    if isinstance(originals, dict):
        for label, text in originals.items():
            if isinstance(text, str):
                lines = [line for line in text.splitlines() if re.search(
                    r"\d|例外|保全|批准|危险|通知|AND|OR|包装|冬季|夏季|版本|Packaging", line)]
                evidence[label] = {"chars": len(text), "relevant_lines": list(dict.fromkeys(lines))}
    guidance = str(report.get("guidance") or "")
    return {
        "directory": str(path.parent), "case": report.get("case"),
        "projection": report.get("projection"), "guidance": guidance,
        "screening_passed": report.get("passed"), "stop_reason": report.get("stop_reason"),
        "answer": report.get("answer"), "seconds": report.get("seconds"), "usage": report.get("usage"),
        "diagnostic_sequence_complete": report.get("diagnostic_sequence_complete"),
        "tools": sequence,
        "delta_delivered": any("Baseline from settled receipt" in content for _, content in messages),
        "compact_delivered": any("Full current observation:" in content for _, content in messages),
        "browser_contract_exposed": any(role == "system" and "- browser_snapshot:" in content
                                         for role, content in messages),
        "guidance_delivered": any(GUIDANCE[guidance] in content for _, content in messages) if guidance else False,
        "evidence_review": evidence,
    }


def inspect_suite(directory: Path) -> list[JsonValue]:
    rows: list[JsonValue] = []
    for path in sorted(directory.rglob("summary.json")):
        report = decode_json_object(json.loads(path.read_text(encoding="utf-8")))
        if "tools" in report and (path.parent / "provider.jsonl").is_file():
            rows.append(inspect_run(path))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = inspect_suite(args.run_dir)
    args.output.write_text(json.dumps({"rows": rows, "limits":
        "Branch coverage and excerpts for human review; screening is not a semantic quality verdict."},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Reviewed {len(rows)} saved outputs: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
