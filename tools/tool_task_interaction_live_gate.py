"""Real local model + production headless entry for stdin, ConPTY and browser waits.

Only source HTML and the provider connection are supplied by this probe. Model
actions, execution, authorization, scheduling and receipts remain production.
Every attempt has a new directory; failed attempts are never overwritten.
"""
from __future__ import annotations

import argparse
import http.client
import json
import re
import time
import uuid
from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

from playwright.sync_api import Route

from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.durable import DurableSession, SQLiteDurableStore
from codey.operations.tool_task_delivery import notified_tool_tasks
from codey.operations.tool_task_reads import tool_task_snapshot
from codey.providers.api_request_policy import LocalRequestPolicy
from codey.research.browser_search import BrowserSearchProvider
from codey.types.json_value import (
    JsonObject,
    JsonValue,
    canonical_json_object,
    decode_json_object,
    encode_transport_value,
    parse_json_object,
    require_json_string,
)
from tools.completion_artifact_audit import EffectReceipt, tool_receipts
from tools.completion_explanation_ab import append_json, production_source_digest, write_json
from tools.local_model_gate_attempts import GateTarget, RecordingProvider, isolate_gate_secondary_models

URL = "https://example.com/tool-task-interaction"
HTML = """<!doctype html><meta charset="utf-8"><title>Background wait probe</title>
<button onclick="document.querySelector('article').textContent='BROWSER_READY_941'">Release wait</button>
<article>Waiting for the button.</article>"""
PIPE_TEST = """import sys

def test_input():
    print('PIPE_READY', flush=True)
    assert sys.stdin.readline() == 'one\\n'
    print('FIRST_ACCEPTED', flush=True)
    assert sys.stdin.readline() == 'two\\n'
    assert sys.stdin.read() == ''
    print('PIPE_COMPLETE', flush=True)
"""
TTY_TEST = """import os
import signal
import sys
import time

def test_terminal():
    assert sys.stdin.isatty() and sys.stdout.isatty()
    interrupted = False
    def handle(signum, frame):
        nonlocal interrupted
        interrupted = True
    signal.signal(signal.SIGINT, handle)
    print('TERMINAL_READY', flush=True)
    while not interrupted:
        time.sleep(0.02)
    print('INTERRUPTED', flush=True)
    assert input('terminal> ') == 'done'
    size = os.get_terminal_size()
    assert (size.columns, size.lines) == (91, 37)
    print('TERMINAL_COMPLETE', flush=True)
"""


class SingleAttemptPolicy(LocalRequestPolicy):
    max_attempts = 1


def endpoint(path: str) -> JsonObject:
    connection = http.client.HTTPConnection("127.0.0.1", 5001, timeout=5)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        if response.status != 200:
            raise RuntimeError(f"model preflight returned HTTP {response.status}")
        return parse_json_object(response.read().decode("utf-8"))
    finally:
        connection.close()


def visible_text(value: JsonValue) -> str:
    if isinstance(value, str):
        return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)
    if isinstance(value, dict):
        return "\n".join(visible_text(item) for item in value.values())
    if isinstance(value, list):
        return "\n".join(visible_text(item) for item in value)
    return ""


