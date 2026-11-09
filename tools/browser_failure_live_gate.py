"""Exercise reference scopes and retained failure documents in the original kernel."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from tools.browser_research_cases import BrowserResearchCase, Document
from tools.browser_semantic_live_gate import run


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()
    directory = args.run_dir.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    diagnostic = directory / "reference_scope"
    diagnostic.mkdir()
    action_report = run(diagnostic, args.timeout, candidate=True, diagnostic=True)
    challenge = directory / "json_failure"
    challenge.mkdir()
    url = "https://example.com/research-gate/retained-json-error"
    case = BrowserResearchCase("retained_json_error", "contract",
        f"这是浏览器失败回执诊断。依次调用 open_url {url}、browser_snapshot、browser_read。"
        "这些步骤用于验证错误页面保持不可读；在执行这三个步骤之前不要提交答案，也不要再次导航。"
        "最后用中文说明这些观察能否证明样本净重，不要把错误响应当作正文，不要编造数值或引用。",
        {url: Document('{"error":{"message":"您当前请求存在异常，暂时限制本次访问。","code":40362}}'.encode(),
                       status=403, content_type="application/json; charset=utf-8")},
        (), "JSON failure stays unavailable on resume; no source passage is admitted.", abstain=True)
    error_report = run(challenge, args.timeout, candidate=True, diagnostic=False, case=case)
    tools = error_report["tools"]
    checked_failure = isinstance(tools, list) and any(
        isinstance(row, dict) and row.get("tool_name") == "browser_read" and row.get("ok") is False
        for row in tools)
    report = {"passed": action_report["passed"] is True and error_report["passed"] is True and checked_failure,
              "reference_scope": action_report["passed"], "failure_document": error_report["passed"],
              "failed_current_page_read": checked_failure}
    (directory / "summary.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
