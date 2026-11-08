"""Record unmodified user prompts through production chat with local Gemma.

Unlike diagnostic tasks, these prompts contain no tool or completion hints.
Acquisition is checked mechanically; answer accuracy remains an explicit review.
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
from codey.env_names import NATIVE_TOOLS_ENV
from codey.types.json_value import JsonObject, decode_json_object
from tools import local_model_gate_attempts as attempts
from tools.shell_web_diagnostic_gate import select_target
from tools.shell_web_food_gate import OPEN_TOOLS, opened_bodies

TASKS = {
    "site": "https://api.lihe.chat/  你帮我看看这个网站是干嘛的",
    "food": "你帮我上网看看福州有什么好吃的",
}


def analyze(directory: Path, case: str) -> JsonObject:
    events = [decode_json_object(json.loads(line)) for line in
              (directory / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    terminal = next((row for row in reversed(events) if row.get("type") == "task_done"), {})
    tools = [row for row in events if row.get("type") == "tool"]
    opens = [row for row in tools if row.get("tool_name") in OPEN_TOOLS]
    successful = [row for row in opens if row.get("ok") is True]
    failures = [row for row in opens if row.get("ok") is False]
    searches = [row for row in tools if row.get("tool_name") == "web_search" and row.get("ok") is True]
    bodies = opened_bodies(directory)
    if case == "site":
        attempted = any(str(row.get("path", "")).rstrip("/") == "https://api.lihe.chat" for row in opens)
        acquisition_ok = attempted and terminal.get("stop_reason") in {"done", "blocked"}
    else:
        acquisition_ok = bool(searches and successful and bodies) and terminal.get("stop_reason") == "done"
    return {"scope": "production_ordinary_chat_original_prompt", "case": case,
            "task": TASKS[case], "acquisition_ok": acquisition_ok,
            "stop_reason": terminal.get("stop_reason"), "summary": terminal.get("summary"),
            "searches": searches, "successful_opens": successful, "failed_opens": failures,
            "delivered_page_bodies": bodies,
            "answer_review_required": True,
            "limits": "One original-prompt trial. Acquisition checks do not certify natural-language claims or reliability."}


def run(directory: Path, case: str, timeout: float) -> JsonObject:
    target = select_target("gemma", timeout, "http://127.0.0.1:5001/v1")
    manifest = {"task": TASKS[case], "model": target.model, "case": case,
                "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()}
    (directory / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    def observe(row: dict[str, object]) -> None:
        with (directory / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")

    def connect(provider_id: str, **kwargs: object) -> attempts.RecordingProvider:
        if provider_id != "local":
            raise ValueError("original-prompt probe uses only the selected local model")
        return attempts.RecordingProvider(target, directory)

    with (
        patch.dict(os.environ, {NATIVE_TOOLS_ENV: "1"}),
        attempts.isolate_gate_secondary_models(target),
        attempts.record_api_generations(directory / "provider.jsonl"),
    ):
        run_headless(HeadlessRequest(None, TASKS[case], intent="chat", provider_id="local", max_turns=8,
            state_home=directory / "state", model_selection={"model": target.model, "base_url": target.base_url},
            requested_capabilities=(), project_changes_required=False), emit_jsonl=observe, connect_provider=connect)
    return analyze(directory, case)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=tuple(TASKS), required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args()
    directory = args.run_dir.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    report = run(directory, args.case, args.timeout)
    (directory / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True), flush=True)
    return 0 if report["acquisition_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
