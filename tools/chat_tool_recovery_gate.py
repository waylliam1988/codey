"""Local Chat file/web tasks and single-generation controlled repair probes."""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from unittest.mock import patch

from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.env_names import NATIVE_TOOLS_ENV
from codey.operations.kernel_prompt import _repair_prompt, kernel_prompt_for_session
from codey.operations.kernel_protocol import build_turn_snapshot, normalize_turn
from codey.operations.task_guidance import task_guidance_for_policy
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.protocols.text_turn import decode_text_turn
from codey.providers.base import tools_from_specs
from codey.providers.text_tool_schema import text_tool_schema
from codey.runtime.core.models import ToolPlan
from codey.types.json_value import JsonObject, decode_json_object
from tools import local_model_gate_attempts as attempts
from tools.chat_answer_format_gate import SOURCE, fixed_web
from tools.shell_web_diagnostic_gate import select_target

NOTES = "核验码：FZ-7319\n口味偏好：清淡，早餐想吃锅边糊。\n"
TASKS = {
    "local": "读取工作区 notes.md，告诉我里面的核验码和口味偏好。不要修改文件，不要上网。",
    "mixed": "先读取工作区 notes.md，再上网搜索并打开网页正文看看福州有什么好吃的，结合我的口味推荐早餐，附来源和核验码。不要修改文件。",
}


def save(path: Path, value: JsonObject) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf8")


def repair_action_ok(plan: ToolPlan, *, necessary: bool) -> bool:
    if plan.protocol_error:
        return False
    if necessary:
        return (len(plan.calls) == 1 and plan.calls[0].name == "read_file"
                and plan.calls[0].args.get("path") == "notes.md")
    control = plan.control
    return ((len(plan.calls) == 1 and plan.calls[0].name in {"web_search", "open_url"})
        or (not plan.calls and control is not None and control.kind == "done" and SOURCE in control.body
            and all(food in control.body for food in ("鱼丸", "肉燕", "锅边糊"))))


def workspace_trial(directory: Path, case: str) -> JsonObject:
    directory.mkdir(parents=True, exist_ok=False)
    project = directory / "project"
    project.mkdir()
    (project / "notes.md").write_text(NOTES, encoding="utf8")
    target = select_target("gemma", 180, "http://127.0.0.1:5001/v1")
    events: list[JsonObject] = []

    def observe(row: dict[str, object]) -> None:
        events.append(decode_json_object(row))
        with (directory / "events.jsonl").open("a", encoding="utf8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")

    def connect(provider_id: str, **kwargs: object) -> attempts.RecordingProvider:
        if provider_id != "local":
            raise ValueError("Only the pinned local model is allowed")
        return attempts.RecordingProvider(target, directory)

    save(directory / "manifest.json", {"case": case, "task": TASKS[case], "model": target.model,
        "source_mode": "controlled_if_called", "entry": "production_headless", "seeded_error": False})
    started = time.monotonic()
    with (patch.dict(os.environ, {NATIVE_TOOLS_ENV: "1"}), fixed_web(),
          attempts.isolate_gate_secondary_models(target), attempts.record_api_generations(directory / "provider.jsonl")):
        run_headless(HeadlessRequest(project.resolve(), TASKS[case], intent="chat", provider_id="local",
            max_turns=8, state_home=directory / "state", project_changes_required=False,
            model_selection={"model": target.model, "base_url": target.base_url}),
            emit_jsonl=observe, connect_provider=connect)
    terminal = next((row for row in reversed(events) if row.get("type") == "task_done"), {})
    summary = str(terminal.get("summary", ""))
    tools = [row for row in events if row.get("type") == "tool"]
    names = [str(row.get("tool_name")) for row in tools]
    reads = [row for row in tools if row.get("tool_name") == "read_file" and row.get("ok") is True]
    unchanged = ((project / "notes.md").read_text(encoding="utf8") == NOTES
                 and {path.name for path in project.iterdir()} == {"notes.md"})
    report: JsonObject = {"case": case, "summary": summary, "stop_reason": terminal.get("stop_reason"),
        "turns": terminal.get("turns"), "tools": [name for name in names],
        "errors": [row for row in tools if row.get("ok") is False],
        "seconds": round(time.monotonic() - started, 3), "project_unchanged": unchanged}
    report["ok"] = (terminal.get("stop_reason") == "done" and bool(reads) and unchanged
        and "FZ-7319" in summary and "清淡" in summary and "锅边糊" in summary
        and not report["errors"] and ("web_search" not in names if case == "local" else
            "web_search" in names and bool({"open_url", "open_result"} & set(names)) and SOURCE in summary))
    save(directory / "summary.json", report)
    return report