def create(project: Path, case: str) -> str:
    if case == "browser":
        return (
            "Run this authorized browser lifecycle acceptance. No file edits, shell, run or evaluate. "
            "Use the existing codemode tools. Every tools call returns Result<T>: always unwrap it "
            "before reading data fields. result_ref identifies a receipt, not a background task.\n"
            f"1. Explicitly call browser_navigate to {URL}; return its result. "
            "Automatic open_url prefetch does not replace this required navigation.\n"
            "2. In ONE subsequent codemode invocation: unwrap a browser_snapshot; find the button "
            "named Release wait in its elements; unwrap browser_wait with condition=text, "
            "value=BROWSER_READY_941, timeout_ms=30000, run_in_background=true; "
            "then click the observed button ref BEFORE returning the wait's task_ref. "
            "This ordering proves that the browser can act while the wait runs.\n"
            "3. Use task_output with that exact task_ref and wait_ms=1000. Read its formal "
            "succeeded result. Explicitly call browser_read to read the updated page.\n"
            "4. Start one more background browser_wait for NEVER_APPEARS_941, timeout_ms=30000. "
            "In that same codemode invocation cancel the returned task_ref using task_cancel "
            "with wait_ms=1000, then use task_output if necessary to confirm cancelled.\n"
            "5. Finish only after both waits have terminal results. Report actual outcomes. "
            "Exactly two background waits are needed. If a program fails after admission, "
            "recover the existing task_ref with read_tool_result(view=value); do not start it again."
        )
    name = "test_terminal.py" if case == "tty" else "test_input.py"
    (project / name).write_text(TTY_TEST if case == "tty" else PIPE_TEST, encoding="utf-8")
    (project / "pytest.ini").write_text("[pytest]\ntestpaths = .\n", encoding="utf-8")
    start = (
        f"这是交互进程验收，不要修改任何文件。后台运行 python -m pytest -q -s {name}，"
        "run 参数必须有 run_in_background=true 和 stdin=true。拿 task_ref 后用 task_output 查看真实输出。"
        "交互期间查看提示符使用 wait_ms=100，避免每次等待30秒。"
        "即使系统自动送达exit 0，也必须在输入结束后显式调用 task_output 读完整输出标记；"
        "不能只根据自动送达的退出码声称看到输出标记。"
    )
    if case == "tty":
        return start + (
            "还要给 run 传 tty=true。看到 TERMINAL_READY 后，用 task_resize 调成91列37行，"
            "再用 task_signal(signal=interrupt) 发送 Ctrl+C。看到 INTERRUPTED 和 terminal> 后，"
            "用 task_input 输入 done 后跟一个真正的回车字符 U+000D（JSON 转义为 \\r）；"
            "Windows终端Enter是回车，不能使用字面反斜杠r。不要用 eof=true。"
            "用 task_output 等待退出并读取正式退出码，必须看到 TERMINAL_COMPLETE 和测试通过。"
            "最后再尝试 task_input 输入 late，确认已退出的进程拒绝输入；这一次拒绝是预期结果。"
            "如实报告结果。"
        )
    return start + (
        "不要使用tty。看到 PIPE_READY 后第一次 task_input 输入 one 加真正的换行 U+000A；"
        "看到 FIRST_ACCEPTED 后第二次 task_input 输入 two 加真正换行，并传 eof=true 关闭标准输入。"
        "用 task_output 等待退出并读取正式退出码，必须看到 PIPE_COMPLETE 和测试通过。"
        "最后再尝试 task_input 输入 late，确认已退出的进程拒绝输入；这一次拒绝是预期结果。"
        "如实报告结果。"
    )


@contextmanager
def browser_fixture(case: str, directory: Path) -> Generator[None, None, None]:
    if case != "browser":
        yield
        return
    browser = BrowserSearchProvider(headless=True)

    def route(route: Route) -> None:
        append_json(directory / "browser-requests.jsonl", {"url": route.request.url})
        route.fulfill(status=200 if route.request.url == URL else 404, content_type="text/html",
                      body=HTML if route.request.url == URL else "Fixture URL unavailable")

    def source_factory() -> BrowserSearchProvider:
        return browser

    try:
        with ExitStack() as stack:
            stack.enter_context(patch.object(browser, "_guard_request", route))
            stack.enter_context(patch("codey.research.search_factory.default_research_search_provider", source_factory))
            stack.enter_context(patch("codey.operations.task_phases.dispatch.default_research_search_provider", source_factory))
            yield
    finally:
        browser.close()


