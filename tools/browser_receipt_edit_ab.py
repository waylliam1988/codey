"""Compare model-supplied edits with fields bound to a verified browser read."""
from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from pathlib import Path
from unittest.mock import patch

from codey.automation.browser_session import BrowserSession
from codey.codemode.host import CodeModeHost
from codey.operations.task_execution import ExecutionDelegate
from codey.runtime.core.models import ToolCall, ToolResult
from codey.toolchain.constants import STALE_READ_REFERENCE_ERROR
from codey.types.json_value import JsonObject, JsonValue, decode_json_object
from tools import browser_outcome_ab as outcome
from tools import browser_production_live_gate as gate
from tools.completion_artifact_audit import tool_receipts
from tools.completion_explanation_ab import Target, append_json, production_source_digest, provider_metrics, write_json

VARIANTS = {"original": (173, "QUARTZ_851"), "alternate": (247, "BASALT_426")}


@contextmanager
def bind_browser_fields(directory: Path) -> Iterator[None]:
    invoke = CodeModeHost.invoke
    execute = ExecutionDelegate._execute_browser_operation
    active_browser: BrowserSession | None = None

    def observe(delegate: ExecutionDelegate, browser: BrowserSession, call: ToolCall, *,
                turn: int, tool_index: int) -> tuple[ToolResult, bool, int | None]:
        nonlocal active_browser
        active_browser = browser
        return execute(delegate, browser, call, turn=turn, tool_index=tool_index)

    def edit(host: CodeModeHost, name: str, args: dict[str, object]) -> object:
        if name != "edit":
            return invoke(host, name, args)

        def reject(reason: str) -> JsonObject:
            append_json(directory / "bindings.jsonl", {"status": "rejected", "reason": reason})
            return {"ok": False, "error": reason}

        ref = args.get("base_result_ref")
        if (host._replay is not None or set(args) != {"base_result_ref", "changes"} or args["changes"] != {}
                or not isinstance(ref, str) or ref not in host._complete_reads
                or host._complete_reads[ref][0] != "result.json"):
            return reject("Use a complete original result.json read and tools.edit({read, changes: {}}).")
        receipts = tool_receipts(directory / "state")
        committed = {child for row in receipts
                     if row.result.call.name == "codemode" and row.result.ok
                     and isinstance(children := row.result.canonical.get("child_effect_ids"), list)
                     for child in children if isinstance(child, str)}
        evidence = outcome.loaded_record(
            ((row.identity, row.result) for row in receipts
             if row.result.ok and row.result.call.name in outcome.OBSERVED), committed,
        )
        if (evidence is None or active_browser is None
                or active_browser.document_identity() != evidence["document"]):
            return reject("No complete committed record after Load record on the current document. Read the page in a successful CodeMode call first.")
        result = invoke(host, name, {**args, "changes": evidence["changes"]})
        assert isinstance(result, dict)
        error = str(result.get("error", ""))
        status = "applied" if result.get("ok") is True else (
            "version_conflict" if STALE_READ_REFERENCE_ERROR in error else "edit_failed")
        append_json(directory / "bindings.jsonl", {**evidence, "file_read_ref": ref, "status": status})
        return result

    with (patch.object(ExecutionDelegate, "_execute_browser_operation", observe),
          patch.object(CodeModeHost, "invoke", edit)):
        yield


def task_for(task: str, arm: str) -> str:
    assignment = ("编辑由宿主从当前网页的已核验回执填值。changes 必须是空对象，"
                  "不要把任何网页值填入 changes。" if arm == "B" else
                  "根据上一条浏览器工具结果填写 inputs.changes，只包含数字 amount 和字符串 code。"
                  "文件内容只用于提供写入版本。")
    changes = "{}" if arm == "B" else "inputs.changes"
    return (task + "\n先用独立的 CodeMode 调用完成导航、加载和读取，把页面正文返回。"
            "再用以下程序编辑文件，确认成功后才提交 done。\n" + assignment + "\n"
            "const read = await tools.read_file({path: 'result.json'});\n"
            f"return unwrap(await tools.edit({{read, changes: {changes}}}));\n"
            "如果文件版本冲突，重新运行这两行，保留文件的其他字段。")


