"""Real native Writer ablations with external quality checks and wire accounting."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from codey.agents.consensus import render_owner_draft_prompt
from codey.agents.request import AgentRequest
from codey.completion.verification_policy import discover_verification_candidates
from codey.operations.task_context import ProjectTaskContextBuilder
from codey.policies.task_policy import TaskPolicy
from codey.providers import controls
from codey.providers.registry import connect_provider
from codey.workspace.change_brief import new_project_change_brief, project_audit_change_brief
from codey.workspace.revision import WorkspaceRevisionStore
from tests.manual.coding_current_context_ab import CountingProvider, _changed_files
from tests.manual.coding_pipeline_cases import BY_NAME, CASES, CodingCase
from tests.support.kernel_runtime import run_agent, run_project_audit_advisor

ARMS = ("baseline", "map", "context", "plan", "audit")


def source_metadata():
    root = Path(__file__).resolve().parents[2]
    paths = ("codey/operations/kernel_context.py", "codey/operations/completion_gate.py",
             "codey/completion/verification_policy.py", "tests/manual/coding_pipeline_value_ab.py",
             "tests/manual/coding_pipeline_cases.py")
    return {"source_sha256": {path: hashlib.sha256((root / path).read_bytes()).hexdigest() for path in paths},
            "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()}


class EvidenceProvider(CountingProvider):
    """Preserve native capabilities and observe actual HTTP payloads/usage."""

    def __init__(self, provider, *, timeout=600):
        super().__init__(provider, timeout=timeout, new_chat_timeout=60)
        self.wire: list[dict[str, Any]] = []
        self.usage: list[dict[str, Any]] = []
        self.requests: list[dict[str, Any]] = []
        original = provider._observe_http_attempt

        def observe(**kwargs):
            original(**kwargs)
            data = kwargs["data"]
            row = {key: value for key, value in kwargs.items() if key != "data"}
            row["sha256"] = hashlib.sha256(data).hexdigest()
            row["bytes"] = len(data)
            self.wire.append(row)
            if row["phase"] == "request":
                self.requests.append(json.loads(data))

        provider._observe_http_attempt = observe
        self.bind_usage("local", lambda _record: None)

    def bind_usage(self, connection_id, sink):
        def record(item):
            self.usage.append(item.to_payload())
            sink(item)
        self.provider.bind_usage(connection_id, record)

    def mark(self):
        return len(self.prompts), len(self.replies), len(self.wire), len(self.usage), len(self.requests)

    def evidence(self, mark):
        prompt, reply, wire, usage, request = mark
        records = self.usage[usage:]
        def total_known(key):
            values = [row["usage"].get(key) for row in records]
            return sum(values) if values and all(isinstance(value, int) for value in values) else None
        contexts = [row["context"].get("value") for row in records]
        request_rows = self.requests[request:]
        return {
            "raw_replies": self.replies[reply:], "logical_prompts": self.prompts[prompt:],
            "wire": self.wire[wire:], "usage": records, "requests": self.requests[request:],
            "http_requests": sum(row["phase"] == "request" for row in self.wire[wire:]),
            "reported_input_tokens": total_known("input_tokens"),
            "reported_output_tokens": total_known("output_tokens"),
            "input_context_tokens": sum(contexts) if contexts and all(isinstance(value, int) for value in contexts) else None,
            "context_count_methods": sorted({row["context"]["method"] for row in records}),
            "coding_context_request_count": sum("Coding current local context:" in json.dumps(row) for row in request_rows),
        }


def write_files(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def grade(root: Path, case: CodingCase) -> dict[str, Any]:
    rows = []
    for name, code in case.checks:
        try:
            result = subprocess.run([sys.executable, "-B", "-c", code], cwd=root,
                                    capture_output=True, text=True, timeout=30)
            rows.append({"name": name, "ok": result.returncode == 0,
                         "output": (result.stdout + result.stderr)[-3000:]})
        except subprocess.TimeoutExpired:
            rows.append({"name": name, "ok": False, "output": "independent check timed out"})
    changed = _changed_files(root, case.files)
    scope_ok = set(changed) <= set(case.allowed) and set(case.required) <= set(changed)
    return {"quality": rows, "quality_passed": all(row["ok"] for row in rows),
            "quality_score": sum(row["ok"] for row in rows), "scope_ok": scope_ok,
            "changed_files": changed,
            "files": {path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
                      for path in root.rglob("*") if path.is_file()
                      and not any(part in {"__pycache__", ".pytest_cache"} for part in path.parts)}}


def writer(provider, case, root, state, *, task=None, project_map="", facts="", context=False,
           max_turns=24, tracker=None, changed=(), successful_checks=(), events_out=None,
           candidates=None, candidate_loader=None, research=""):
    events = [] if events_out is None else events_out
    candidates = discover_verification_candidates(root) if candidates is None else candidates
    result = run_agent(AgentRequest(
        provider=provider, project=root, task=task or case.task,
        max_turns=max_turns, on_event=events.append, fresh_chat=True, provider_id="local",
        run_id=f"ab-{time.time_ns()}", session_id="pipeline-value-ab",
        workspace_revision_store=WorkspaceRevisionStore(state), change_tracker=tracker,
        verification_candidates=candidates,
        verification_candidate_loader=candidate_loader or (lambda: discover_verification_candidates(root)),
        verification_changed_files=changed, verification_successful_checks=successful_checks,
        project_map=project_map, project_facts=facts, research_context=research, coding_context_enabled=context,
    ))
    return result, events


def event_rows(events):
    return [{"kind": event.kind, "turn": event.turn, "message": event.message,
             "call": {"name": event.call.name, "args": event.call.args} if event.call else None,
             "outcome": {"ok": event.outcome.ok, "output": event.outcome.model_text,
                         "exit_code": event.outcome.exit_code, "changed": event.outcome.changed}
             if event.outcome else None} for event in events]


def run_arm(provider, case, arm, *, max_turns=24):
    mark = provider.mark()
    start = time.monotonic()
    row: dict[str, Any] = {"case": case.name, "arm": arm}
    with tempfile.TemporaryDirectory() as temporary:
        root, state = Path(temporary) / "project", Path(temporary) / "state"
        root.mkdir()
        write_files(root, case.files)
        row["initial_quality_score"] = grade(root, case)["quality_score"]
        task = case.task
        events = []
        try:
            context = ProjectTaskContextBuilder().build(project=root, task=task,
                session_id="pipeline-value-ab", run_id="prepare", continue_task=False,
                provider_session_changed=False)
            if arm == "plan":
                if case.files:
                    raise ValueError("production new-project planning applies only to empty projects")
                provider.new_chat()
                plan = provider.send(render_owner_draft_prompt(task=task, plan=True))
                row["stage_output"] = plan
                row["stage_usable"] = bool(plan.strip())
                task = new_project_change_brief(task, plan).apply_to_task(task)
            elif arm == "audit":
                if not case.files:
                    raise ValueError("pre-audit applies only to existing projects")
                provider.new_chat()
                before = _changed_files(root, case.files)
                audit = run_project_audit_advisor(provider, root, task,
                    parent_policy=TaskPolicy(grants=frozenset({"control", "project.read", "project.write", "project.verify"})),
                    context=context.project_map, advisor_id="local")
                row["stage_output"] = audit
                row["stage_usable"] = bool(audit.strip())
                assert _changed_files(root, case.files) == before, "read-only audit modified files"
                if audit:
                    task = project_audit_change_brief(task, (audit,)).apply_to_task(task)
            result, events = writer(provider, case, root, state, task=task, max_turns=max_turns,
                                    project_map=context.project_map if arm == "map" else "",
                                    context=arm == "context", events_out=events)
            row.update(stop_reason=result.stop_reason, summary=result.summary, turns=result.turns,
                       checks_passed=result.checks_passed, events=event_rows(events))
        except Exception as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"
            row["events"] = event_rows(events)
        row.update(grade(root, case))
    row["seconds"] = round(time.monotonic() - start, 3)
    row["success"] = row.get("stop_reason") == "done" and row["quality_passed"] and row["scope_ok"]
    row.update(provider.evidence(mark))
    return row


def save(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", action="append", choices=tuple(BY_NAME))
    parser.add_argument("--arm", action="append", choices=ARMS)
    parser.add_argument("--max-turns", type=int, default=24)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    controls.begin_task_context("coding-pipeline-value-ab")
    provider = EvidenceProvider(connect_provider("local"))
    cases = tuple(BY_NAME[name] for name in args.case) if args.case else CASES
    arms = args.arm or ("baseline", "map", "context")
    payload = {"model": provider.model, "native_tools": provider.native_tools,
               "api_protocol": provider.api_protocol, "max_turns": args.max_turns,
               "arms": arms, "rows": [], **source_metadata()}
    try:
        for case in cases:
            for arm in arms:
                row = run_arm(provider, case, arm, max_turns=args.max_turns)
                payload["rows"].append(row)
                save(args.output, payload)
                print(json.dumps({key: row.get(key) for key in ("case", "arm", "success", "quality_score",
                    "scope_ok", "stop_reason", "turns", "http_requests", "input_context_tokens", "coding_context_request_count",
                    "seconds", "error")}, ensure_ascii=False), flush=True)
    finally:
        provider.close()
        controls.end_task_context()
    return 0 if payload["rows"] and all(row["success"] for row in payload["rows"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
