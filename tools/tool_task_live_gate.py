"""Exercise background commands through the real headless entry and local model.

Each invocation creates fresh fixtures and preserves requests, replies, events,
and durable receipts. No model actions or tool results are scripted.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from urllib.request import urlopen

from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.durable import DurableSession, SQLiteDurableStore
from codey.operations.kernel_receipts import restore_result_receipt
from codey.providers.api_request_policy import LocalRequestPolicy
from codey.runtime.effects.effect_records import RuntimeEffectIntent, RuntimeEffectProjection, RuntimeEffectSettlement
from codey.storage.managed_outputs import ManagedOutputStore
from codey.types.json_value import (
    JsonObject,
    canonical_json_object,
    decode_json_object,
    parse_json_object,
    require_json_string,
)
from tools.local_model_gate_attempts import GateTarget, RecordingProvider, isolate_gate_secondary_models
from tools.tool_task_integration_fixture import create as create_integration


class SingleAttemptPolicy(LocalRequestPolicy):
    max_attempts = 1


def write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def assess(report: JsonObject, case: str) -> list[str]:
    """Check actual receipts and task state, independently of the model's answer."""
    def rows(key: str) -> list[JsonObject]:
        value = report[key]
        if not isinstance(value, list):
            raise ValueError(f"{key} must be a list")
        return [decode_json_object(row) for row in value]

    issues: list[str] = []
    if report.get("exit_code") != 0 or report.get("stop_reason") != "done":
        issues.append("main request did not finish normally")
    tasks, calls = rows("tasks"), rows("calls")
    children = [row for row in tasks if row["kind"] == "tool"]
    accepted = [row for row in calls if row["kind"] == "accepted"]
    if case != "integration" and (len(children) != 1 or len(accepted) != 1):
        issues.append("expected one admitted background command and one child")
    outputs = [parse_json_object(require_json_string(row["model_text"], "model_text"))
               for row in calls if row["call_name"] == "task_output" and row["ok"] is True]
    if not outputs:
        issues.append("model did not successfully observe the background task")
    if any(decode_json_object(row["state"]).get("cleanup_unknown") for row in children):
        issues.append("background process cleanup is unconfirmed")
    if case == "integration":
        if len(children) < 3 or len(accepted) < 3:
            issues.append("expected reproduction, fixed regression and cancellable diagnostic in background")
        regressions = [row for row in calls if row["call_name"] == "run" and row["kind"] == "completed"
                       and "test_checkout.py" in str(decode_json_object(row["args"]).get("command"))]
        if not regressions or regressions[0]["ok"] is not False or regressions[-1]["ok"] is not True:
            issues.append("regression did not progress from an observed failure to success")
        if not any(row["call_name"] == "edit" and row["ok"] is True for row in calls):
            issues.append("model did not successfully repair the service")
        if not any(row["call_name"] == "task_cancel" and row["ok"] is True for row in calls):
            issues.append("model did not cancel the partner diagnostic")
        if not any(row["status"] == "cancelled" for row in children):
            issues.append("partner diagnostic was not stopped")
        if not any(row["call_name"] == "run" and row["kind"] == "completed" and row["ok"] is True
                   and "test_health.py" in str(decode_json_object(row["args"]).get("command")) for row in calls):
            issues.append("main task did not verify health after the diagnostic")
        observed_results = [decode_json_object(row["result"]) for row in outputs if row.get("result") is not None]
        if not any(row["ok"] is False and row["exit_code"] == 1 for row in observed_results):
            issues.append("model did not read the initial failing execution")
        if not any(row["ok"] is True and row["exit_code"] == 0 for row in observed_results):
            issues.append("model did not read the fixed successful execution")
        first_edit = next((index for index, row in enumerate(calls)
                           if row["call_name"] == "edit" and row["ok"] is True), len(calls))
        prior_observations = [parse_json_object(require_json_string(row["model_text"], "model_text"))
                              for row in calls[:first_edit] if row["call_name"] == "task_output" and row["ok"] is True]
        if not any(decode_json_object(row["result"]).get("exit_code") == 1
                   for row in prior_observations if row.get("result") is not None):
            issues.append("model edited before reading the initial formal failure")
        if not any(row.get("status") == "running" and "partner-ready" in json.dumps(row) for row in outputs):
            issues.append("model did not read live partner output")
    elif case == "cancel":
        if not children or any(row["status"] != "cancelled" for row in children):
            issues.append("slow command was not cancelled")
        if not any(row["call_name"] == "task_cancel" and row["ok"] is True for row in calls):
            issues.append("model did not request cancellation")
        if not any(row.get("status") == "running" and "integration-ready" in json.dumps(row) for row in outputs):
            issues.append("model did not read live output before cancelling")
        if not any(row["call_name"] == "run" and row["kind"] == "completed" and row["ok"] is True
                   and "test_fast.py" in str(decode_json_object(row["args"]).get("command")) for row in calls):
            issues.append("fast test did not pass after cancellation")
    else:
        code = 1 if case == "failure" else 0
        if not children or any(row["status"] != "completed" for row in children):
            issues.append("background command did not finish")
        if not any(isinstance(result := row.get("result"), dict) and result.get("exit_code") == code
                   and result.get("ok") is (case == "success") for row in outputs):
            issues.append(f"model did not observe the expected formal exit code {code}")
        read_paths = {str(decode_json_object(row["args"]).get("path")) for row in calls
                      if row["call_name"] == "read_file" and row["ok"] is True}
        if not {"README.md", "pricing.py"}.issubset(read_paths):
            issues.append("model did not read both project files")
    return issues