def assess(case: str, report: JsonObject, receipts: list[EffectReceipt]) -> list[str]:
    issues: list[str] = []
    if report.get("exit_code") != 0 or report.get("stop_reason") != "done":
        issues.append("main request did not finish normally")
    results = [row.result for row in receipts]
    observed = [decode_json_object(row.canonical["value"]) for row in results
                if row.call.name == "task_output" and row.ok]
    notifications = report.get("background_observations", [])
    if not isinstance(notifications, list):
        raise ValueError("background_observations must be a list")
    observed.extend(decode_json_object(row["snapshot"]) for row in notifications
                    if isinstance(row, dict) and row.get("delivered_automatically") is True)
    succeeded = [row for row in observed if row.get("outcome") == "succeeded"]
    if not succeeded:
        issues.append("model did not read a formally successful background result")
    if report.get("source_stable") is not True:
        issues.append("production source changed during this attempt")
    tasks = report.get("tasks")
    if not isinstance(tasks, list):
        raise ValueError("tasks must be a list")
    children = [decode_json_object(task) for task in tasks if isinstance(task, dict) and task.get("kind") == "tool"]
    if any(task.get("status") not in {"completed", "cancelled"}
           or decode_json_object(task["state"]).get("cleanup_unknown") for task in children):
        issues.append("owned background work did not settle with confirmed cleanup")
    if case == "browser":
        if len(children) != 2:
            issues.append("expected exactly two browser wait tasks")
        if not any(row.call.name == "task_cancel" and row.ok for row in results):
            issues.append("model did not successfully cancel the second wait")
        if not any(task["status"] == "cancelled" for task in children):
            issues.append("second browser wait did not reach cancelled")
        controls = [decode_json_object(row.canonical["value"]) for row in results
                    if row.call.name in {"task_cancel", "task_output"} and row.ok]
        if not any(row.get("status") == "cancelled" for row in controls):
            issues.append("model did not observe confirmed cancellation")
        for name in ("browser_navigate", "browser_snapshot", "browser_click", "browser_read"):
            if not any(row.call.name == name and row.ok for row in results):
                issues.append(f"missing successful {name}")
        accepted_index = next((i for i, row in enumerate(results)
                               if row.call.name == "browser_wait" and row.kind == "accepted"), len(results))
        click_index = next((i for i, row in enumerate(results)
                            if row.call.name == "browser_click" and row.ok), -1)
        if accepted_index >= click_index:
            issues.append("button was not clicked after background admission")
    else:
        if len(children) != 1:
            issues.append("expected exactly one managed process")
        expected = "TERMINAL_COMPLETE" if case == "tty" else "PIPE_COMPLETE"
        visible = "\n".join(visible_text(row) for row in succeeded)
        if expected not in visible:
            issues.append(f"successful result did not contain {expected}")
        if not any(row.call.name == "task_input" and not row.ok and row.call.args.get("data") == "late"
                   for row in results):
            issues.append("model did not confirm that input after exit is rejected")
        required = ("task_resize", "task_signal", "task_input") if case == "tty" else ("task_input",)
        for name in required:
            if not any(row.call.name == name and row.ok for row in results):
                issues.append(f"missing successful {name}")
    return issues


def collect_evidence(directory: Path, report: JsonObject) -> list[EffectReceipt]:
    from codey.storage.managed_outputs import ManagedOutputStore

    durable = DurableSession(SQLiteDurableStore(directory / "state" / "durable.sqlite3"))
    try:
        tasks = durable.tasks()
        managed = ManagedOutputStore(directory / "state")
        notified = {ref for task in tasks if task.execution_kind == "model"
                    for ref in notified_tool_tasks(durable, owner_task_id=task.task_id, store=managed)}
        report["tasks"] = [{"task_id": task.task_id, "kind": task.execution_kind, "status": str(task.status),
                            "state": canonical_json_object(task.state)} for task in tasks]
        report["background_observations"] = [
            {"delivered_automatically": task.task_id in notified,
             "snapshot": canonical_json_object(tool_task_snapshot(durable, task, store=managed, args={"limit": 16000}))}
            for task in tasks if task.execution_kind == "tool"]
    finally:
        durable.close()
    return tool_receipts(directory / "state")


