"""Paired local-model format trials through production Chat; no new agent loop."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.env_names import NATIVE_TOOLS_ENV
from codey.operations import kernel_prompt
from codey.operations.task_session import TaskSession
from codey.research.chat_web import ChatWebProvider
from codey.toolchain.tool_spec import ToolSpec
from codey.types.json_value import JsonObject, decode_json_object, decode_json_value
from codey.workspace.coding_context import CodingContext
from tools import local_model_gate_attempts as attempts
from tools.shell_web_diagnostic_gate import select_target

OLD_FORMAT = "Preserve the user's requested final format, including JSON if requested. "
NEW_FORMAT = ("Follow the user's requested final format. Otherwise, answer naturally in the user's language, "
              "using Markdown when helpful. ")
OLD_NATIVE = ("For the final answer, use done with a summary string if declared. summary contains the complete response "
              "in the user's requested final format. Requested JSON is a bare document without Markdown fences "
              "inside summary, not the outer tool-call object.")
SOURCE = "https://example.com/food-guide"
CASES = {
    "natural": "简单介绍福州鱼丸、肉燕和锅边糊分别是什么。",
    "json": "计算17乘19，只返回JSON对象，字段product为整数。",
    "table": "用Markdown表格比较鱼丸和肉燕，表头为美食、主要原料，只列两行数据。",
    "code": "只返回一段Python代码，定义greet(name: str) -> str：返回两行文本，第一行为你好加name，第二行为欢迎。不要执行或创建文件。",
    "web": "你帮我上网看看福州有什么好吃的",
}


@contextmanager
def baseline_format(enabled: bool) -> Generator[None, None, None]:
    """Restore only the two pre-change prompt fragments inside this worker."""
    original = kernel_prompt.kernel_prompt_for_session
    native_rule = kernel_prompt._native_final_rule

    def final_rule(*, structured_report: bool) -> str:
        return native_rule(structured_report=True) if structured_report else OLD_NATIVE

    def render(session: TaskSession, *, tool_names: tuple[str, ...], user_task: str = "",
               contract_text: str = "", context_text: str = "", native: bool = False,
               task_guidance: str = "", coding_context: CodingContext | None = None,
               request_scoped_contract: bool = False, done_spec: ToolSpec | None = None,
               whole_submission: bool = False) -> str:
        return original(session, tool_names=tool_names, user_task=user_task, contract_text=contract_text,
            context_text=context_text, native=native, task_guidance=task_guidance, coding_context=coding_context,
            request_scoped_contract=request_scoped_contract, done_spec=done_spec,
            whole_submission=whole_submission).replace(NEW_FORMAT, OLD_FORMAT)

    with ExitStack() as stack:
        if enabled:
            stack.enter_context(patch.object(kernel_prompt, "_native_final_rule", final_rule))
            stack.enter_context(patch.object(kernel_prompt, "kernel_prompt_for_session", render))
        yield


def format_ok(case: str, summary: str) -> bool:
    if case == "json":
        try:
            return decode_json_value(json.loads(summary)) == {"product": 323}
        except ValueError:
            return False
    if case == "code":
        code = summary.strip()
        if code.startswith("```python\n") and code.endswith("\n```"):
            code = code[10:-4]
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return False
        return (len(tree.body) == 1 and isinstance(tree.body[0], ast.FunctionDef)
                and tree.body[0].name == "greet" and any(isinstance(node, ast.Constant)
                    and isinstance(node.value, str) and "\n" in node.value for node in ast.walk(tree)))
    if case == "table":
        rows = [line for line in summary.splitlines() if line.strip().startswith("|")]
        return (len(rows) == 4 and "美食" in rows[0] and "主要原料" in rows[0]
                and all(any(food in row for row in rows[2:]) for food in ("鱼丸", "肉燕")))
    return (not summary.lstrip().startswith(("{", "[", "```json"))
            and all(word in summary for word in ("鱼丸", "肉燕", "锅边糊")))


@contextmanager
def fixed_web() -> Generator[None, None, None]:
    def search(provider: ChatWebProvider, query: str, *, limit: int = 8) -> list[JsonObject]:
        results: list[JsonObject] = [{"title": "福州小吃记录", "url": SOURCE, "snippet": "三种传统小吃的食材与做法。"}]
        return results[:limit]

    def fetch(url: str) -> dict[str, object]:
        if url != SOURCE:
            return {"status": "error", "url": url, "error": "No such fixture source"}
        return {"status": "ok", "url": url, "title": "福州小吃记录", "content_kind": "html",
                "text": "鱼丸用鱼肉制成丸皮，包入肉馅。肉燕以肉制燕皮包馅。锅边糊用米浆沿锅边烫熟，适合作早餐。"}

    with patch.object(ChatWebProvider, "search", search), patch("codey.research.chat_web.fetch_http_document", fetch):
        yield


def trial(directory: Path, case: str, baseline: bool) -> JsonObject:
    directory.mkdir(parents=True, exist_ok=False)
    target = select_target("gemma", 180, "http://127.0.0.1:5001/v1")
    events: list[JsonObject] = []

    def observe(row: dict[str, object]) -> None:
        events.append(decode_json_object(row))
        with (directory / "events.jsonl").open("a", encoding="utf8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")

    def connect(provider_id: str, **kwargs: object) -> attempts.RecordingProvider:
        if provider_id != "local":
            raise ValueError("This gate only uses the pinned local model")
        return attempts.RecordingProvider(target, directory)

    manifest: JsonObject = {"case": case, "baseline": baseline, "task": CASES[case], "model": target.model,
        "temperature": target.temperature, "source_mode": "controlled_if_called",
        "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "prompt_source_sha256": hashlib.sha256(Path(kernel_prompt.__file__).read_bytes()).hexdigest()}
    (directory / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf8")
    started = time.monotonic()
    with (patch.dict(os.environ, {NATIVE_TOOLS_ENV: "1"}), baseline_format(baseline), fixed_web(),
          attempts.isolate_gate_secondary_models(target), attempts.record_api_generations(directory / "provider.jsonl")):
        run_headless(HeadlessRequest(None, CASES[case], intent="chat", provider_id="local", max_turns=8,
            state_home=directory / "state", model_selection={"model": target.model, "base_url": target.base_url},
            project_changes_required=False, output_format="json" if case == "json" else "text"),
            emit_jsonl=observe, connect_provider=connect)
    terminal = next((row for row in reversed(events) if row.get("type") == "task_done"), {})
    summary = str(terminal.get("summary", ""))
    tools = [row for row in events if row.get("type") == "tool"]
    report: JsonObject = {**manifest, "summary": summary, "stop_reason": terminal.get("stop_reason"),
        "format_ok": format_ok(case, summary), "turns": terminal.get("turns"),
        "tools": [str(row.get("tool_name")) for row in tools],
        "tool_errors": [row for row in tools if row.get("ok") is False],
        "seconds": round(time.monotonic() - started, 3), "manual_review_required": True}
    report["ok"] = (report["format_ok"] is True and report["stop_reason"] == "done"
                     and (case != "web" or SOURCE in summary))
    (directory / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")
    return report


class Arguments(argparse.Namespace):
    run_dir: Path
    cases: list[str]
    repeats: int
    worker: bool
    baseline: bool


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--cases", nargs="+", choices=tuple(CASES), default=list(CASES))
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--baseline", action="store_true")
    args = parser.parse_args(namespace=Arguments())
    if args.worker:
        result = trial(args.run_dir, args.cases[0], args.baseline)
        print(json.dumps(result, ensure_ascii=True), flush=True)
        return 0
    args.run_dir.mkdir(parents=True, exist_ok=False)
    (args.run_dir / "plan.json").write_text(json.dumps({"cases": args.cases, "repeats": args.repeats,
        "variants": ["baseline", "production"], "limits": "Real local model, controlled sources; format checks do not certify facts."},
        indent=2), encoding="utf8")
    reports: list[JsonObject] = []
    for repeat in range(args.repeats):
        for case in args.cases:
            for baseline in ([True, False] if repeat % 2 == 0 else [False, True]):
                directory = args.run_dir / f"r{repeat + 1}-{case}-{'baseline' if baseline else 'production'}"
                command = [sys.executable, str(Path(__file__).resolve()), "--worker", "--run-dir", str(directory), "--cases", case]
                if baseline:
                    command.append("--baseline")
                completed = subprocess.run(command, capture_output=True, text=True, encoding="utf8", timeout=600)
                if completed.returncode:
                    (args.run_dir / "worker-error.txt").write_text(completed.stdout + completed.stderr, encoding="utf8")
                    raise RuntimeError(f"Worker failed: {directory}")
                report = decode_json_object(json.loads((directory / "summary.json").read_text(encoding="utf8")))
                reports.append(report)
                print(json.dumps({key: report[key] for key in ("case", "baseline", "stop_reason", "format_ok", "ok", "seconds")}), flush=True)
                (args.run_dir / "summary.json").write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf8")
    return 0 if all(report["ok"] is True for report in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
