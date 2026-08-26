"""Live A/B probe for Coding current context.

This probe compares the production coding loop with the production
``coding_context_enabled`` switch disabled and enabled. Both arms run the real
Agent loop on temporary projects and execute real local read/edit/run tools.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from codey.agents import runner as agent
from codey.providers import controls as provider_controls
from codey.runtime.events import RunEvent, render_run_event
from codey.providers.registry import DEFAULT_PROVIDER_ID, connect_provider, provider_ids
from codey.completion.verification_policy import (
    VerificationCandidate,
    check_covers_selected_candidate,
    discover_verification_candidates,
    select_verification_candidate,
)
from tests.manual.project_task_context import render_production_project_map


ARMS = ("baseline", "context")
RESULTS_DIR = Path(__file__).resolve().parent / "results"
IGNORED_CHANGED_PARTS = frozenset({
    ".pytest_cache",
    "__pycache__",
    "node_modules",
})


@dataclass(frozen=True)
class ContextCase:
    name: str
    task: str
    files: dict[str, str]
    expected_changed: tuple[str, ...]
    check_command: tuple[str, ...]
    check_cwd: str = "."


CASES = (
    ContextCase(
        name="edit-then-verify",
        task="Change only RATE from 5 to 7.",
        files={
            "app.py": "RATE = 5\n\n\ndef current_rate():\n    return RATE\n",
            "test_app.py": (
                "from app import current_rate\n\n"
                "def test_current_rate():\n"
                "    assert current_rate() == 7\n"
            ),
            "pytest.ini": "[pytest]\n",
        },
        expected_changed=("app.py",),
        check_command=(sys.executable, "-B", "-m", "pytest", "-q"),
    ),
    ContextCase(
        name="avoid-duplicate-read",
        task=(
            "In config.py, change only MAX_PARALLEL_CALLS from 4 to 3. "
            "Do not change MAX_ACCIDENTAL_TOOL_CALLS."
        ),
        files={
            "config.py": (
                "MAX_ACCIDENTAL_TOOL_CALLS = 8\n"
                "MAX_PARALLEL_CALLS = 4\n\n"
                "def parallel_ok(count):\n"
                "    return count <= MAX_PARALLEL_CALLS\n"
            ),
            "test_config.py": (
                "from config import MAX_ACCIDENTAL_TOOL_CALLS, parallel_ok\n\n"
                "def test_parallel_limit():\n"
                "    assert parallel_ok(3)\n"
                "    assert not parallel_ok(4)\n\n"
                "def test_accidental_unchanged():\n"
                "    assert MAX_ACCIDENTAL_TOOL_CALLS == 8\n"
            ),
            "pytest.ini": "[pytest]\n",
        },
        expected_changed=("config.py",),
        check_command=(sys.executable, "-B", "-m", "pytest", "-q"),
    ),
    ContextCase(
        name="monorepo-selected-check",
        task="In frontend, change only the default greeting from hello to hi.",
        files={
            "pytest.ini": "[pytest]\n",
            "test_root.py": "def test_root():\n    assert True\n",
            "frontend/src/app.js": "export function greeting() { return 'hello'; }\n",
            "frontend/test.js": (
                "import { greeting } from './src/app.js';\n"
                "if (greeting() !== 'hi') process.exit(1);\n"
            ),
            "frontend/package.json": json.dumps({
                "type": "module",
                "scripts": {"test": "node test.js"},
            }, indent=2) + "\n",
        },
        expected_changed=("frontend/src/app.js",),
        check_command=("npm", "test"),
        check_cwd="frontend",
    ),
)


def _write_case(root: Path, case: ContextCase) -> None:
    for rel, content in case.files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _changed_files(root: Path, original: dict[str, str]) -> tuple[str, ...]:
    changed: list[str] = []
    for rel, before in original.items():
        if _ignored_changed_path(rel):
            continue
        path = root / rel
        if not path.is_file() or path.read_text(encoding="utf-8") != before:
            changed.append(rel)
    for path in root.rglob("*"):
        if path.is_file():
            rel = path.relative_to(root).as_posix()
            if rel not in original and not _ignored_changed_path(rel):
                changed.append(rel)
    return tuple(sorted(dict.fromkeys(changed)))


def _ignored_changed_path(rel: str) -> bool:
    parts = Path(str(rel).replace("\\", "/")).parts
    return any(part in IGNORED_CHANGED_PARTS for part in parts)


def _run_process(root: Path, command: tuple[str, ...], cwd: str = ".") -> bool:
    if shutil.which(command[0]) is None:
        return False
    try:
        completed = subprocess.run(
            command,
            cwd=root / cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=45,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _tool_events(events: list[RunEvent]) -> list[RunEvent]:
    return [event for event in events if event.kind == "tool" and event.call is not None]


def _latest_changed_edit_index(events: list[RunEvent]) -> int:
    latest = -1
    for index, event in enumerate(_tool_events(events)):
        if (
            event.call is not None
            and event.call.name == "edit"
            and event.outcome is not None
            and event.outcome.changed
        ):
            latest = index
    return latest


def _selected_check_passed_after_edit(
    events: list[RunEvent],
    selected: VerificationCandidate | None,
    changed_files: tuple[str, ...],
) -> tuple[bool, int]:
    if selected is None:
        return False, 0
    latest_edit = _latest_changed_edit_index(events)
    selected_runs = 0
    selected_passed = False
    for index, event in enumerate(_tool_events(events)):
        if index <= latest_edit or event.call is None or event.call.name != "run":
            continue
        if check_covers_selected_candidate(
            selected,
            str(event.call.args.get("command") or ""),
            str(event.call.args.get("path") or "."),
            changed_files,
        ):
            selected_runs += 1
            selected_passed = selected_passed or bool(
                event.outcome is not None and event.outcome.ok
            )
    return selected_passed, selected_runs


def _duplicate_read_count(events: list[RunEvent]) -> int:
    reads: list[str] = []
    for event in _tool_events(events):
        if event.call is not None and event.call.name == "read":
            reads.append(str(event.call.args.get("path") or ""))
    return len(reads) - len(set(reads))


def _default_verification_reminders(prompts: list[str]) -> int:
    return sum("trusted local check" in prompt for prompt in prompts)


def _protocol_errors(events: list[RunEvent]) -> int:
    return sum(
        1
        for event in events
        if event.kind == "status" and "rejected invalid tool request" in event.message
    )


class CountingProvider:
    def __init__(self, provider, *, timeout: float, new_chat_timeout: float) -> None:
        self.provider = provider
        self.timeout = timeout
        self.new_chat_timeout = new_chat_timeout
        self.prompts: list[str] = []
        self.replies: list[str] = []
        self.name = getattr(provider, "name", "provider")
        self.id = getattr(provider, "id", "")

    def new_chat(self, timeout: float | None = None) -> object:
        return self.provider.new_chat(timeout=timeout or self.new_chat_timeout)

    def send(self, text: str, timeout: float | None = None) -> str:
        self.prompts.append(text)
        reply = self.provider.send(text, timeout=timeout or self.timeout)
        self.replies.append(reply)
        return reply

    def close(self) -> None:
        self.provider.close()


class _ScriptedProvider:
    id = "self-test"
    name = "Self Test"

    def __init__(self, *replies: str) -> None:
        self.prompts: list[str] = []
        self.replies = list(replies)

    def new_chat(self, timeout: float | None = None) -> object:
        return object()

    def send(self, text: str) -> str:
        self.prompts.append(text)
        if not self.replies:
            raise AssertionError("scripted provider ran out of replies")
        return self.replies.pop(0)

    def close(self) -> None:
        return None


def _run_arm(
    provider,
    case: ContextCase,
    arm: str,
    *,
    max_turns: int,
) -> dict[str, Any]:
    events: list[RunEvent] = []
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _write_case(root, case)
        candidates = discover_verification_candidates(root)
        selected = select_verification_candidate(candidates, case.expected_changed)
        prompts_before = len(provider.prompts)
        started = time.time()
        result = agent.run(
            provider,
            root,
            case.task,
            max_turns=max_turns,
            on_event=events.append,
            fresh_chat=True,
            provider_id=getattr(provider, "id", ""),
            project_map=render_production_project_map(root, task=case.task),
            verification_candidates=candidates,
            verification_candidate_loader=lambda: discover_verification_candidates(root),
            coding_context_enabled=arm == "context",
        )
        elapsed = round(time.time() - started, 3)
        changed = _changed_files(root, case.files)
        independent_check = _run_process(root, case.check_command, case.check_cwd)

    prompts = provider.prompts[prompts_before:]
    selected_passed, selected_run_count = _selected_check_passed_after_edit(
        events,
        selected,
        changed or case.expected_changed,
    )
    report: dict[str, Any] = {
        "case": case.name,
        "arm": arm,
        "seconds": elapsed,
        "stop_reason": result.stop_reason,
        "turns": result.turns,
        "tool_calls": len(_tool_events(events)),
        "duplicate_reads": _duplicate_read_count(events),
        "default_verification_reminders": _default_verification_reminders(prompts),
        "protocol_errors": _protocol_errors(events),
        "changed_files": list(changed),
        "expected_changed_files": list(case.expected_changed),
        "independent_check_passed": independent_check,
        "selected_candidate": vars(selected) if selected else None,
        "selected_check_passed_after_edit": selected_passed,
        "selected_run_count_after_edit": selected_run_count,
        "context_prompt_count": sum("Coding current local context:" in prompt for prompt in prompts),
        "sent_chars": sum(len(prompt) for prompt in prompts),
        "success": bool(
            result.stop_reason == "done"
            and independent_check
            and tuple(changed) == tuple(case.expected_changed)
        ),
    }
    if not report["success"]:
        report["event_tail"] = [render_run_event(event) for event in events[-10:]]
    return report


def _summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {"arms": {}}
    for arm in ARMS:
        arm_rows = [row for row in rows if row.get("arm") == arm and "error" not in row]
        summary["arms"][arm] = {
            "count": len(arm_rows),
            "success": sum(1 for row in arm_rows if row.get("success")),
            "selected_check_passed_after_edit": sum(
                1 for row in arm_rows if row.get("selected_check_passed_after_edit")
            ),
            "default_verification_reminders": sum(
                int(row.get("default_verification_reminders") or 0) for row in arm_rows
            ),
            "duplicate_reads": sum(int(row.get("duplicate_reads") or 0) for row in arm_rows),
            "protocol_errors": sum(int(row.get("protocol_errors") or 0) for row in arm_rows),
            "turns": sum(int(row.get("turns") or 0) for row in arm_rows),
            "tool_calls": sum(int(row.get("tool_calls") or 0) for row in arm_rows),
            "sent_chars": sum(int(row.get("sent_chars") or 0) for row in arm_rows),
        }
    baseline = summary["arms"].get("baseline", {})
    context = summary["arms"].get("context", {})
    if baseline.get("count") and context.get("count"):
        summary["context_delta_vs_baseline"] = {
            key: int(context.get(key) or 0) - int(baseline.get(key) or 0)
            for key in (
                "success",
                "selected_check_passed_after_edit",
                "default_verification_reminders",
                "duplicate_reads",
                "protocol_errors",
                "turns",
                "tool_calls",
                "sent_chars",
            )
        }
    return summary


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _default_output(provider_id: str) -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%S")
    return RESULTS_DIR / f"coding_current_context_ab-{provider_id}-{stamp}.json"


def _select_cases(names: list[str]) -> tuple[ContextCase, ...]:
    index = {case.name: case for case in CASES}
    if not names:
        return CASES
    selected: list[ContextCase] = []
    seen: set[str] = set()
    for value in names:
        for item in str(value or "").split(","):
            name = item.strip()
            if name and name not in seen:
                selected.append(index[name])
                seen.add(name)
    return tuple(selected)


def _select_arms(values: list[str]) -> tuple[str, ...]:
    if not values:
        return ARMS
    selected: list[str] = []
    for value in values:
        for item in str(value or "").split(","):
            arm = item.strip()
            if arm:
                if arm not in ARMS:
                    raise SystemExit(f"unknown arm: {arm}")
                selected.append(arm)
    return tuple(dict.fromkeys(selected))


def run_live(
    *,
    provider_id: str,
    port: int,
    timeout: float,
    new_chat_timeout: float,
    cases: tuple[ContextCase, ...],
    arms: tuple[str, ...],
    max_turns: int,
    output: Path,
    keep_open: bool,
) -> int:
    payload: dict[str, Any] = {
        "probe": "coding_current_context_ab",
        "provider": provider_id,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "cases": [case.name for case in cases],
        "arms": list(arms),
        "rows": [],
        "summary": {},
    }
    _atomic_write_json(output, payload)
    provider_controls.begin_task_context(f"coding-current-context-ab:{provider_id}")
    provider = None
    try:
        provider = CountingProvider(
            connect_provider(provider_id, port=port),
            timeout=timeout,
            new_chat_timeout=new_chat_timeout,
        )
        for case in cases:
            for arm in arms:
                try:
                    row = _run_arm(provider, case, arm, max_turns=max_turns)
                except Exception as exc:
                    row = {
                        "case": case.name,
                        "arm": arm,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                payload["rows"].append(row)
                payload["summary"] = _summarize(payload["rows"])
                _atomic_write_json(output, payload)
                print(json.dumps(row, ensure_ascii=False), flush=True)
        payload["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        payload["summary"] = _summarize(payload["rows"])
        _atomic_write_json(output, payload)
        print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))
        print(f"report: {output}")
        return 0 if all("error" not in row for row in payload["rows"]) else 1
    finally:
        try:
            if provider is not None and not keep_open:
                provider.close()
        finally:
            provider_controls.end_task_context()


def _self_test() -> None:
    read_case = ContextCase(
        name="read-only-self-test",
        task="Read app.py.",
        files={"app.py": "VALUE = 1\n"},
        expected_changed=(),
        check_command=(sys.executable, "-c", "pass"),
    )
    baseline = _run_arm(
        _ScriptedProvider(
            '{"tool":"read_file","args":{"path":"app.py"}}',
            '{"tool":"done","args":{"summary":"read app.py"}}',
        ),
        read_case,
        "baseline",
        max_turns=4,
    )
    context = _run_arm(
        _ScriptedProvider(
            '{"tool":"read_file","args":{"path":"app.py"}}',
            '{"tool":"done","args":{"summary":"read app.py"}}',
        ),
        read_case,
        "context",
        max_turns=4,
    )
    assert baseline["success"]
    assert context["success"]
    assert baseline["context_prompt_count"] == 0
    assert context["context_prompt_count"] == 1
    print("self-test ok")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Live A/B for Coding current context.")
    parser.add_argument("--provider", choices=provider_ids(), default=DEFAULT_PROVIDER_ID)
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--new-chat-timeout", type=float, default=45.0)
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--arm", action="append", default=[])
    parser.add_argument("--max-turns", type=int, default=12)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--keep-open", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if args.self_test:
        _self_test()
        return 0
    cases = _select_cases(args.case)
    arms = _select_arms(args.arm)
    output = args.output or _default_output(args.provider)
    return run_live(
        provider_id=args.provider,
        port=args.port,
        timeout=args.timeout,
        new_chat_timeout=args.new_chat_timeout,
        cases=cases,
        arms=arms,
        max_turns=args.max_turns,
        output=output,
        keep_open=args.keep_open,
    )


if __name__ == "__main__":
    raise SystemExit(main())