def create_simple(project: Path, case: str) -> str:
    (project / "pricing.py").write_text(
        "def total(price, quantity):\n    return round(price * quantity, 2)\n", encoding="utf-8")
    (project / "README.md").write_text(
        "# Checkout pricing\nThe public entry is pricing.total(price, quantity).\n"
        "test_fast.py checks a two-item order; test_slow.py represents a slow external integration.\n", encoding="utf-8")
    (project / "test_fast.py").write_text(
        "from pricing import total\ndef test_total():\n    assert total(12.50, 2) == 25.0\n", encoding="utf-8")
    expected = "26.0" if case == "failure" else "25.0"
    duration = 90 if case == "cancel" else 8
    (project / "test_slow.py").write_text(
        "import time\nfrom pricing import total\ndef test_slow():\n"
        "    print('integration-ready', flush=True)\n"
        f"    time.sleep({duration})\n    assert total(12.50, 2) == {expected}\n", encoding="utf-8")
    if case == "cancel":
        task = ("请检查这个小型结算项目。先后台运行 python -m pytest -q -s test_slow.py，"
                "查看到 integration-ready 输出后停止这条慢测试，不要停止整个任务。"
                "然后运行 python -m pytest -q test_fast.py 并确认真实结果。"
                "最后说明哪条被停止、哪条通过。不要修改任何文件。")
    else:
        task = ("请帮我做这个小型结算项目的上线检查。先后台运行 python -m pytest -q -s，"
                "测试运行期间阅读 README.md 和 pricing.py，说明测试检查什么。"
                "然后查看后台测试的正式结果，最后如实说明通过还是失败、失败原因。"
                "不要修改任何文件，也不要把已启动说成测试通过。")
    return task


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--case", choices=("success", "failure", "cancel", "integration"), required=True)
    parser.add_argument("--protocol", choices=("native", "text"), default="native")
    args = parser.parse_args()
    root: Path = args.root
    case: str = args.case
    protocol: str = args.protocol
    directory = root / f"{case}-{protocol}-{uuid.uuid4().hex[:8]}"
    directory.mkdir(parents=True, exist_ok=False)
    with urlopen("http://127.0.0.1:5001/api/extra/perf", timeout=5) as response:
        before = decode_json_object(json.load(response))
    write(directory / "backend-before.json", before)
    if not before.get("idle") or before.get("queue"):
        raise RuntimeError("KoboldCpp is busy; no generation was submitted")
    with urlopen("http://127.0.0.1:5001/v1/models", timeout=5) as response:
        inventory = decode_json_object(json.load(response))
    models = inventory["data"]
    if not isinstance(models, list) or not models:
        raise ValueError("local model inventory is empty")
    model = require_json_string(decode_json_object(models[0])["id"], "model")
    target = GateTarget("http://127.0.0.1:5001/v1", model, 262144, 32768, 32000,
                        protocol=protocol, temperature=0, request_timeout=120)
    project = directory / "project"
    project.mkdir()
    task = create_integration(project) if case == "integration" else create_simple(project, case)
    task += ("\n后台启动时必须给 run 传 run_in_background=true，拿到 task_ref 后用 task_output 查看输出和正式结果，"
             "停止用 task_cancel。不要把前台长命令当成后台命令。")
    fixtures = {path: path.read_bytes() for path in project.iterdir() if path.is_file()}
    write(directory / "manifest.json", {"case": case, "task": task, "target": asdict(target),
                                        "expected": "cancel_then_fast_pass" if case == "cancel" else case})
    provider = RecordingProvider(target, directory)
    provider.request_policy = SingleAttemptPolicy()

    def configure(payload: dict[str, object]) -> None:
        payload["seed"] = 123456789
        payload["genkey"] = "codey-tool-task-" + uuid.uuid4().hex

    provider.configure_request = configure

    def observe(row: dict[str, object]) -> None:
        with (directory / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")

    started = time.monotonic()
    report: dict[str, object] = {"directory": str(directory)}
    try:
        with isolate_gate_secondary_models(target):
            result = run_headless(HeadlessRequest(project, task, provider_id="local", max_turns=36 if case == "integration" else 10,
                state_home=directory / "state", project_changes_required=case == "integration", review_policy="web_if_available",
                model_selection={"model": model, "base_url": target.base_url}),
                emit_jsonl=observe, connect_provider=lambda _provider, *, port: provider)
        report.update(asdict(result))
    except Exception as exc:
        report.update(exception=type(exc).__name__, detail=str(exc))
    finally:
        provider.close()
    durable = DurableSession(SQLiteDurableStore(directory / "state" / "durable.sqlite3"))
    try:
        tasks = durable.tasks()
        receipts: list[dict[str, object]] = []
        calls: list[dict[str, object]] = []
        managed = ManagedOutputStore(directory / "state")
        for task_checkpoint in tasks:
            for effect in durable.effects(task_checkpoint.task_id):
                row: dict[str, object] = {"task_id": effect.task_id, "effect_id": effect.effect_id, "status": effect.status,
                       "intent": canonical_json_object(effect.intent),
                       "settlement": canonical_json_object(effect.settlement) if effect.settlement is not None else None}
                receipts.append(row)
                if effect.category == "tool_call" and effect.settlement is not None and effect.settlement.get("result_payload_sha256"):
                    projection = RuntimeEffectProjection(RuntimeEffectIntent.from_payload(dict(canonical_json_object(effect.intent))),
                        RuntimeEffectSettlement.from_payload(dict(canonical_json_object(effect.settlement))))
                    result_receipt = restore_result_receipt(projection, store=managed,
                        session_id=task_checkpoint.conversation_id, run_id=task_checkpoint.task_id)
                    calls.append({"call_name": result_receipt.call.name, "args": result_receipt.call.args,
                                  "kind": result_receipt.kind, "ok": result_receipt.ok, "model_text": result_receipt.model_text})
        write(directory / "receipts.json", receipts)
        report["tasks"] = [{"task_id": task.task_id, "kind": task.execution_kind, "status": str(task.status),
                            "state": canonical_json_object(task.state)} for task in tasks]
        report["calls"] = calls
    finally:
        durable.close()
    report["seconds"] = round(time.monotonic() - started, 3)
    issues = assess(canonical_json_object(report), case)
    immutable = {path: contents for path, contents in fixtures.items() if case != "integration" or path.name != "checkout.py"}
    if any(not path.is_file() or path.read_bytes() != contents for path, contents in immutable.items()):
        issues.append("model changed fixture files despite the task instruction")
    if case == "integration":
        checked = subprocess.run([sys.executable, "-m", "pytest", "-q", "test_checkout.py", "test_health.py"],
            cwd=project, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, check=False)
        write(directory / "independent-check.json", {"exit_code": checked.returncode, "stdout": checked.stdout, "stderr": checked.stderr})
        if checked.returncode != 0:
            issues.append("independent checkout and health regressions failed")
    report["issues"] = issues
    report["passed"] = not issues
    write(directory / "summary.json", report)
    print(json.dumps({key: report.get(key) for key in ("directory", "passed", "issues", "exit_code", "stop_reason", "exception", "seconds")}), flush=True)
    if issues:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
