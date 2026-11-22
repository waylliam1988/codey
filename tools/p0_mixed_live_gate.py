"""Run the mixed task against one local model with production receipts."""
from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path

from codey.types.json_value import parse_json_object
from tools.browser_production_live_gate import URL, run_case, select_target
from tools.completion_artifact_audit import tool_receipts
from tools.completion_explanation_ab import Target

DOC_URL = "https://example.com/production-record-format"
DOC = "<article><h1>Record format</h1><p>Amounts use integer grams and the code is a string.</p></article>"
PYTHON_CHECK = "python -m pytest"
NODE_CHECK = "npm test"
FILES = {
    "service/core.py": "AMOUNT = 0\n",
    "service/test_core.py": "from core import AMOUNT\n\ndef test_amount():\n    assert AMOUNT == 173\n",
    "service/pytest.ini": "[pytest]\n",
    "web/component.ts": 'export const code: string = "unknown";\n',
    "web/test.mjs": 'import {strictEqual} from "node:assert";\nimport {code} from "./component.ts";\nstrictEqual(code, "QUARTZ_851");\n',
    "package.json": '{"type":"module","scripts":{"test":"node web/test.mjs"}}',
}


def mixed_task(_: str) -> str:
    return (
        f"Use open_url to read {DOC_URL} and confirm the record format. "
        f"Navigate to the test page at {URL}\n"
        "Use target=task for each tool's bound URL. Observe the page, fill Locality with 福州, "
        "refresh the snapshot, click Load record, wait for text 鱼丸, and browser_read the resulting record. "
        "Then edit service/core.py AMOUNT, web/component.ts code, and result.json amount and code "
        "using the observed record. Read both code files before editing, and preserve the test files. "
        f"After all edits, use the offered verification choices for {PYTHON_CHECK} in service and {NODE_CHECK} in the root. "
        "Report the observed amount and code and verification results using done."
    )


def mixed_run(directory: Path, target: Target) -> dict[str, object]:
    report = run_case(directory, "hybrid", target, restore_snapshots=True, intent_override="auto",
        task_transform=mixed_task, project_files=FILES, source_pages={DOC_URL: DOC}, max_turns=25,
        additional_required_tools=("open_url", "run"))
    files_path = directory / "project-files.json"
    files = parse_json_object(files_path.read_text(encoding="utf-8")) if files_path.is_file() else {}
    receipts = tool_receipts(directory / "state")
    successful = [(row.result.call.args.get("command"), row.result.call.args.get("path")) for row in receipts
                  if row.result.call.name == "run" and row.result.ok and row.exit_code == 0]
    checks = ((PYTHON_CHECK, "service"), (NODE_CHECK, "."))
    passed = (report.get("passed") is True and "173" in str(files.get("service/core.py", ""))
              and "QUARTZ_851" in str(files.get("web/component.ts", ""))
              and all(pair in successful for pair in checks)
              and any(row.result.ok and row.result.call.name == "open_url" and row.result.call.args.get("url") == DOC_URL
                      for row in receipts))
    report.update(passed=passed, successful_checks=successful)
    (directory / "mixed-summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args()
    directory: Path = args.run_dir
    target = replace(select_target(180, "http://127.0.0.1:5001/v1"), context_window_tokens=65536)
    report = mixed_run(directory, target)
    print(json.dumps(report, ensure_ascii=True))
    return 0 if report.get("passed") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
