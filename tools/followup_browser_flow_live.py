"""Exercise read-only CodeMode Flow followed by one authorized page action."""
from __future__ import annotations

import argparse
from pathlib import Path

from tools.browser_production_live_gate import URL, run_case, select_target
from tools.completion_artifact_audit import tool_receipts
from tools.completion_explanation_ab import production_source_digest, write_json


def task(_: str) -> str:
    return (f"Open {URL} and use codemode only for browser operations. First call tools.browser_snapshot with flow steps "
        "for browser_navigate {target:'task'}, browser_snapshot {}, and browser_tab_list {}. Return the flow result. "
        "Then fill Locality with 福州, refresh the snapshot, click Load record once, wait for text 鱼丸, and use "
        "browser_read to obtain the resulting record. Report the amount and code using done.")


class Options(argparse.Namespace):
    directory: Path
    base_url: str


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:5001/v1")
    options = parser.parse_args(namespace=Options())
    directory = options.directory
    before = production_source_digest()
    outcome = run_case(directory, "chat", select_target(120, options.base_url), max_turns=10,
                       task_transform=task, codemode_only=True)
    receipts = tool_receipts(directory / "state")
    supplied_flow = [row.result for row in receipts if row.result.call.name == "codemode" and row.result.ok
                     and "flow" in str(row.result.call.args) and "steps" in row.result.model_text]
    clicks = sum(row.result.call.name == "browser_click" and row.result.ok for row in receipts)
    passed = outcome.get("passed") is True and bool(supplied_flow) and clicks == 1 and before == production_source_digest()
    write_json(directory / "flow-summary.json", {"passed": passed, "stop_reason": outcome.get("stop_reason"),
        "flow_calls": len(supplied_flow), "clicks": clicks, "gate_passed": outcome.get("passed"),
        "source_stable": before == production_source_digest()})
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