def repair_trial(directory: Path, *, necessary: bool) -> JsonObject:
    """One real generation after a seeded rejection, not a simulated agent loop."""
    directory.mkdir(parents=True, exist_ok=False)
    task = TASKS["local"] if necessary else "你帮我上网看看福州有什么好吃的"
    filename = "notes.md" if necessary else "福州美食.md"
    session = TaskSession(TaskPolicy(frozenset({"control", "project.read", "web.read"})),
                          task_kind="chat", task_text=task)
    snapshot = build_turn_snapshot(session)
    rejected = json.dumps({"tool": "read_file", "args": {"path": "D:/fictional/" + filename}}, ensure_ascii=False)
    plan = normalize_turn(decode_text_turn(rejected), snapshot=snapshot)
    if not plan.protocol_error:
        raise AssertionError("The seeded absolute path must be rejected")
    repair = _repair_prompt(plan, snapshot=snapshot, native=False, reply=rejected)
    context = ("Controlled workspace: notes.md exists; its content has not been read." if necessary else
               "Controlled completed open_url result: " + SOURCE
               + "\n鱼丸用鱼肉制成丸皮，包入肉馅。肉燕以肉制燕皮包馅。锅边糊用米浆沿锅边烫熟，适合作早餐。")
    prompt = kernel_prompt_for_session(session, tool_names=snapshot.tool_names, user_task=task,
        contract_text=snapshot.contract_text, context_text=context, request_scoped_contract=True,
        task_guidance=task_guidance_for_policy(session.policy, task_kind="chat")) + "\n\n" + repair
    (directory / "prompt.txt").write_text(prompt, encoding="utf8")
    target = select_target("gemma", 180, "http://127.0.0.1:5001/v1")
    provider = attempts.RecordingProvider(target, directory)
    schema = decode_json_object(text_tool_schema(tools_from_specs(snapshot.model_specs)))
    save(directory / "manifest.json", {"model": target.model, "seeded_error": True, "rejected_call": rejected,
        "error": plan.protocol_error, "entry": "production_prompt_provider_decoder_single_generation",
        "source_mode": "controlled_workspace" if necessary else "controlled_acquired_page", "necessary": necessary})
    started = time.monotonic()
    reply = provider.send(prompt, tool_call_json=True, tool_call_schema=schema, tool_call_contract=snapshot.contract_text)
    (directory / "reply.txt").write_text(reply, encoding="utf8")
    result = normalize_turn(decode_text_turn(reply), snapshot=snapshot)
    control = result.control
    report: JsonObject = {"case": "necessary_repair" if necessary else "unnecessary_repair", "reply": reply,
        "protocol_error": result.protocol_error, "tools": [call.name for call in result.calls],
        "control": control.kind if control is not None else None, "ok": repair_action_ok(result, necessary=necessary),
        "seconds": round(time.monotonic() - started, 3), "seeded_error": True}
    save(directory / "summary.json", report)
    return report


class Arguments(argparse.Namespace):
    run_dir: Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    args = parser.parse_args(namespace=Arguments())
    args.run_dir.mkdir(parents=True, exist_ok=False)
    reports: list[JsonObject] = []
    for case in TASKS:
        reports.append(workspace_trial(args.run_dir / case, case))
        print(json.dumps(reports[-1], ensure_ascii=True), flush=True)
    for necessary in (False, True):
        reports.append(repair_trial(args.run_dir / ("necessary" if necessary else "unnecessary"), necessary=necessary))
        print(json.dumps(reports[-1], ensure_ascii=True), flush=True)
    save(args.run_dir / "summary.json", {"trials": [row for row in reports],
        "ok": all(row["ok"] is True for row in reports)})
    return 0 if all(row["ok"] is True for row in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
