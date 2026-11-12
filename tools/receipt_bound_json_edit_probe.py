"""Compare receipt-bound full text with receipt-bound field updates."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import NoReturn
from unittest.mock import patch

from codey.codemode.runtime import NodeWorkerRuntime, ToolInvoker
from codey.codemode.types import CodeModeLimits, CodeModeResult, ToolCatalogEntry
from codey.types.json_value import JsonObject, JsonValue, decode_json_object
from tools import receipt_bound_edit_ab as ab
from tools.completion_artifact_audit import EffectReceipt, tool_receipts
from tools.local_model_gate_attempts import GateTarget
from tools.receipt_bound_edit_recovery_probe import external_content, receipt_audit
from tools.shell_web_diagnostic_gate import select_target

CASE = ab.CASES[0]
FIELDS: JsonObject = {"amount": CASE.expected["amount"], "code": CASE.expected["code"]}
TASK = ("只修改现有 result.json 的 amount 和 code。将 amount 改成数字 173，code 改成字符串 QUARTZ_851。"
        "保留其他字段和 JSON 类型，result-backup.json 不得改变。使用 CodeMode 读取并修改，检查写入结果后用 done 报告。"
        "如果文件版本冲突，重新完整读取，再对新读取提交同样两个字段。不要运行命令、测试或浏览网页。")
HELPER = ab.read_object_helper("changes", "content: changes")
DECLARATION = (
    "  // Pass the original result from tools.read_file in this program as read. Supply only the new field values.\n"
    "  // The application preserves all other fields. After a version conflict, read again and repeat the same changes.\n"
    "  edit(args: {read: Awaited<ReturnType<typeof tools.read_file>>; changes: {amount: number; code: string}}): Result<string>;\n"
    "  // Example: const r=await tools.read_file({path:inputs.path}); "
    "return unwrap(await tools.edit({read:r,changes:{amount:inputs.amount,code:inputs.code}}));\n"
)
_WS = re.compile(r"[ \t\r\n]*")


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    data: dict[str, object] = {}
    for key, value in pairs:
        if key in data:
            raise ValueError("duplicate JSON field")
        data[key] = value
    return data


def _invalid_constant(value: str) -> NoReturn:
    raise ValueError(f"invalid JSON constant: {value}")


_DECODER = json.JSONDecoder(object_pairs_hook=_unique, parse_float=Decimal, parse_constant=_invalid_constant)


def _skip(text: str, offset: int) -> int:
    match = _WS.match(text, offset)
    assert match is not None
    return match.end()


def patch_fields(text: str, changes: JsonObject) -> str:
    if (set(changes) != FIELDS.keys() or type(changes["amount"]) is not int
            or not isinstance(changes["code"], str)):
        raise ValueError("changes must contain an integer amount and a string code")
    # Validate the whole document, then replace only the two value spans.
    original: object = _DECODER.decode(text)
    if not isinstance(original, dict) or not changes.keys() <= original.keys():
        raise ValueError("expected a JSON object with existing amount and code fields")
    cursor = _skip(text, 0) + 1
    spans: list[tuple[int, int, str]] = []
    for key in original:
        _, key_end = _DECODER.raw_decode(text, _skip(text, cursor))
        start = _skip(text, _skip(text, key_end) + 1)
        _, end = _DECODER.raw_decode(text, start)
        if key in changes:
            spans.append((start, end, json.dumps(changes[key], ensure_ascii=False, allow_nan=False)))
        cursor = _skip(text, end) + 1
    for start, end, replacement in reversed(spans):
        text = text[:start] + replacement + text[end:]
    return text


@contextmanager
def field_interface(directory: Path) -> Generator[None, None, None]:
    execute = NodeWorkerRuntime.execute

    def lower_edits(runtime: NodeWorkerRuntime, code: str, *, catalog: Sequence[ToolCatalogEntry],
                    invoke: ToolInvoker, inputs: Mapping[str, object] | None = None, state_store: object = None,
                    replay_context: Mapping[str, object] | None = None,
                    limits: CodeModeLimits | None = None) -> CodeModeResult:
        reads: dict[str, str] = {}

        def lower(name: str, args: dict[str, object]) -> object:
            if name == "edit":
                ref = args.get("base_result_ref")
                if args.get("path") != "result.json" or not isinstance(ref, str) or ref not in reads:
                    return {"ok": False, "error": "edit requires an original complete result.json read from this program"}
                try:
                    content = patch_fields(reads[ref], decode_json_object(args.get("content")))
                except (ValueError, TypeError) as exc:
                    return {"ok": False, "error": f"JSON field edit rejected: {exc}"}
                return invoke(name, {**args, "content": content})
            result = invoke(name, args)
            if name == "read_file" and isinstance(result, dict) and result.get("ok") is True:
                ref, value = result.get("result_ref"), result.get("value")
                if (isinstance(ref, str) and isinstance(value, dict) and value.get("path") == "result.json"
                        and value.get("text_complete") is True and isinstance(text := value.get("text"), str)):
                    reads[ref] = text
            return result

        return execute(runtime, code, catalog=catalog, invoke=lower, inputs=inputs, state_store=state_store,
                       replay_context=replay_context, limits=limits)

    with (patch.object(NodeWorkerRuntime, "execute", lower_edits),
          ab.prototype_helper(directory, enabled=True, helper=HELPER, declaration=DECLARATION)):
        yield


def run_arm(directory: Path, arm: str, target: GateTarget, *, max_turns: int) -> JsonObject:
    path = directory / "project" / "result.json"
    state = directory / "state"
    injected: bytes | None = None
    edit_snapshots: list[bytes] = []
    expected = external_content(CASE.expected)

    def observe(event: JsonObject) -> None:
        nonlocal injected
        if event.get("type") != "tool":
            return
        if event.get("tool_name") == "edit":
            edit_snapshots.append(path.read_bytes())
        if (injected is not None or event.get("tool_name") != "read_file"
                or event.get("ok") is not True or event.get("path") != "result.json"):
            return
        if not any(row.result.call.name == "read_file" and row.result.ok
                   and row.result.call.args.get("path") == "result.json"
                   and isinstance(value := row.result.canonical.get("value"), dict)
                   and value.get("text_complete") is True for row in tool_receipts(state)):
            return
        current = decode_json_object(json.loads(path.read_text(encoding="utf-8")))
        ab.write_json(path, external_content(current))
        injected = path.read_bytes()
        (directory / "injected.json").write_bytes(injected)

    with (patch("codey.app.provider_services.provider_tab_availability", return_value={}),
          patch("codey.app.review_service.run_review", return_value=None)):
        # Both arms get the original-read-object helper. Only B changes the edit payload.
        row = ab.run_arm(directory, replace(CASE, expected=expected), "B", target, max_turns=max_turns,
                         task=TASK, on_event=observe, interface=field_interface if arm == "B" else None)
    receipts: list[EffectReceipt] = tool_receipts(state) if (state / "durable.sqlite3").exists() else []
    audit = receipt_audit([(receipt.identity, receipt.result) for receipt in receipts])
    ab.write_json(directory / "receipts.json", audit)
    for index, contents in enumerate(edit_snapshots, 1):
        (directory / f"after-edit-{index}.json").write_bytes(contents)
    stable = row["source_before"] == row["source_after"]
    untouched = injected is not None and bool(edit_snapshots) and edit_snapshots[0] == injected
    row.update(arm=arm, source_stable=stable, injected=injected is not None,
               rejected_edit_left_file_unchanged=untouched, true_stale_conflicts=audit["true_stale_conflicts"],
               fresh_read_recovery=audit["fresh_read_recovery"],
               passed=row["passed"] is True and stable and untouched and len(edit_snapshots) == 2
               and audit["true_stale_conflicts"] == 1 and audit["fresh_read_recovery"] is True)
    ab.write_json(directory / "summary.json", row)
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--max-turns", type=int, default=8)
    parser.add_argument("--kobold-base-url", default="http://127.0.0.1:5001/v1")
    parser.add_argument("--worker-case", choices=[CASE.name], help=argparse.SUPPRESS)
    parser.add_argument("--worker-arm", choices=("A", "B"), help=argparse.SUPPRESS)
    parser.add_argument("--expected-model", help=argparse.SUPPRESS)
    args = parser.parse_args()
    target = select_target("gemma", args.timeout, args.kobold_base_url)
    root = args.run_dir.resolve()
    if args.worker_case:
        if not args.worker_arm or args.expected_model != target.model:
            parser.error("worker needs the matrix arm and the unchanged model")
        run_arm(root, args.worker_arm, target, max_turns=args.max_turns)
        return 0
    root.mkdir(parents=True)
    source = ab.production_source_digest()
    paths = (Path(__file__), Path(ab.__file__))
    hashes: JsonObject = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    manifest: JsonObject = {"model": target.model, "temperature": target.temperature, "protocol": target.protocol,
                           "source": source, "harness_sha256": hashes, "max_turns": args.max_turns, "timeout": args.timeout}
    ab.write_json(root / "manifest.json", manifest)
    rows: list[JsonValue] = []
    for arm in ("A", "B"):
        print(f"Starting stale_edit/{arm}", flush=True)
        row = ab.run_worker(root / arm, CASE, arm, base_url=args.kobold_base_url, timeout=args.timeout,
                            max_turns=args.max_turns, worker_module="tools.receipt_bound_json_edit_probe",
                            worker_args=("--expected-model", target.model))
        rows.append(row)
        print(f"stale_edit/{arm}: passed={row['passed']} stop={row.get('stop_reason')} "
              f"calls={row.get('model_calls')} error={row.get('error')}", flush=True)
        ab.write_json(root / "progress.json", rows)
    task_files = [root / arm / "task.txt" for arm in ("A", "B")]
    same_task = all(path.exists() for path in task_files) and task_files[0].read_bytes() == task_files[1].read_bytes()
    stable = (source == ab.production_source_digest()
              and hashes == {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths})
    ab.write_json(root / "summary.json", {**manifest, "runs": rows, "task_identical": same_task, "sources_stable": stable})
    return 0 if stable and same_task and all(isinstance(row, dict) and row["passed"] is True for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
