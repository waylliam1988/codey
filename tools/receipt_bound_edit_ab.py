"""Live paired probe of explicit edit receipts versus a scoped CodeMode helper.

Only this harness installs the proposed helper. Both arms use the production
headless entry, model provider, guest, read_file, edit, authorization and receipts.
No production assets, settings, Git checkout or worktree are changed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import traceback
from collections.abc import Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from unittest.mock import patch

from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.codemode import catalog as codemode_catalog
from codey.codemode.runtime import NodeWorkerRuntime, ToolInvoker
from codey.codemode.types import CodeModeLimits, CodeModeResult, ToolCatalogEntry, ToolCatalogSnapshot
from codey.env_names import NATIVE_TOOLS_ENV
from codey.operations import kernel_protocol
from codey.operations.kernel_protocol import KernelSessionView, TurnSnapshot
from codey.policies.tool_requirement import require_tool_success
from codey.runtime.core import cancellation
from codey.toolchain.tool_prompt import json_contract_text
from codey.toolchain.tool_spec import is_text_batch_tool
from codey.types.json_value import JsonObject, JsonValue, decode_json_object, require_json_integer
from tools import local_model_gate_attempts as attempts
from tools.shell_web_diagnostic_gate import select_target

HELPER = """return await (async (hostTools) => {
let helperCalls = 0;
let helperCompleted = 0;
const tools = new Proxy(hostTools, {
  get(target, property) {
    if (property !== "edit") return Reflect.get(target, property);
    return async ({path, transform}) => {
      helperCalls += 1;
      const read = await hostTools.read_file({path});
      const content = transform(unwrap(read).text);
      const edited = await hostTools.edit({path, content, base_result_ref: read.result_ref});
      unwrap(edited);
      helperCompleted += 1;
      return edited;
    };
  },
});
return {probe_value: await (async () => {
"""
HELPER_SUFFIX = "\n})(), probe_helper_calls: helperCalls, probe_helper_completed: helperCompleted};\n})(tools);\n"
HELPER_DIGEST = hashlib.sha256((HELPER + HELPER_SUFFIX).encode("utf-8")).hexdigest()
DECLARATION = (
    "  // Guest composition: reads this path, transforms its text, edits using that exact receipt. Errors propagate.\n"
    "  // Callback stays in the guest. No content/base_result_ref/replacements arguments on this interface.\n"
    "  edit(args: {path: string; transform: (text: string) => string}): Result<string>;\n"
    "  // Example: return unwrap(await tools.edit({path: inputs.path, transform: text => text.replace('old', 'new')}));\n"
)
ROOT = Path(__file__).resolve().parents[1]
INTERFACE_VERSION = "guest-tools-edit-transform-v3"


@dataclass(frozen=True)
class EditCase:
    name: str
    task: str
    initial: JsonObject
    expected: JsonObject


CASES = (
    EditCase("record", "将 amount 改成数字 173，code 改成字符串 QUARTZ_851。",
             {"amount": 0, "code": "unknown", "metadata": {"note": '保留 "原文"', "active": False}},
             {"amount": 173, "code": "QUARTZ_851", "metadata": {"note": '保留 "原文"', "active": False}}),
    EditCase("nested", "只将 services 中 name 为 reporting 的 retries 改成数字 5。",
             {"services": [{"name": "ingest", "retries": 2}, {"name": "reporting", "retries": 2}],
              "enabled": True, "version": 7},
             {"services": [{"name": "ingest", "retries": 2}, {"name": "reporting", "retries": 5}],
              "enabled": True, "version": 7}),
    EditCase("unicode", '将 title 改成两行文字：第一行是 福州 "鱼丸"，第二行是 已核验；在 tags 末尾追加字符串 确认。',
             {"title": "旧标题", "tags": ["海味", "汤"], "active": False, "revision": 2},
             {"title": '福州 "鱼丸"\n已核验', "tags": ["海味", "汤", "确认"], "active": False, "revision": 2}),
)


def write_json(path: Path, value: JsonValue) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def mentions_helper_call(code: str) -> bool:
    """Lexical hint only; executed counters are the adoption evidence."""
    return bool(re.search(r"\btools\s*\.\s*edit\s*\(", code))


def codemode_only_surface(snapshot: TurnSnapshot) -> TurnSnapshot:
    """Both arms expose project leaves through CodeMode, retaining their frozen executors."""
    outer = tuple(name for name in snapshot.tool_names
                  if name not in snapshot.nested_tool_names and not is_text_batch_tool(name))
    inventory = tuple(dict.fromkeys((*outer, *snapshot.nested_tool_names)))
    by_name = {spec.name: spec for spec in snapshot.frozen_specs}
    executors = dict(snapshot.custom_executors)
    return replace(snapshot, tool_names=outer,
                   contract_text=json_contract_text(by_name[name] for name in outer),
                   frozen_specs=tuple(by_name[name] for name in inventory),
                   custom_executors=tuple((name, executors[name]) for name in inventory))


def helper_metrics(directory: Path) -> JsonObject:
    """Only successful guest envelopes confirm execution; failed programs stay unknown."""
    path = directory / "helper-executions.jsonl"
    rows = [decode_json_object(json.loads(line)) for line in path.read_text(encoding="utf-8").splitlines()
            ] if path.exists() else []
    return {
        "confirmed_helper_calls": sum(require_json_integer(row["helper_calls"], "helper_calls")
                                      for row in rows if row["helper_calls"] is not None),
        "confirmed_helper_edits": sum(require_json_integer(row["helper_completed"], "helper_completed")
                                      for row in rows if row["helper_completed"] is not None),
        "untraced_failed_programs": sum(row["ok"] is False for row in rows),
    }


def production_source_digest() -> str:
    files = subprocess.run(["git", "ls-files", "-co", "--exclude-standard", "--", "codey"],
                           cwd=ROOT, check=True, capture_output=True, text=True, encoding="utf-8").stdout.splitlines()
    digest = hashlib.sha256()
    for name in sorted(set(files)):
        path = ROOT / name
        digest.update(name.encode("utf-8") + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest() if path.is_file() else b"deleted")
    return digest.hexdigest()


@contextmanager
def prototype_helper(directory: Path, *, enabled: bool) -> Iterator[None]:
    """Prepend only the helper; retain the existing runtime and host executor."""
    execute = NodeWorkerRuntime.execute
    contract_digest = NodeWorkerRuntime.contract_digest
    declarations = codemode_catalog.render_declarations
    build_snapshot = kernel_protocol.build_turn_snapshot

    def probe_snapshot(session: KernelSessionView, *, native: bool = False) -> TurnSnapshot:
        return codemode_only_surface(build_snapshot(session, native=native))

    def helper_declarations(snapshot: ToolCatalogSnapshot) -> str:
        visible = replace(snapshot, entries=tuple(entry for entry in snapshot.entries if entry.name != "edit"))
        return declarations(visible).replace("\n};", "\n" + DECLARATION + "};")

    def recorded_execute(
        runtime: NodeWorkerRuntime, code: str, *, catalog: Sequence[ToolCatalogEntry], invoke: ToolInvoker,
        inputs: Mapping[str, object] | None = None, state_store: object = None,
        replay_context: Mapping[str, object] | None = None, limits: CodeModeLimits | None = None,
    ) -> CodeModeResult:
        expanded = HELPER + code + HELPER_SUFFIX if enabled else code
        with (directory / "programs.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"model_code": code, "executed_code": expanded}, ensure_ascii=False) + "\n")
        result = execute(runtime, expanded, catalog=catalog, invoke=invoke, inputs=inputs,
                         state_store=state_store, replay_context=replay_context, limits=limits)
        trace: JsonObject = {"ok": result.ok, "helper_calls": None, "helper_completed": None}
        if enabled and result.ok:
            envelope = decode_json_object(result.value)
            trace.update(helper_calls=require_json_integer(envelope["probe_helper_calls"], "probe_helper_calls"),
                         helper_completed=require_json_integer(envelope["probe_helper_completed"], "probe_helper_completed"))
            result = replace(result, value=envelope["probe_value"])
        with (directory / "helper-executions.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(trace, ensure_ascii=False) + "\n")
        return result

    def bound_contract_digest(runtime: NodeWorkerRuntime) -> str:
        original = contract_digest(runtime)
        return hashlib.sha256((original + ":guest-tools-edit-transform:" + HELPER_DIGEST).encode("utf-8")).hexdigest()

    with ExitStack() as stack:
        stack.enter_context(patch.object(NodeWorkerRuntime, "execute", recorded_execute))
        stack.enter_context(patch.object(kernel_protocol, "build_turn_snapshot", probe_snapshot))
        if enabled:
            stack.enter_context(patch.object(NodeWorkerRuntime, "contract_digest", bound_contract_digest))
            stack.enter_context(patch.object(codemode_catalog, "render_declarations", helper_declarations))
        yield


def provider_metrics(path: Path) -> JsonObject:
    """Count each recorded exchange once; missing usage remains unknown."""
    requests: set[tuple[str, int]] = set()
    responses: dict[tuple[str, int], JsonObject] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            row = decode_json_object(json.loads(line))
            identity = (str(row["recorder_id"]), require_json_integer(row["exchange"], "exchange"))
            if row["type"] == "request":
                requests.add(identity)
            elif row["type"] == "response":
                responses[identity] = row
    prompt_tokens = completion_tokens = 0
    usage_complete = bool(responses) and requests == responses.keys()
    inference_seconds = 0.0
    for row in responses.values():
        payload = row["payload"]
        assert isinstance(payload, dict)
        usage = payload.get("usage")
        if not isinstance(usage, dict) or not all(type(usage.get(key)) is int for key in (
            "prompt_tokens", "completion_tokens",
        )):
            usage_complete = False
        else:
            prompt_tokens += require_json_integer(usage["prompt_tokens"], "prompt_tokens")
            completion_tokens += require_json_integer(usage["completion_tokens"], "completion_tokens")
        seconds = row["seconds"]
        assert isinstance(seconds, (float, int))
        inference_seconds += seconds
    return {"model_calls": len(requests), "model_responses": len(responses), "usage_complete": usage_complete,
            "prompt_tokens": prompt_tokens if usage_complete else None,
            "completion_tokens": completion_tokens if usage_complete else None,
            "inference_seconds": round(inference_seconds, 3)}


def run_arm(directory: Path, case: EditCase, arm: str, target: attempts.GateTarget, *, max_turns: int) -> JsonObject:
    directory.mkdir(parents=True, exist_ok=False)
    project = directory / "project"
    project.mkdir()
    write_json(project / "result.json", case.initial)
    write_json(project / "result-backup.json", case.initial)
    before = {path.name: path.read_bytes() for path in project.iterdir()}
    events: list[JsonObject] = []
    source_before = production_source_digest()
    started = time.monotonic()

    def observe(payload: dict[str, object]) -> None:
        event = decode_json_object(payload)
        events.append(event)
        with (directory / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")

    def connect(provider_id: str, **_kwargs: object) -> attempts.RecordingProvider:
        if provider_id != "local":
            raise ValueError("probe requires the selected local model")
        return attempts.RecordingProvider(target, directory)

    common = ("只修改现有 result.json。" + case.task + "保留其余字段、嵌套内容和 JSON 类型，result-backup.json 不得改变。"
              "本任务专门验收整文件更新。"
              "用 codemode 完成文件操作，返回实际写入结果，然后用 done 简短报告。不要运行命令、测试或浏览网页。")
    interface = ("当前使用现有协议：先 tools.read_file 完整读取，再 tools.edit；使用 content，不用 replacements。"
                 "回执编号在 readResult.result_ref，正文在 unwrap(readResult).text；检查工具是否成功。"
                 "示例：const r=await tools.read_file({path:inputs.path}); "
                 "return unwrap(await tools.edit({path:inputs.path,content:unwrap(r).text.replace('old','new'),"
                 "base_result_ref:r.result_ref}));") if arm == "A" else (
                     "本实验 tools.edit 的唯一签名是 tools.edit({path, transform})，返回 Result<string>。"
                     "transform 收到读取正文并返回新正文；读取回执由程序绑定，读取不完整或版本变动仍拒绝写入。"
                     "示例：return unwrap(await tools.edit({path:inputs.path,transform:text=>text.replace('old','new')}));")
    summary: JsonObject = {"case": case.name, "arm": arm, "passed": False}
    try:
        with (prototype_helper(directory, enabled=arm == "B"),
              patch.dict(os.environ, {NATIVE_TOOLS_ENV: "1"}),
              attempts.isolate_gate_secondary_models(target)):
            result = run_headless(HeadlessRequest(
                project=project, task=common + interface, intent="project", provider_id="local", max_turns=max_turns,
                state_home=directory / "state", research_store_root=directory / "knowledge", allow_web=False,
                project_changes_required=True,
                required_tools=(require_tool_success("read_file", via="codemode"),
                                require_tool_success("edit", via="codemode")),
                model_selection={"model": target.model, "base_url": target.base_url},
            ), emit_jsonl=observe, connect_provider=connect)
        actual = decode_json_object(json.loads((project / "result.json").read_text(encoding="utf-8")))
        after = {path.relative_to(project).as_posix(): path.read_bytes() for path in project.rglob("*") if path.is_file()}
        changed = sorted(name for name in before.keys() | after.keys() if before.get(name) != after.get(name))
        changed_files: list[JsonValue] = [name for name in changed]
        preserved = (project / "result-backup.json").read_bytes() == before["result-backup.json"]
        correct = json.dumps(actual, sort_keys=True, allow_nan=False) == json.dumps(
            case.expected, sort_keys=True, allow_nan=False)
        summary.update(stop_reason=result.stop_reason, target_correct=correct, backup_preserved=preserved,
                       changed_files=changed_files, passed=result.stop_reason == "done" and correct and preserved
                       and changed == ["result.json"])
    except Exception as exc:
        summary.update(error=f"{type(exc).__name__}: {exc}")
        (directory / "error.txt").write_text(traceback.format_exc(), encoding="utf-8")
    tools = [row for row in events if row.get("type") == "tool"]
    programs = directory / "programs.jsonl"
    helper_programs = sum(mentions_helper_call(str(
        decode_json_object(json.loads(line))["model_code"])) for line in programs.read_text(encoding="utf-8").splitlines()
    ) if programs.exists() else 0
    summary.update(
        seconds=round(time.monotonic() - started, 3),
        source_before=source_before, source_after=production_source_digest(),
        source_digest_kind="codey-files-sha256-v2",
        first_pass_success=summary["passed"] is True and not any(row.get("ok") is False for row in tools),
        read_calls=sum(row.get("tool_name") == "read_file" for row in tools),
        edit_calls=sum(row.get("tool_name") == "edit" for row in tools),
        failed_edits=sum(row.get("tool_name") == "edit" and row.get("ok") is False for row in tools),
        failed_codemode_calls=sum(row.get("tool_name") == "codemode" and row.get("ok") is False for row in tools),
        edit_call_programs=helper_programs,
        **helper_metrics(directory),
        **provider_metrics(directory / "provider.jsonl"),
    )
    write_json(directory / "summary.json", summary)
    return summary


def run_worker(directory: Path, case: EditCase, arm: str, *, base_url: str, timeout: float, max_turns: int) -> JsonObject:
    command = [sys.executable, "-m", "tools.receipt_bound_edit_ab", "--run-dir", str(directory),
               "--worker-case", case.name, "--worker-arm", arm, "--kobold-base-url", base_url,
               "--timeout", str(timeout), "--max-turns", str(max_turns)]
    process, owner = cancellation.start_process(command, cwd=ROOT)
    error = ""
    output = ""
    try:
        completed = cancellation.wait_process(process, owner, command, timeout, capture_limit_bytes=100_000)
        output = completed.stdout + completed.stderr
        if completed.returncode:
            error = f"worker exited {completed.returncode}"
    except subprocess.TimeoutExpired:
        error = f"worker exceeded {timeout}s; owned process tree terminated"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "worker.log").write_text(output + error, encoding="utf-8")
    summary_path = directory / "summary.json"
    if summary_path.exists() and not error:
        return decode_json_object(json.loads(summary_path.read_text(encoding="utf-8")))
    summary: JsonObject = {"case": case.name, "arm": arm, "passed": False, "first_pass_success": False,
                           "error": error, "seconds": timeout, "failed_edits": None,
                           "failed_codemode_calls": None, "confirmed_helper_calls": None,
                           "confirmed_helper_edits": None, "untraced_failed_programs": None,
                           **provider_metrics(directory / "provider.jsonl")}
    write_json(summary_path, summary)
    return summary


def aggregate(rows: list[JsonObject], arm: str) -> JsonObject:
    selected = [row for row in rows if row["arm"] == arm]
    summary: JsonObject = {"runs": len(selected), "passed": sum(row["passed"] is True for row in selected),
                           "first_pass_successes": sum(row["first_pass_success"] is True for row in selected),
                           "runs_mentioning_edit": sum(isinstance(row.get("edit_call_programs"), int)
                                                      and bool(row["edit_call_programs"]) for row in selected)}
    for key in ("model_calls", "prompt_tokens", "completion_tokens", "failed_edits", "failed_codemode_calls",
                "confirmed_helper_calls", "confirmed_helper_edits", "untraced_failed_programs"):
        values = [row[key] for row in selected]
        summary[key] = sum(require_json_integer(value, key) for value in values) if all(
            type(value) is int for value in values) else None
    for key in ("seconds", "inference_seconds"):
        values = [row[key] for row in selected]
        assert all(isinstance(value, (int, float)) for value in values)
        summary[key] = round(sum(float(str(value)) for value in values), 3)
    return summary


def audit_run(root: Path) -> JsonObject:
    """Recompute derived adoption metrics from the unmodified model programs."""
    summary = decode_json_object(json.loads((root / "summary.json").read_text(encoding="utf-8")))
    manifest = decode_json_object(json.loads((root / "manifest.json").read_text(encoding="utf-8")))
    if manifest.get("interface_version") != INTERFACE_VERSION:
        raise ValueError("This auditor requires guest-tools-edit-transform-v3 artifacts; preserve earlier audits.")
    audited: list[JsonValue] = []
    pairs: dict[str, dict[str, tuple[JsonObject, int]]] = {}
    for programs in sorted(root.glob("??-*/*/programs.jsonl")):
        directory = programs.parent
        row = decode_json_object(json.loads((directory / "summary.json").read_text(encoding="utf-8")))
        code = [str(decode_json_object(json.loads(line))["model_code"])
                for line in programs.read_text(encoding="utf-8").splitlines()]
        metrics = helper_metrics(directory)
        completed = require_json_integer(metrics["confirmed_helper_edits"], "confirmed_helper_edits")
        pairs.setdefault(directory.parent.name, {})[str(row["arm"])] = (row, completed)
        audited.append({"directory": str(directory.relative_to(root)), "case": row["case"], "arm": row["arm"],
                        "edit_call_programs": sum(mentions_helper_call(program) for program in code),
                        **metrics,
                        "source_digest_kind": row.get("source_digest_kind", "git-state-v1"),
                        "source_stable": row.get("source_before") == row.get("source_after")
                        if row.get("source_before") else None})
    comparisons: list[JsonValue] = []
    for name, arms in sorted(pairs.items()):
        if set(arms) != {"A", "B"}:
            comparisons.append({"pair": name, "efficacy_comparable": False, "reason": "missing arm"})
            continue
        sources = [row.get(key) for row, _calls in arms.values() for key in ("source_before", "source_after")]
        comparable = (all(row.get("source_digest_kind") == "codey-files-sha256-v2" for row, _calls in arms.values())
                      and all(isinstance(source, str) for source in sources) and len(set(sources)) == 1)
        adopted = arms["B"][1] > 0
        comparisons.append({"pair": name, "source_comparable": comparable, "helper_adopted": adopted,
                            "efficacy_comparable": comparable and adopted})
    audit: JsonObject = {"A": summary["A"], "B": summary["B"], "program_audit": audited,
                         "pair_comparisons": comparisons,
                         "adoption_rule": "Guest execution counters confirm calls and edits. Failed guest traces are unknown."}
    write_json(root / "audit.json", audit)
    return audit


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--kobold-base-url", default="http://127.0.0.1:5001/v1")
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--max-turns", type=int, default=8)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--worker-case", choices=[case.name for case in CASES], help=argparse.SUPPRESS)
    parser.add_argument("--worker-arm", choices=("A", "B"), help=argparse.SUPPRESS)
    parser.add_argument("--audit-only", action="store_true", help="Audit existing artifacts without calling a model.")
    args = parser.parse_args()
    if args.repeats < 1 or args.max_turns < 1:
        parser.error("repeats and max-turns must be positive")
    if args.audit_only:
        print(json.dumps(audit_run(args.run_dir.resolve()), ensure_ascii=True), flush=True)
        return 0
    target = select_target("gemma", args.timeout, args.kobold_base_url)
    if args.worker_case:
        if not args.worker_arm:
            parser.error("worker-arm is required for worker-case")
        case = next(case for case in CASES if case.name == args.worker_case)
        run_arm(args.run_dir.resolve(), case, args.worker_arm, target, max_turns=args.max_turns)
        return 0
    root = args.run_dir.resolve()
    root.mkdir(parents=True, exist_ok=False)
    (root / "helper.js").write_text(HELPER + "/* model program */" + HELPER_SUFFIX, encoding="utf-8")
    write_json(root / "manifest.json", {
        "scope": "prototype_helper_over_existing_production_runtime", "model": target.model,
        "base_url": target.base_url, "temperature": target.temperature, "helper_sha256": HELPER_DIGEST,
        "max_turns": args.max_turns, "repeats": args.repeats, "recovery_tested": False,
        "production_source_at_start": production_source_digest(),
        "source_digest_kind": "codey-files-sha256-v2",
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "helper_declaration": DECLARATION,
        "interface_version": INTERFACE_VERSION,
        "first_pass_definition": "task completed correctly without any failed tool receipt",
        "limitations": "Small pilot; prompt caching and model variation affect wall time. No browser work is included.",
    })
    rows: list[JsonObject] = []
    for repetition in range(args.repeats):
        for index, case in enumerate(CASES):
            order = ("A", "B") if (repetition + index) % 2 == 0 else ("B", "A")
            pair = root / f"{repetition + 1:02d}-{case.name}"
            pair.mkdir()
            for arm in order:
                print(json.dumps({"starting": case.name, "arm": arm, "repetition": repetition + 1}), flush=True)
                row = run_worker(pair / arm, case, arm, base_url=args.kobold_base_url,
                                 timeout=args.timeout, max_turns=args.max_turns)
                rows.append(row)
                write_json(root / "summary.json", {"A": aggregate(rows, "A"), "B": aggregate(rows, "B"),
                                                   "runs": [row for row in rows]})
                print(json.dumps(row, ensure_ascii=True), flush=True)
    audit_run(root)
    return 0 if all(row["passed"] is True for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