def run_arm(directory: Path, variant: str, arm: str, target: Target, *, max_turns: int) -> JsonObject:
    amount, code = VARIANTS[variant]
    before = production_source_digest()
    html = outcome.DELAYED_HTML.replace("173", str(amount)).replace("QUARTZ_851", code)
    with (outcome.readiness_candidate(directory, enabled=True),
          bind_browser_fields(directory) if arm == "B" else nullcontext(),
          patch("codey.app.provider_services.provider_tab_availability", return_value={}),
          patch("codey.app.review_service.run_review", return_value=None)):
        row = decode_json_object(gate.run_case(directory, "hybrid", target, max_turns=max_turns,
            stale_edit=True, preserve_external_field=True, explicit_wait=False, fixture_html=html,
            expected_amount=amount, expected_code=code, task_transform=lambda task: task_for(task, arm)))
    path = directory / "bindings.jsonl"
    bindings = [decode_json_object(json.loads(line))
                for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []
    attempts = [r for r in bindings if r["status"] != "rejected"]
    edits = [row.result for row in tool_receipts(directory / "state") if row.result.call.name == "edit"] \
        if (directory / "state" / "durable.sqlite3").exists() else []
    bound = (len(attempts) >= 2 and attempts[0]["status"] == "version_conflict"
             and attempts[1]["status"] == "applied" and attempts[0]["file_read_ref"] != attempts[1]["file_read_ref"]
             and len(edits) == len(attempts)
             and all(r["changes"] == {"amount": amount, "code": code}
                     and r["file_read_ref"] == edit.call.args.get("base_result_ref")
                     for r, edit in zip(attempts, edits, strict=True)))
    stable = before == production_source_digest()
    row.update(variant=variant, arm=arm, source_stable=stable, bindings=list(bindings), binding_verified=bound,
               passed=row["passed"] is True and stable and (arm == "A" or bound),
               **provider_metrics(directory / "provider.jsonl"))
    write_json(directory / "summary.json", row)
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--max-turns", default=10, type=int)
    parser.add_argument("--timeout", default=90.0, type=float)
    parser.add_argument("--base-url", default="http://127.0.0.1:5001/v1")
    args = parser.parse_args()
    target = gate.select_target(args.timeout, args.base_url)
    root = args.run_dir.resolve()
    root.mkdir(parents=True)
    source = production_source_digest()
    scripts = [Path(__file__), Path(gate.__file__), Path(outcome.__file__)]
    hashes: list[JsonValue] = [hashlib.sha256(path.read_bytes()).hexdigest() for path in scripts]
    manifest: JsonObject = {"source": source, "scripts": hashes, "model": target.model,
        "seed": target.seed, "temperature": 0, "max_turns": args.max_turns, "timeout": args.timeout}
    write_json(root / "manifest.json", manifest)
    rows: list[JsonObject] = []
    for variant, order in (("original", "AB"), ("alternate", "BA")):
        for arm in order:
            print(f"Starting {variant}/{arm}", flush=True)
            row = run_arm(root / f"{variant}-{arm}", variant, arm, target, max_turns=args.max_turns)
            rows.append(row)
            print(f"{variant}/{arm}: passed={row['passed']} stop={row.get('stop_reason')} calls={row.get('model_calls')}", flush=True)
            write_json(root / "progress.json", {"runs": rows})
    stable = source == production_source_digest() and all(row["source_stable"] is True for row in rows)
    scripts_stable = hashes == [hashlib.sha256(path.read_bytes()).hexdigest() for path in scripts]
    write_json(root / "summary.json", {**manifest, "source_stable": stable, "scripts_stable": scripts_stable, "runs": rows})
    return 0 if stable and scripts_stable and all(row["passed"] is True for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
