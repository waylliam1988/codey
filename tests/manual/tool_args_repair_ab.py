"""Manual A/B test harness for Tool Argument Repair (0.5.2 baseline vs 0.5.3 canonicalization).

Compares parsing behavior, protocol repair turns, and telemetry across diverse
model output styles (standard, dialect aliases, path variations, and invalid payloads).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from codey.protocols.json_codec import JsonToolCodec


@dataclass(frozen=True)
class TestCase:
    name: str
    reply_text: str
    expected_valid_053: bool
    description: str


SAMPLE_DATASET: list[TestCase] = [
    TestCase(
        name="canonical_read",
        reply_text='{"tool":"read_file","args":{"path":"src/app.py","offset":10,"limit":50}}',
        expected_valid_053=True,
        description="Standard canonical read_file call.",
    ),
    TestCase(
        name="grep_pattern_alias",
        reply_text='{"tool":"grep","args":{"pattern":"fetch_data","path":"src"}}',
        expected_valid_053=True,
        description="grep using 'pattern' instead of 'query'.",
    ),
    TestCase(
        name="edit_old_new_alias",
        reply_text='{"tool":"edit","args":{"path":"app.py","old":"x = 1","new":"x = 2"}}',
        expected_valid_053=True,
        description="edit using 'old'/'new' instead of 'old_string'/'new_string'.",
    ),
    TestCase(
        name="edit_search_replace_alias",
        reply_text='{"tool":"edit","args":{"path":"app.py","search":"def old()","replace":"def new()"}}',
        expected_valid_053=True,
        description="edit using 'search'/'replace'.",
    ),
    TestCase(
        name="edit_before_after_alias",
        reply_text='{"tool":"edit","args":{"path":"app.py","before":"return False","after":"return True"}}',
        expected_valid_053=True,
        description="edit using 'before'/'after'.",
    ),
    TestCase(
        name="edit_json_string_replacements",
        reply_text='{"tool":"edit","args":{"path":"app.py","replacements":"[{\\"old_string\\":\\"a\\",\\"new_string\\":\\"b\\"}]"}}',
        expected_valid_053=True,
        description="edit with replacements encoded as a JSON string.",
    ),
    TestCase(
        name="edit_single_dict_wrapped",
        reply_text='{"tool":"edit","args":{"path":"app.py","replacements":{"old_string":"a","new_string":"b"}}}',
        expected_valid_053=True,
        description="edit with a single dictionary object passed to replacements.",
    ),
    TestCase(
        name="read_windows_backslashes",
        reply_text=r'{"tool":"read_file","args":{"path":"src\\utils\\helpers.py"}}',
        expected_valid_053=True,
        description="read_file with Windows-style backslashes.",
    ),
    TestCase(
        name="read_numeric_string_bounds",
        reply_text='{"tool":"read_file","args":{"path":"app.py","offset":"1","limit":"100"}}',
        expected_valid_053=True,
        description="read_file with numeric string offset and limit.",
    ),
    TestCase(
        name="references_name_alias",
        reply_text='{"tool":"find_references","args":{"name":"UserSession","path":"."}}',
        expected_valid_053=True,
        description="find_references with 'name' alias.",
    ),
    TestCase(
        name="run_cmd_alias",
        reply_text='{"tool":"run","args":{"cmd":"python -m pytest -q","path":"."}}',
        expected_valid_053=True,
        description="run with 'cmd' alias.",
    ),
    TestCase(
        name="invalid_parent_traversal_escape",
        reply_text='{"tool":"read_file","args":{"path":"../../etc/passwd"}}',
        expected_valid_053=False,
        description="Security boundary: path traversal escaping root fails closed.",
    ),
    TestCase(
        name="invalid_absolute_drive_path",
        reply_text=r'{"tool":"read_file","args":{"path":"C:\\secret.txt"}}',
        expected_valid_053=False,
        description="Security boundary: absolute drive path fails closed.",
    ),
    TestCase(
        name="invalid_json_replacements",
        reply_text='{"tool":"edit","args":{"path":"app.py","replacements":"{bad json"}}',
        expected_valid_053=False,
        description="Corrupted JSON replacements fail closed.",
    ),
    TestCase(
        name="unknown_write_file_stays_unknown",
        reply_text='{"tool":"write_file","args":{"path":"new.py","content":"A = 1"}}',
        expected_valid_053=False,
        description="Discipline: write_file remains unknown tool, no hidden alias.",
    ),
]


def run_ab_comparison() -> dict[str, Any]:
    codec = JsonToolCodec()

    valid_count = 0
    invalid_count = 0
    total_rewrites = 0
    repair_counts: dict[str, int] = {}
    details: list[dict[str, Any]] = []

    for case in SAMPLE_DATASET:
        plan = codec.parse(case.reply_text)
        is_valid = bool(plan.calls or plan.control is not None) and not plan.protocol_error

        if is_valid:
            valid_count += 1
            total_rewrites += plan.alias_rewrite_count
            for k, v in plan.arg_repair_counts.items():
                repair_counts[k] = repair_counts.get(k, 0) + v
        else:
            invalid_count += 1

        details.append({
            "name": case.name,
            "description": case.description,
            "valid": is_valid,
            "expected_053": case.expected_valid_053,
            "alias_rewrites": plan.alias_rewrite_count,
            "repairs": plan.arg_repair_counts,
            "protocol_error": plan.protocol_error,
        })

    return {
        "total_cases": len(SAMPLE_DATASET),
        "valid_count": valid_count,
        "invalid_count": invalid_count,
        "total_rewrites": total_rewrites,
        "repair_counts": repair_counts,
        "details": details,
    }


def main() -> None:
    results = run_ab_comparison()
    print("=" * 60)
    print("0.5.3 Tool Argument Repair A/B Harness Results")
    print("=" * 60)
    print(f"Total Test Cases: {results['total_cases']}")
    print(f"Valid Plans:      {results['valid_count']}")
    print(f"Invalid / Errors: {results['invalid_count']}")
    print(f"Total Rewrites:   {results['total_rewrites']}")
    print(f"Repair Counts:    {json.dumps(results['repair_counts'], indent=2)}")
    print("-" * 60)
    for item in results["details"]:
        status = "PASS" if item["valid"] == item["expected_053"] else "FAIL"
        print(f"[{status}] {item['name']}: valid={item['valid']} (rewrites={item['alias_rewrites']})")
        if item["protocol_error"]:
            print(f"       error: {item['protocol_error']}")
    print("=" * 60)


if __name__ == "__main__":
    main()