def audit_saved(directory: Path) -> JsonObject:
    """Recheck original durable evidence without invoking the model or replacing its report."""
    report = parse_json_object((directory / "summary.json").read_text(encoding="utf-8"))
    report["original_passed"] = report.get("passed")
    report["original_issues"] = report.get("issues")
    receipts = collect_evidence(directory, report)
    issues = assess(require_json_string(report["case"], "case"), report, receipts)
    original = report["original_issues"]
    if isinstance(original, list) and "model changed immutable fixture files" in original:
        issues.append("model changed immutable fixture files")
    report["issues"] = [issue for issue in issues]
    report["passed"] = not issues
    write_json(directory / "reassessment.json", report)
    summary: JsonObject = {"directory": str(directory), "passed": not issues}
    summary["issues"] = [issue for issue in issues]
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--case", choices=("pipe", "tty", "browser"))
    parser.add_argument("--protocol", choices=("native", "text"), default="native")
    parser.add_argument("--audit-directory", type=Path)
    args = parser.parse_args()
    audit_directory: Path | None = args.audit_directory
    if audit_directory is not None:
        assessment = audit_saved(audit_directory)
        print(json.dumps(assessment), flush=True)
        if assessment["passed"] is not True:
            raise SystemExit(1)
        return
    root: Path | None = args.root
    case: str | None = args.case
    if root is None or case is None:
        parser.error("--root and --case are required unless --audit-directory is used")
    protocol: str = args.protocol
    directory = root / f"{case}-{protocol}-{uuid.uuid4().hex[:8]}"
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "probe-source.py").write_text(Path(__file__).read_text(encoding="utf-8"), encoding="utf-8")
    before = endpoint("/api/extra/perf")
    write_json(directory / "backend-before.json", before)
    if not before.get("idle") or before.get("queue"):
        raise RuntimeError("model backend is busy; no generation was submitted")
    models = endpoint("/v1/models")["data"]
    if not isinstance(models, list) or len(models) != 1:
        raise ValueError("expected one loaded local model")
    model = require_json_string(decode_json_object(models[0])["id"], "model")
    target = GateTarget("http://127.0.0.1:5001/v1", model, 262144, 32768, 32000,
                        protocol=protocol, temperature=0, request_timeout=120)
    project = directory / "project"
    project.mkdir()
    task = create(project, case)
    fixtures = {path: path.read_bytes() for path in project.iterdir()}
    source_before = production_source_digest()
    write_json(directory / "manifest.json", {"case": case, "task": task, "target": encode_transport_value(target),
                                             "source_digest": source_before})
    provider = RecordingProvider(target, directory)
    provider.request_policy = SingleAttemptPolicy()

    def configure(payload: dict[str, object]) -> None:
        payload["seed"] = 123456789
        payload["genkey"] = "codey-interaction-" + uuid.uuid4().hex

    provider.configure_request = configure

    def observe(row: dict[str, object]) -> None:
        append_json(directory / "events.jsonl", row)

    def connect(provider_id: str, *, port: int) -> RecordingProvider:
        if provider_id != "local":
            raise ValueError("probe only authorizes the selected local model")
        return provider

    started = time.monotonic()
    report: JsonObject = {"directory": str(directory), "case": case, "native_tools": provider.native_tools,
                          "text_tool_format": provider.text_tool_format}
    try:
        with isolate_gate_secondary_models(target), browser_fixture(case, directory):
            result = run_headless(HeadlessRequest(project, task, provider_id="local", max_turns=16,
                state_home=directory / "state", project_changes_required=False, review_policy="web_if_available",
                requested_capabilities=("web.read", "browser.observe", "browser.interact", "browser.effect")
                    if case == "browser" else (), allow_web=case == "browser",
                required_tools=("browser_navigate", "browser_snapshot", "browser_wait", "browser_click", "browser_read")
                    if case == "browser" else (),
                model_selection={"model": model, "base_url": target.base_url}),
                emit_jsonl=observe, connect_provider=connect)
        report.update(decode_json_object(encode_transport_value(result)))
    except Exception as exc:
        report.update(exception=type(exc).__name__, detail=str(exc))
    finally:
        provider.close()
    report["source_stable"] = production_source_digest() == source_before
    receipts = collect_evidence(directory, report)
    write_json(directory / "receipts.json", [{"identity": row.identity, "exit_code": row.exit_code,
               "result": encode_transport_value(row.result)} for row in receipts])
    issues = assess(case, report, receipts)
    if any(not path.is_file() or path.read_bytes() != data for path, data in fixtures.items()):
        issues.append("model changed immutable fixture files")
    report["issues"] = [issue for issue in issues]
    report.update(seconds=round(time.monotonic() - started, 3), passed=not issues)
    write_json(directory / "summary.json", report)
    summary: JsonObject = {key: report.get(key) for key in (
        "directory", "passed", "issues", "exit_code", "stop_reason", "exception", "seconds")}
    print(json.dumps(summary), flush=True)
    if issues:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
