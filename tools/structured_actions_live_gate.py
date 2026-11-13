"""Acceptance of explicitly enabled action inputs through production + Chromium."""
from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path

from codey.policies.tool_requirement import (
    parse_tool_success_check,
    require_browser_read_field,
    require_structured_actions,
    require_tool_success,
)
from codey.types.json_value import JsonObject, decode_json_object
from tools.browser_production_live_gate import URL, run_case, select_target
from tools.completion_explanation_ab import BINDINGS, SCHEMA, Target, production_source_digest

NO_URL = r"\A(?![\s\S]*[Hh][Tt][Tt][Pp][Ss]?://)[\s\S]*\Z"
WAIT_ARGS: JsonObject = {"condition": "text", "value": "鱼丸", "timeout_ms": 5000}


def bindings() -> tuple[str, ...]:
    rules: list[str] = []
    for check in BINDINGS:
        rule = parse_tool_success_check(check)
        assert rule is not None
        rules.append(replace(rule, omit_from_explanation=rule.answer_pointer == "/token").declaration)
    return tuple(rules)


def schema() -> JsonObject:
    value = decode_json_object(SCHEMA)
    properties = decode_json_object(value["properties"])
    properties["explanation"] = {"type": "string", "minLength": 1, "pattern": NO_URL}
    value["properties"] = properties
    return value


def checks() -> tuple[str, ...]:
    return (require_structured_actions(),
        require_tool_success("browser_wait", via="codemode", arguments=WAIT_ARGS),
        require_browser_read_field(url=URL, prefix="净重为 ", suffix=" 克", answer_pointer="/explanation", answer_contains=True),
        require_browser_read_field(url=URL, prefix="记录校验码 ", suffix="。", answer_pointer="/explanation", answer_contains=True))


def task(mode: str) -> str:
    return (f"用户授权：打开 <{URL}>。通过 codemode 的 action 接口完成操作，不提交 JavaScript 程序。"
        "先 navigate(target=task)，观察 snapshot；用 fill(target={role:textbox,name:Locality},value=福州)，"
        "再 click(target={role:button,name:Load record})，"
        f"wait(condition={WAIT_ARGS['condition']},value={WAIT_ARGS['value']},timeout_ms={WAIT_ARGS['timeout_ms']})，"
        "然后 read 当前页。用 eval(code=window.__contractToken) 取得 token，用 tabs 取得当前页面 URL。"
        + ("将实测净重和校验码通过 edit_json(path=result.json,changes={amount:实测数字,code:实测校验码}) 写入项目。"
           "版本冲突时重交相同 edit_json 动作，它会重新读取并保留其他字段。" if mode == "hybrid" else "")
        + "最后 done.summary 只填自然语言观察过程和实测净重、校验码，两段真实换行。"
        "说明不含 URL 或 token，它们由应用从回执填入独立 JSON 字段。不要运行测试或命令。")


def run(directory: Path, mode: str, target: Target) -> dict[str, object]:
    return run_case(directory, mode, target, explanation_output=True, restore_snapshots=True,
        stale_edit=mode == "hybrid", preserve_external_field=mode == "hybrid", max_turns=18,
        additional_required_tools=checks(), explanation_bindings=bindings(), explanation_schema=schema(),
        task_transform=lambda _: task(mode))


class Options(argparse.Namespace):
    run_dir: Path
    mode: str
    timeout: float
    base_url: str


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--mode", choices=("chat", "hybrid", "all"), default="all")
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--base-url", default="http://127.0.0.1:5001/v1")
    args = parser.parse_args(namespace=Options())
    args.run_dir.mkdir(parents=True, exist_ok=False)
    target = select_target(args.timeout, args.base_url)
    before = production_source_digest()
    rows: list[dict[str, object]] = []
    for mode in (("chat", "hybrid") if args.mode == "all" else (args.mode,)):
        starting: JsonObject = {"starting": mode}
        print(json.dumps(starting), flush=True)
        row = run(args.run_dir / mode, mode, target)
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    summary = {"results": rows, "passed": sum(row.get("passed") is True for row in rows),
               "runs": len(rows), "source_before": before, "source_after": production_source_digest()}
    (args.run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if summary["passed"] == summary["runs"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
