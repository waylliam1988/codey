"""Exercise one experimental receipt-binding API across three browser data shapes."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from playwright.sync_api import Route

from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.automation.browser_session import BrowserSession
from codey.codemode.host import CodeModeHost
from codey.env_names import NATIVE_TOOLS_ENV
from codey.operations import task_execution
from codey.operations.task_execution import ExecutionDelegate
from codey.policies.browser_requirement import browser_receipt_tools
from codey.policies.tool_requirement import (
    BrowserTextSelector,
    _browser_body,
    decode_answer,
    json_pointer,
    require_tool_success,
)
from codey.research.browser_search import BrowserSearchProvider
from codey.runtime.core.models import ToolCall, ToolResult
from codey.types.json_value import JsonObject, JsonValue, decode_json_object, require_json_string
from tools.browser_production_live_gate import select_target
from tools.completion_artifact_audit import rejection_audit, tool_receipts
from tools.completion_explanation_ab import (
    ExperimentProvider,
    Target,
    append_json,
    production_source_digest,
    provider_metrics,
    write_json,
)
from tools.receipt_bound_edit_recovery_probe import EXTERNAL_FIELD, receipt_audit

# Fixed, read-only projections. Model-generated eval results cannot be substituted for these.
TABLE = ('Array.from(document.querySelectorAll("table"),t=>({id:t.id,'
         'headers:Array.from(t.tHead?.rows[0]?.cells||[],c=>c.innerText.trim()),'
         'rows:Array.from(t.tBodies).flatMap(b=>Array.from(b.rows,r=>Array.from(r.cells,c=>c.innerText.trim())))'
         '}))')
STRUCTURED = """Array.from(document.querySelectorAll('script[type="application/json"]'),s=>({id:s.id,text:s.textContent}))"""
PROJECTIONS = {"receipt:table": TABLE, "receipt:json": STRUCTURED}


@dataclass(frozen=True)
class Case:
    url: str
    path: str
    html: str
    task: str
    expected: JsonObject
    acquisition: str


CASES = {
    "text": Case("https://example.com/lab-report", "sample.json",
        "<article>Batch Q7: Net mass = 284 g; checked.</article>",
        "把 Q7 样本报告里的净重以整数克写到 sample.json 的 mass_g。",
        {"mass_g": 284}, "tools.browser_read({})"),
    "table": Case("https://example.com/invoices", "invoice.json",
        "<table id='orders'><thead><tr><th>Order</th><th>Subtotal</th><th>Due</th></tr></thead>"
        "<tbody><tr><td>A-101</td><td>1900</td><td>2100</td></tr>"
        "<tr><td>A-102</td><td>4400</td><td>4875</td></tr></tbody></table>",
        "把订单 A-102 的 Due（整数分）写到 invoice.json 的 due_cents。页面同时有别的订单和 Subtotal。",
        {"due_cents": 4875}, 'tools.browser_eval({code:"receipt:table"})'),
    "json": Case("https://example.com/shipment", "shipment.json",
        '<script id="shipment" type="application/json">'
        '{"shipping":{"trackingNumber":"0042091"},"billing":{"reference":"7834"}}</script>'
        '<article>Shipment ready. The shipment record contains separate shipping and billing details. '
        'Use the shipping tracking number for carrier tracking and keep its original formatting.</article>',
        "把 shipment.json 的 tracking 写成发货追踪号字符串，保留开头的零。数据在页面 JSON 的 shipping 下。",
        {"tracking": "0042091"}, 'tools.browser_eval({code:"receipt:json"})'),
}


def unique(rows: list[JsonValue], key: str, value: JsonValue) -> JsonObject:
    matches = [row for row in rows if isinstance(row, dict) and row.get(key) == value]
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one {key}={value!r}, found {len(matches)}")
    return matches[0]


class TableSelectionError(ValueError):
    def __init__(self, message: str, candidates: JsonObject) -> None:
        super().__init__(message)
        self.candidates = candidates


def table_value(table: JsonObject, selector: JsonObject) -> JsonValue:
    headers, rows = table["headers"], table["rows"]
    if not isinstance(headers, list) or not isinstance(rows, list):
        raise ValueError("Invalid table projection")
    key_column, column = selector["key_column"], selector["column"]
    if headers.count(key_column) != 1 or headers.count(column) != 1:
        raise TableSelectionError("Table column missing or ambiguous", {"columns": headers})
    key_index, index = headers.index(key_column), headers.index(column)
    if any(not isinstance(row, list) or len(row) != len(headers) for row in rows):
        raise ValueError("Unsupported table row shape")
    matches = [row for row in rows if isinstance(row, list) and row[key_index] == selector["key"]]
    if len(matches) != 1:
        raise TableSelectionError(f"Expected exactly one table row, found {len(matches)}",
                                  {"key_column": key_column, "keys": [row[key_index] for row in rows[:8]
                                                                     if isinstance(row, list)]})
    return matches[0][index]


def select_value(receipt: ToolResult, selector: JsonObject, url: str, *, table_projection: str = TABLE) -> JsonValue:
    code = receipt.call.args.get("code")
    if receipt.call.name == "browser_read":
        body = _browser_body(receipt, url=url)
        if body is None:
            raise ValueError("Browser text is incomplete")
        prefix = require_json_string(selector["prefix"], "prefix")
        suffix = require_json_string(selector["suffix"], "suffix")
        value: JsonValue = BrowserTextSelector(url, prefix, suffix).select(body)
    elif receipt.call.name == "browser_eval" and code in (table_projection, STRUCTURED):
        output = decode_json_object(receipt.canonical["value"])
        data = output["value"]
        if output["truncated"] is not False or not isinstance(data, list):
            raise ValueError("Expected the complete fixed browser projection")
        item = unique(data, "id", selector["id"])
        if code == table_projection:
            value = table_value(item, selector)
        else:
            pointer = require_json_string(selector["pointer"], "pointer")
            if pointer and not pointer.startswith("/"):
                raise ValueError("Use JSON Pointer /a/b syntax")
            value = json_pointer(decode_answer(require_json_string(item["text"], "JSON source")), pointer)
    else:
        raise ValueError("Expected a browser read or a fixed browser projection")

    if selector["type"] == "integer":
        if type(value) is int:
            return value
        if isinstance(value, str) and re.fullmatch(r"-?[0-9]{1,15}", value):
            return int(value)
    elif selector["type"] == "string" and isinstance(value, str):
        return value
    raise ValueError("Selected value does not match its declared type")


@contextmanager
def field_binding(directory: Path, *, table_projection: str = TABLE, expire_url: str | None = None) -> Iterator[None]:
    invoke = CodeModeHost.invoke
    execute = ExecutionDelegate._execute_browser_operation
    browser: BrowserSession | None = None
    expired = False

    def observe(delegate: ExecutionDelegate, active: BrowserSession, call: ToolCall, *,
                turn: int, tool_index: int) -> tuple[ToolResult, bool, int | None]:
        nonlocal browser
        browser = active
        return execute(delegate, active, call, turn=turn, tool_index=tool_index)

    def bind(host: CodeModeHost, name: str, args: dict[str, object]) -> object:
        nonlocal expired
        if name == "browser_eval" and isinstance(code := args.get("code"), str) and code in PROJECTIONS:
            return invoke(host, name, {**args, "code": table_projection if code == "receipt:table" else PROJECTIONS[code]})
        if name != "edit":
            return invoke(host, name, args)
        if expire_url is not None and not expired and browser is not None:
            reload = browser.navigate(expire_url)
            if not reload.ok:
                raise RuntimeError("Fixture could not reload the browser document")
            expired = True
            append_json(directory / "expiry.jsonl", {"url": expire_url, "document": browser.document_identity()})
        try:
            file_ref = args.get("base_result_ref")
            if (host._replay is not None or set(args) != {"base_result_ref", "changes"}
                    or not isinstance(file_ref, str) or file_ref not in host._complete_reads):
                raise ValueError("Use the original complete read_file result and receipt-backed changes")
            selectors = decode_json_object(args["changes"])
            if not selectors:
                raise ValueError("At least one receipt-backed change is required")
            receipts = {row.identity: row.result for row in tool_receipts(directory / "state")}
            committed = {child for result in receipts.values() if result.call.name == "codemode" and result.ok
                         and isinstance(children := result.canonical.get("child_effect_ids"), list) for child in children
                         if isinstance(child, str)}
            document = browser.document_identity() if browser is not None else None
            if document is None:
                raise ValueError("No active browser document")
            changes: JsonObject = {}
            for field, raw in selectors.items():
                selector = decode_json_object(raw)
                ref = require_json_string(selector.get("ref"), "selector.ref")
                receipt = receipts.get(ref) if ref in committed else None
                if receipt is not None and receipt.audit.get("browser_document") != document:
                    reason = "Expected a complete committed receipt from the current document; observe again"
                    append_json(directory / "bindings.jsonl", {"status": "rejected", "reason": reason,
                                "kind": "source_expired", "source_ref": ref})
                    return {"ok": False, "error": reason}
                if receipt is None or not receipt.ok or receipt.truncated:
                    raise ValueError("Expected a complete committed receipt from the current document")
                changes[field] = select_value(receipt, selector, str(document["url"]), table_projection=table_projection)
        except TableSelectionError as exc:
            reason = f"Browser field binding rejected: {exc}. Observed candidates: {json.dumps(exc.candidates, ensure_ascii=False)}"
            append_json(directory / "bindings.jsonl", {"status": "rejected", "reason": reason,
                        "kind": "selector", "candidates": exc.candidates})
            return {"ok": False, "error": reason}
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            reason = f"Browser field binding rejected: {exc}"
            append_json(directory / "bindings.jsonl", {"status": "rejected", "reason": reason})
            return {"ok": False, "error": reason}
        result = invoke(host, name, {**args, "changes": changes})
        assert isinstance(result, dict)
        append_json(directory / "bindings.jsonl", {
            "status": "applied" if result.get("ok") is True else "edit_failed",
            "file_read_ref": file_ref, "document": document, "selectors": selectors, "changes": changes,
        })
        return result

    with (patch.object(ExecutionDelegate, "_execute_browser_operation", observe),
          patch.object(task_execution, "browser_receipt_tools",
                       lambda rules: browser_receipt_tools(rules) | {"browser_read", "browser_eval"}),
          patch.object(CodeModeHost, "invoke", bind)):
        yield


def run_case(directory: Path, case: Case, target: Target, *, max_turns: int,
             task_override: str | None = None,
             provider_factory: Callable[[Target, Path], ExperimentProvider] | None = None,
             table_projection: str = TABLE, expire_source: bool = False,
             initial_fields: JsonObject | None = None) -> JsonObject:
    directory.mkdir(parents=True)
    project = directory / "project"
    project.mkdir()
    state = directory / "state"
    file = project / case.path
    initial: JsonObject = {**(dict.fromkeys(case.expected) if initial_fields is None else initial_fields),
                           "metadata": {"keep": "00017"}}
    external: JsonObject = {**initial, "external_revision": EXTERNAL_FIELD}
    write_json(file, initial)
    backup = project / "backup.json"
    backup.write_bytes(file.read_bytes())
    backup_before = backup.read_bytes()
    injected = False
    edit_snapshots: list[bytes] = []
    injected_bytes = b""

    def observe(event: dict[str, object]) -> None:
        nonlocal injected, injected_bytes
        if event.get("type") == "tool" and event.get("tool_name") == "edit":
            edit_snapshots.append(file.read_bytes())
        if (not injected and event.get("type") == "tool" and event.get("tool_name") == "read_file"
                and event.get("ok") is True and event.get("path") == case.path
                and any(item.result.call.name == "read_file" and item.result.ok
                        and item.result.call.args.get("path") == case.path
                        and isinstance(value := item.result.canonical.get("value"), dict)
                        and value.get("text_complete") is True and not item.result.truncated
                        for item in tool_receipts(state))):
            write_json(file, external)
            injected_bytes = file.read_bytes()
            injected = True

    def route(_provider: BrowserSearchProvider, request: Route) -> None:
        match = request.request.url == case.url
        request.fulfill(status=200 if match else 404, content_type="text/html; charset=utf-8",
                        body=case.html if match else "Unknown fixture URL")

    selector_shape = (
        "正文定位格式示例 {prefix:'Weight = ',suffix:' kg',type:'integer'}。根据正文选择精确边界。"
        if case.acquisition.startswith("tools.browser_read") else
        "表格定位格式示例 {id:'items',key_column:'ID',key:'R-1',column:'Total',type:'integer'}。根据回执选唯一行。"
        if "receipt:table" in case.acquisition else
        "JSON 定位格式示例 {id:'record',pointer:'/a/b',type:'string'}。根据回执选择原始字段。"
    )
    task = (f"打开 {case.url}。{case.task} 保留其他字段和 backup.json，只用 CodeMode。不要运行测试或命令。"
            "先用一段 CodeMode 执行下面的采集程序。inputs.url 为任务指定 URL。\n"
            "unwrap(await tools.browser_navigate({url:inputs.url}));\n"
            f"const capture=await {case.acquisition};\n"
            "const data=unwrap(capture); await store('source',capture.result_ref); return data;\n"
            "然后在下一次 CodeMode 调用中执行以下程序，inputs.path 是目标路径，"
            "inputs.field 是目标字段名，inputs.selector 是网页定位：\n"
            "const read=await tools.read_file({path:inputs.path});\n"
            "const ref=await load('source');\n"
            "return unwrap(await tools.edit({read,changes:{[inputs.field]:{...inputs.selector,ref}}}));\n"
            "inputs.selector 只传定位，不传值。回执编号由 store/load 传递。"
            + selector_shape +
            "如果真实版本冲突，重新完整读取文件，使用同一网页回执和定位重试。成功写入后调用 done。")
    if task_override is not None:
        task = task_override
    (directory / "task.txt").write_text(task, encoding="utf-8")
    before = production_source_digest()
    recorder = (provider_factory(target, directory) if provider_factory is not None
                else ExperimentProvider(target, directory, fixed_acquisition=False))
    try:
        with (field_binding(directory, table_projection=table_projection, expire_url=case.url if expire_source else None),
              patch.dict(os.environ, {NATIVE_TOOLS_ENV: "1"}),
              patch.object(BrowserSearchProvider, "_guard_request", route),
              patch("codey.app.provider_services.provider_availability", return_value={"local": True}),
              patch("codey.app.provider_services.provider_tab_availability", return_value={}),
              patch("codey.app.review_service.run_review", return_value=None)):
            result = run_headless(HeadlessRequest(
                project, task, intent="hybrid", provider_id="local", max_turns=max_turns, state_home=state,
                research_store_root=directory / "knowledge", project_changes_required=True,
                requested_capabilities=("web.read", "browser.observe", "browser.interact", "browser.effect"),
                required_tools=(require_tool_success("browser_navigate", via="codemode"),
                                require_tool_success("edit", via="codemode", arguments={"path": case.path})),
                model_selection={"model": target.model, "base_url": target.base_url},
            ), emit_jsonl=observe, connect_provider=lambda *_args, **_kwargs: recorder)
    finally:
        recorder.close()
    receipts = tool_receipts(state)
    audit = receipt_audit([(item.identity, item.result) for item in receipts])
    write_json(directory / "receipts.json", audit)
    disk = decode_json_object(json.loads(file.read_text(encoding="utf-8")))
    stable = before == production_source_digest()
    backup_preserved = backup.read_bytes() == backup_before
    unchanged_on_conflict = bool(edit_snapshots) and edit_snapshots[0] == injected_bytes
    binding_log = directory / "bindings.jsonl"
    bindings: list[JsonValue] = [decode_json_object(json.loads(line))
        for line in binding_log.read_text(encoding="utf-8").splitlines()] if binding_log.exists() else []
    applied = [r for r in bindings if isinstance(r, dict) and r.get("status") == "applied"]
    correctly_bound = len(applied) == 1 and applied[0]["changes"] == case.expected
    version_ok = ((directory / "expiry.jsonl").exists() and len(edit_snapshots) == 1
                  and audit["true_stale_conflicts"] == 0 if expire_source else
                  unchanged_on_conflict and len(edit_snapshots) == 2
                  and audit["true_stale_conflicts"] == 1 and audit["fresh_read_recovery"] is True)
    passed = (result.stop_reason == "done" and stable and backup_preserved and version_ok
              and correctly_bound and disk == {**external, **case.expected})
    summary: JsonObject = {"passed": passed, "stop_reason": result.stop_reason, "source_stable": stable,
        "project_result": disk, "backup_preserved": backup_preserved, "unchanged_on_conflict": unchanged_on_conflict,
        "true_stale_conflicts": audit["true_stale_conflicts"], "fresh_read_recovery": audit["fresh_read_recovery"],
        "bindings": bindings, **rejection_audit(state), **provider_metrics(directory / "provider.jsonl")}
    write_json(directory / "summary.json", summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--max-turns", default=8, type=int)
    parser.add_argument("--base-url", default="http://127.0.0.1:5001/v1")
    args = parser.parse_args()
    root = args.run_dir.resolve()
    root.mkdir(parents=True, exist_ok=False)
    target = select_target(90, args.base_url)
    source = production_source_digest()
    script = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    rows: JsonObject = {}
    for name, case in CASES.items():
        print(f"Starting {name}", flush=True)
        row = run_case(root / name, case, target, max_turns=args.max_turns)
        rows[name] = row
        print(f"{name}: passed={row['passed']} stop={row['stop_reason']} calls={row['model_calls']}", flush=True)
    stable = source == production_source_digest()
    script_stable = script == hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    write_json(root / "summary.json", {"cases": rows, "source_stable": stable, "source": source,
        "script_stable": script_stable, "script_sha256": script, "model": target.model,
        "seed": target.seed, "temperature": 0})
    return 0 if stable and script_stable and all(isinstance(row, dict) and row["passed"] is True for row in rows.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
