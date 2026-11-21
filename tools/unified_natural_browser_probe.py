"""Frozen natural task; raw requests and every failure remain in a fresh directory."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from codey.policies.tool_requirement import require_tool_success
from codey.types.json_value import JsonObject, decode_json_object, parse_json_object
from tools.browser_production_live_gate import URL, run_case, select_target
from tools.completion_artifact_audit import tool_receipts

TASK = (f"请打开 {URL}，在网页中查询福州的鱼丸净重和记录校验码。"
        "先给出带来源引用的调查报告，再把项目 result.json 中 amount 更新为实际克数（数值）、code 更新为实际校验码（字符串），"
        "保留其它字段；运行项目已有的本地检查，确认文件里的两项与网页记录一致。最后说明修改内容和检查结果。")
REPAIR_TASK = ("请先检查项目现有文件并运行项目已有检查，定位记录同步失败的原因。"
    f"需要核对真实记录时，请打开 {URL}，查询福州的净重和校验码。"
    "修复 result.json 的 amount（实际克数数值）和 code（完整字符串），保留其他字段，重新验证；"
    "给出带实际来源引用的调查说明与修复、检查结果。")
FLEXIBLE_REPAIR_TASK = (f"项目的记录同步有问题。请结合项目文件和 {URL} 中福州的真实记录查明原因，"
    "修复 result.json 的 amount（实际克数数值）和 code（完整字符串），保留其他字段。"
    "运行项目已有检查验证修复，并给出带实际来源引用的调查说明、修改内容和检查结果。")
PROJECT = {
    "result.json": '{"amount": 0, "code": "unknown", "keep": "preserved"}\n',
    "README.md": "# Record fixture\nUpdate result.json using the current public record.\nRun local checks with python -m unittest -v.\n",
    ".codey/config.json": json.dumps({"schema_version": 1, "verification": {"commands": [
        {"command": "python -m unittest -v", "cwd": ".", "label": "Record checks"}]}}),
    "test_record.py": '''import json
import unittest
from pathlib import Path

class RecordCheck(unittest.TestCase):
    def test_record(self):
        record = json.loads(Path(__file__).with_name("result.json").read_text(encoding="utf-8"))
        self.assertIs(type(record["amount"]), int)
        self.assertEqual(record["amount"], 173)
        self.assertEqual(record["code"], "QUARTZ_851")
        self.assertEqual(record["keep"], "preserved")
''',
}

VISION_HTML = """<!doctype html><title>Visual record</title><main>Read the displayed record image.</main>
<canvas width="720" height="220"></canvas><script>
const c=document.querySelector('canvas').getContext('2d');c.fillStyle='white';c.fillRect(0,0,720,220);
c.fillStyle='#141414';c.font='38px sans-serif';c.fillText('Amount: 173 grams',30,75);
c.fillText('Code: QUARTZ_851',30,150);</script>"""


def has_image_request(directory: Path) -> bool:
    if not (directory / "provider.jsonl").exists():
        return False
    for line in (directory / "provider.jsonl").read_text(encoding="utf-8").splitlines():
        row = parse_json_object(line)
        if row.get("phase") != "request":
            continue
        messages = decode_json_object(row["payload"]).get("messages")
        for message in messages if isinstance(messages, list) else []:
            content = decode_json_object(message).get("content")
            for item in content if isinstance(content, list) else []:
                if isinstance(item, dict) and item.get("type") == "image_url":
                    image = item.get("image_url")
                    if isinstance(image, dict) and isinstance(url := image.get("url"), str) and url.startswith("data:image/"):
                        return True
    return False


def fixed_copy_exchanges(directory: Path) -> list[int]:
    """Inspect actual request argument schemas, not examples in prompt text."""
    exchanges: list[int] = []
    for line in (directory / "provider.jsonl").read_text(encoding="utf-8").splitlines():
        row = parse_json_object(line)
        if row.get("phase") != "request":
            continue
        payload = decode_json_object(row["payload"])
        format_value = payload.get("response_format")
        if not isinstance(format_value, dict):
            continue
        schema = decode_json_object(decode_json_object(format_value["json_schema"])["schema"])
        variants = schema.get("oneOf")
        for variant in variants if isinstance(variants, list) else []:
            clauses = decode_json_object(variant).get("allOf")
            if not isinstance(clauses, list):
                continue
            properties: JsonObject = {key: value for clause in clauses if isinstance(clause, dict)
                          for key, value in decode_json_object(clause.get("properties", {})).items()}
            names = decode_json_object(properties.get("tool", {})).get("enum", [])
            args = decode_json_object(properties.get("args", {}))
            if (names in (["edit"], ["codemode"]) and args.get("required") == ["path"]
                    and set(decode_json_object(args.get("properties", {}))) == {"path"}):
                exchange = row.get("exchange")
                if type(exchange) is not int:
                    raise ValueError("request exchange requires an integer")
                exchanges.append(exchange)
    return exchanges


class Options(argparse.Namespace):
    directory: Path
    base_url: str
    case: str


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:5001/v1")
    parser.add_argument("--case", choices=("composition", "repair", "repair-flexible", "vision"), default="composition")
    args = parser.parse_args(namespace=Options())
    directory = Path(args.directory)
    target = select_target(180, str(args.base_url))
    if args.case == "vision":
        summary = run_case(directory, "chat", target, max_turns=10, declare_required_tools=False,
            fixture_html=VISION_HTML, task_transform=lambda _task:
                f"请打开 {URL}，查看页面上实际显示的记录图片，告诉我净重克数和完整校验码，不要修改项目或运行命令。")
        answer = str(summary.get("answer") or "")
        summary["actual_image_request"] = has_image_request(directory)
        summary["natural_task_closed"] = (summary.get("stop_reason") == "done"
            and summary["actual_image_request"] is True and "173" in answer and "QUARTZ_851" in answer)
        (directory / "natural-verdict.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(summary, ensure_ascii=False))
        return
    summary = run_case(directory, "hybrid", target, max_turns=14, declare_required_tools=False,
        strict_research=True,
        task_transform=lambda _task: (REPAIR_TASK if args.case == "repair" else
                                     FLEXIBLE_REPAIR_TASK if args.case == "repair-flexible" else TASK),
        additional_required_tools=(require_tool_success("run"),),
        requested_capabilities=("web.read", "browser.observe", "browser.interact", "browser.effect", "knowledge.write"),
        project_files=PROJECT)
    summary["fixed_copy_offered_exchanges"] = fixed_copy_exchanges(directory)
    result = summary.get("project_result")
    if isinstance(result, dict):
        summary["preserved_other_fields"] = result.get("keep") == "preserved"
    receipts = tool_receipts(directory / "state")
    reports = [i for i, row in enumerate(receipts) if row.result.call.name == "submit_research_answer" and row.result.ok]
    edits = [i for i, row in enumerate(receipts) if row.result.call.name == "edit" and row.result.ok]
    verified = any(row.result.call.name == "run" and row.result.ok and row.exit_code == 0 for row in receipts)
    summary["report_before_edit"] = bool(reports and edits and reports[0] < edits[0])
    summary["successful_project_check"] = verified
    failed = [i for i, row in enumerate(receipts) if row.result.call.name == "run" and not row.result.ok]
    reads = [i for i, row in enumerate(receipts) if row.result.call.name == "browser_read" and row.result.ok]
    repair = any(failure < read < edit for failure in failed for read in reads for edit in edits)
    summary["repair_triggered_research"] = repair
    summary["case"] = args.case
    summary["probe_configuration"] = {"strict_research": True, "project_verification_config": True,
        "max_turns": 14, "seed": 123456789, "temperature": 0}
    summary["operational_goal_completed"] = (summary.get("stop_reason") == "done" and verified and bool(reports)
        and summary.get("final_file_correct") is True
        and summary.get("preserved_other_fields") is True)
    summary["requested_order_satisfied"] = (True if args.case == "repair-flexible" else repair
        if args.case == "repair" else summary.get("report_before_edit") is True)
    summary["natural_task_closed"] = (summary["operational_goal_completed"] is True
        and summary["requested_order_satisfied"] is True)
    (directory / "natural-verdict.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(parse_json_object(json.dumps(summary)), ensure_ascii=False))


if __name__ == "__main__":
    main()
