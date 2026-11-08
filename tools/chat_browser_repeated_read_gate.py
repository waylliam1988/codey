"""Read one live source in two production Chat tasks sharing an artifact ledger.

This focused probe checks body delivery and artifact ownership, not search quality.
The original-prompt search gate remains independent and keeps its own failures.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.durable import SQLiteDurableStore
from codey.env_names import NATIVE_TOOLS_ENV
from codey.types.json_value import JsonObject, decode_json_object
from tools import local_model_gate_attempts as attempts
from tools.shell_web_diagnostic_gate import select_target
from tools.shell_web_food_gate import OPEN_TOOLS, opened_bodies

SOURCE_URL = "https://chinatraveljetso.com/%e7%a6%8f%e5%b7%9e%e8%87%aa%e7%94%b1%e8%a1%8c/"


def _read_task(target: attempts.GateTarget, task: str, turn_dir: Path, state_home: Path,
               session_id: str) -> tuple[JsonObject, set[str], set[str]]:
    events: list[JsonObject] = []

    def observe(row: dict[str, object]) -> None:
        events.append(decode_json_object(row))
        with (turn_dir / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")

    def connect(provider_id: str, **kwargs: object) -> attempts.RecordingProvider:
        if provider_id != "local":
            raise ValueError("repeated-read probe uses only the selected local model")
        return attempts.RecordingProvider(target, turn_dir)

    with (
        patch.dict(os.environ, {NATIVE_TOOLS_ENV: "1"}),
        attempts.isolate_gate_secondary_models(target),
        attempts.record_api_generations(turn_dir / "provider.jsonl"),
    ):
        run_headless(HeadlessRequest(None, task, intent="chat", provider_id="local", max_turns=8,
            state_home=state_home, session_id=session_id,
            model_selection={"model": target.model, "base_url": target.base_url},
            requested_capabilities=(), project_changes_required=False), emit_jsonl=observe, connect_provider=connect)
    terminal = next((row for row in reversed(events) if row.get("type") == "task_done"), {})
    task_id = str(terminal.get("run_id", ""))
    store = SQLiteDurableStore(state_home / "durable.sqlite3")
    try:
        artifacts = store.artifacts_for_task(task_id)
    finally:
        store.close()
    handles = {record.handle for record in artifacts}
    text_outputs = [record for record in artifacts if record.handle.startswith("out_")]
    output_hashes = {record.sha256 for record in text_outputs}
    successful_opens = [row for row in events if row.get("type") == "tool"
                        and row.get("tool_name") in OPEN_TOOLS and row.get("ok") is True
                        and row.get("path") == SOURCE_URL]
    bodies = opened_bodies(turn_dir)
    source_delivered = any(f"URL: {SOURCE_URL}" in body for body in bodies)
    owners_ok = bool(artifacts) and all(record.owner_task_id == task_id for record in artifacts)
    report: JsonObject = {"run_id": task_id, "stop_reason": terminal.get("stop_reason"),
        "summary": terminal.get("summary"), "successful_opens": [row for row in successful_opens],
        "delivered_body_count": len(bodies), "artifact_handles": [handle for handle in sorted(handles)],
        "text_output_sha256": [digest for digest in sorted(output_hashes)], "artifact_owners_match": owners_ok,
        "ok": terminal.get("stop_reason") == "done" and bool(successful_opens and text_outputs)
        and source_delivered and owners_ok}
    (turn_dir / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report, handles, output_hashes


def run(directory: Path, timeout: float) -> JsonObject:
    target = select_target("gemma", timeout, "http://127.0.0.1:5001/v1")
    task = f"打开 {SOURCE_URL} 阅读正文，简短总结文中提及的福州美食，并附实际来源链接。"
    manifest = {"task": task, "model": target.model, "temperature": target.temperature,
                "max_turns": 8, "timeout": timeout,
                "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()}
    (directory / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    reports: list[JsonObject] = []
    owned_handles: list[set[str]] = []
    output_hashes: list[set[str]] = []
    for number in (1, 2):
        turn_dir = directory / f"task-{number}"
        turn_dir.mkdir()
        report, handles, hashes = _read_task(target, task, turn_dir, directory / "state", f"repeated-read-{number}")
        reports.append(report)
        owned_handles.append(handles)
        output_hashes.append(hashes)
    disjoint = owned_handles[0].isdisjoint(owned_handles[1])
    same_output = bool(output_hashes[0] & output_hashes[1])
    return {"scope": "production_chat_repeated_live_source", "tasks": [row for row in reports],
            "artifact_handles_disjoint": disjoint, "identical_text_output_registered_twice": same_output,
            "ok": disjoint and same_output and all(row["ok"] is True for row in reports),
            "answer_review_required": True,
            "limits": "Two explicit URL tasks; search relevance and original-prompt reading behavior are separate checks."}


class GateArguments(argparse.Namespace):
    run_dir: Path
    timeout: float


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=600)
    args = parser.parse_args(namespace=GateArguments())
    directory = args.run_dir.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    report = run(directory, args.timeout)
    (directory / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True), flush=True)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
