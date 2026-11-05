"""Single-model A/B for optional planning and pre-audit stages.

This is deliberately an evidence harness rather than a production feature.  It
uses the same KoboldCpp provider for the optional stage and the Writer, so it
can measure whether an extra request helps the coding result without claiming
that a same-model answer is an independent advisor opinion.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from codey.agents.request import AgentRequest
from codey.completion.verification_policy import discover_verification_candidates
from codey.providers import controls as provider_controls
from codey.providers.registry import DEFAULT_PROVIDER_ID, connect_provider, provider_ids
from codey.runtime.observe.events import RunEvent, render_run_event
from codey.workspace.revision import WorkspaceRevisionStore
from tests.manual.coding_current_context_ab import (
    CountingProvider,
    _changed_files,
    _run_process,
    _select_cases,
)
from tests.manual.project_task_context import render_production_project_map
from tests.support.kernel_runtime import run_agent

STAGES = ("none", "plan", "audit")


def _stage_prompt(stage: str, case, root: Path) -> str:
    files = []
    for rel, content in case.files.items():
        files.append(f"--- {rel} ---\n{content[:3000]}")
    if stage == "plan":
        instruction = (
            "Give a bounded implementation plan for the coding task. "
            "List files to inspect, intended edits, and one verification command. "
            "Do not edit files and do not write the final answer."
        )
    else:
        instruction = (
            "Perform a bounded read-only audit for the coding task. "
            "Identify concrete risks, relevant files, and a verification command. "
            "Do not edit files and do not write the final answer."
        )
    return "\n".join((instruction, f"Project root: {root}", "Task:", case.task, *files))


def _run_case(provider: CountingProvider, case, stage: str, max_turns: int) -> dict[str, Any]:
    events: list[RunEvent] = []
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "project"
        root.mkdir()
        for rel, content in case.files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        before = len(provider.prompts)
        stage_reply = ""
        stage_error = ""
        started = time.time()
        try:
            if stage != "none":
                provider.new_chat()
                stage_reply = provider.send(_stage_prompt(stage, case, root), timeout=120)
            provider.new_chat()
            task = case.task
            if stage_reply.strip():
                task += f"\n\nPrivate {stage} notes (use only when supported by files):\n{stage_reply[:6000]}"
            candidates = discover_verification_candidates(root)
            result = run_agent(
                AgentRequest(
                    provider=provider,
                    project=root,
                    task=task,
                    workspace_revision_store=WorkspaceRevisionStore(Path(td) / "state"),
                    max_turns=max_turns,
                    on_event=events.append,
                    fresh_chat=True,
                    provider_id=getattr(provider, "id", ""),
                    project_map=render_production_project_map(root, task=case.task),
                    verification_candidates=candidates,
                    verification_candidate_loader=lambda: discover_verification_candidates(root),
                    coding_context_enabled=False,
                )
            )
        except Exception as exc:
            stage_error = f"{type(exc).__name__}: {exc}"
            result = None
        changed = _changed_files(root, case.files)
        check = _run_process(root, case.check_command, case.check_cwd)
    prompts = provider.prompts[before:]
    row: dict[str, Any] = {
        "case": case.name,
        "stage": stage,
        "seconds": round(time.time() - started, 3),
        "stage_reply_chars": len(stage_reply),
        "writer_prompt_chars": sum(len(item) for item in prompts[1:] if stage != "none"),
        "total_prompt_chars": sum(len(item) for item in prompts),
        "stage_request_count": 0 if stage == "none" else 1,
        "writer_request_count": max(0, len(prompts) - (0 if stage == "none" else 1)),
        "changed_files": list(changed),
        "expected_changed_files": list(case.expected_changed),
        "independent_check_passed": check,
        "success": bool(
            result is not None
            and result.stop_reason == "done"
            and check
            and tuple(changed) == tuple(case.expected_changed)
        ),
    }
    if result is not None:
        row.update({"stop_reason": result.stop_reason, "turns": result.turns})
    if stage_error:
        row["error"] = stage_error
    if not row["success"] and events:
        row["event_tail"] = [render_run_event(event) for event in events[-8:]]
    return row


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=provider_ids(), default=DEFAULT_PROVIDER_ID)
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--stage", choices=STAGES, default="none")
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--max-turns", type=int, default=10)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    cases = _select_cases(args.case)
    provider_controls.begin_task_context(f"single-model-stage-ab:{args.provider}:{args.stage}")
    provider = CountingProvider(connect_provider(args.provider, port=args.port), timeout=120, new_chat_timeout=45)
    rows = []
    try:
        for case in cases:
            row = _run_case(provider, case, args.stage, args.max_turns)
            rows.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
    finally:
        provider.close()
        provider_controls.end_task_context()
    payload = {
        "probe": "single_model_stage_ab",
        "provider": args.provider,
        "stage": args.stage,
        "rows": rows,
        "summary": {
            "count": len(rows),
            "success": sum(bool(row.get("success")) for row in rows),
            "total_prompt_chars": sum(int(row.get("total_prompt_chars") or 0) for row in rows),
            "stage_reply_chars": sum(int(row.get("stage_reply_chars") or 0) for row in rows),
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"report: {args.output}")
    return 0 if all("error" not in row for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())

