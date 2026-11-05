"""Writer-level A/B for preferred versus flat verification candidates."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from codey.agents.request import AgentRequest
from codey.completion.verification_policy import (
    VerificationCandidate,
    check_covers_selected_candidate,
    discover_verification_candidates,
    render_verification_guidance,
    select_verification_candidate,
)
from codey.operations import project_adapter
from codey.providers import controls as provider_controls
from codey.providers.registry import DEFAULT_PROVIDER_ID, connect_provider, provider_ids
from codey.runtime.observe.events import RunEvent, render_run_event
from codey.workspace.revision import WorkspaceRevisionStore
from tests.manual.coding_current_context_ab import CASES as CONTEXT_CASES
from tests.manual.coding_current_context_ab import CountingProvider, _changed_files, _run_process
from tests.support.kernel_runtime import run_agent

ARMS = ("flat", "preferred")


@dataclass(frozen=True)
class Case:
    name: str
    task: str
    files: dict[str, str]
    expected_changed: tuple[str, ...]
    check_command: tuple[str, ...]
    check_cwd: str = "."


CASES = (
    Case(
        name="pytest-configured",
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
    Case(
        name="unittest-configured-by-layout",
        task="Change only RATE from 5 to 7.",
        files={
            "app.py": "RATE = 5\n\n\ndef current_rate():\n    return RATE\n",
            "test_app.py": (
                "import unittest\n"
                "from app import current_rate\n\n"
                "class AppTests(unittest.TestCase):\n"
                "    def test_current_rate(self):\n"
                "        self.assertEqual(current_rate(), 7)\n"
            ),
        },
        expected_changed=("app.py",),
        check_command=(sys.executable, "-B", "-m", "unittest", "discover"),
    ),
    Case(
        name="pytest-edge-bug",
        task="Fix clamp in limits.py so it clamps values to the inclusive lower/upper bounds. Keep the public API and tests unchanged.",
        files={
            "limits.py": "def clamp(value, lower, upper):\n    return min(lower, max(value, upper))\n",
            "test_limits.py": (
                "from limits import clamp\n\n"
                "def test_clamp():\n"
                "    assert clamp(-2, 0, 10) == 0\n"
                "    assert clamp(4, 0, 10) == 4\n"
                "    assert clamp(12, 0, 10) == 10\n"
                "    assert clamp(0, 0, 10) == 0\n"
                "    assert clamp(10, 0, 10) == 10\n"
                "    assert clamp(-4, -3, -1) == -3\n"
            ),
            "pytest.ini": "[pytest]\n",
        },
        expected_changed=("limits.py",),
        check_command=(sys.executable, "-B", "-m", "pytest", "-q"),
    ),
    Case(
        name="unittest-feature",
        task="Implement is_even(number) in app.py for integers, including zero and negative integers. Keep current_rate and the existing tests unchanged.",
        files={
            "app.py": "def current_rate():\n    return 5\n\ndef is_even(number):\n    raise NotImplementedError\n",
            "test_app.py": (
                "import unittest\nfrom app import current_rate, is_even\n\n"
                "class AppTests(unittest.TestCase):\n"
                "    def test_rate(self):\n        self.assertEqual(current_rate(), 5)\n"
                "    def test_even(self):\n"
                "        for value in (0, 4, -6):\n            self.assertTrue(is_even(value))\n"
                "    def test_odd(self):\n"
                "        for value in (1, 5, -3):\n            self.assertFalse(is_even(value))\n"
            ),
        },
        expected_changed=("app.py",),
        check_command=(sys.executable, "-B", "-m", "unittest", "discover"),
    ),
    *(Case(item.name, item.task, item.files, item.expected_changed, item.check_command, item.check_cwd)
      for item in CONTEXT_CASES if item.name == "monorepo-selected-check"),
)


def _candidate_prefix(text: str) -> str:
    markers = ("Trusted verification candidates after edits:\n",
               "Preferred verification command after edits:\n",
               "Verification candidates (no unique preference):\n")
    offsets = [text.index(marker) for marker in markers if marker in text]
    if not offsets:
        raise ValueError("verification guidance section missing; A/B would not change the prompt")
    return text[:min(offsets)].rstrip()


def _rewrite_candidate_prompt(text: str, candidates: tuple[VerificationCandidate, ...]) -> str:
    return _candidate_prefix(text) + "\n\n" + render_verification_guidance(candidates)


def _flat_candidate_prompt(text: str, candidates: tuple[VerificationCandidate, ...]) -> str:
    lines = [
        f"- {item.command[:240]} (cwd: {item.cwd[:120]})"
        for item in candidates[:5]
    ]
    return _candidate_prefix(text) + "\n\nTrusted verification candidates after edits:\n" + "\n".join(lines)


def _run_arm(provider: CountingProvider, case: Case, arm: str, *, max_turns: int) -> dict[str, Any]:
    events: list[RunEvent] = []
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "project"
        root.mkdir()
        for rel, content in case.files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        candidates = discover_verification_candidates(root)
        selected = select_verification_candidate(candidates, case.expected_changed)
        baseline_check_passed = _run_process(root, case.check_command, case.check_cwd)
        if baseline_check_passed:
            raise ValueError("fixture must fail its independent check before editing")
        original_context = project_adapter._project_context
        if arm != "production":
            def experiment_context(request: AgentRequest) -> str:
                text = original_context(request)
                return (_rewrite_candidate_prompt(text, candidates) if arm == "preferred" else
                        _flat_candidate_prompt(text, candidates))
            project_adapter._project_context = experiment_context
        prompts_before = len(provider.prompts)
        started = time.time()
        try:
            result = run_agent(
                AgentRequest(
                    provider=provider,
                    project=root,
                    task=case.task,
                    workspace_revision_store=WorkspaceRevisionStore(Path(td) / "state"),
                    max_turns=max_turns,
                    on_event=events.append,
                    fresh_chat=True,
                    provider_id=getattr(provider, "id", ""),
                    verification_candidates=candidates,
                    verification_candidate_loader=lambda: discover_verification_candidates(root),
                    coding_context_enabled=False,
                )
            )
        finally:
            project_adapter._project_context = original_context
        elapsed = round(time.time() - started, 3)
        changed = _changed_files(root, case.files)
        independent = _run_process(root, case.check_command, case.check_cwd)
        captured_files = {rel: (root / rel).read_text(encoding="utf-8")
                          for rel in sorted(set(case.files) | set(changed)) if (root / rel).is_file()}
        actual_runs = [event for event in events if event.kind == "tool" and event.call is not None
                       and event.call.name == "run" and event.outcome is not None]
        first_covers_selected = bool(selected and actual_runs and check_covers_selected_candidate(
            selected, str(actual_runs[0].call.args.get("command") or ""),
            str(actual_runs[0].call.args.get("path") or "."), changed, root=root))
    run_commands = [
        str(event.call.args.get("command") or "")
        for event in events
        if event.kind == "tool" and event.call is not None and event.call.name == "run" and event.outcome is not None
    ]
    first_run = run_commands[0] if run_commands else ""
    row: dict[str, Any] = {
        "case": case.name,
        "arm": arm,
        "seconds": elapsed,
        "stop_reason": result.stop_reason,
        "summary": result.summary,
        "turns": result.turns,
        "changed_files": list(changed),
        "expected_changed_files": list(case.expected_changed),
        "independent_check_passed": independent,
        "baseline_check_passed": baseline_check_passed,
        "candidates": [item.command for item in candidates],
        "preferred": selected.command if selected else "",
        "selected_cwd": selected.cwd if selected else "",
        "run_commands": run_commands,
        "run_results": [
            {"command": event.call.args.get("command"), "cwd": event.call.args.get("path", "."),
             "ok": event.outcome.ok, "output": event.outcome.model_text,
             "exit_code": event.outcome.exit_code}
            for event in actual_runs
        ],
        "first_run": first_run,
        "first_run_is_selected": first_covers_selected,
        "files": captured_files,
        "raw_replies": provider.replies[prompts_before:],
        "prompt_chars": sum(len(prompt) for prompt in provider.prompts[prompts_before:]),
        "success": bool(
            result.stop_reason == "done"
            and independent
            and tuple(changed) == tuple(case.expected_changed)
        ),
    }
    if not row["success"]:
        row["event_tail"] = [render_run_event(event) for event in events[-10:]]
        row["prompt_tail"] = provider.prompts[-3:]
    return row


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=provider_ids(), default=DEFAULT_PROVIDER_ID)
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--max-turns", type=int, default=12)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case", action="append", choices=[item.name for item in CASES])
    parser.add_argument("--arm", action="append", choices=(*ARMS, "production"))
    args = parser.parse_args(argv)
    provider_controls.begin_task_context(f"verification-candidate-prompt-ab:{args.provider}")
    provider = CountingProvider(
        connect_provider(args.provider, port=args.port),
        timeout=args.timeout,
        new_chat_timeout=60,
    )
    rows: list[dict[str, Any]] = []
    cases = tuple(item for item in CASES if not args.case or item.name in args.case)
    arms = tuple(args.arm or ARMS)
    try:
        for case in cases:
            for arm in arms:
                try:
                    row = _run_arm(provider, case, arm, max_turns=args.max_turns)
                except Exception as exc:
                    row = {"case": case.name, "arm": arm, "error": f"{type(exc).__name__}: {exc}"}
                rows.append(row)
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps({"probe": "verification_candidate_prompt_ab", "rows": rows},
                                                 ensure_ascii=False, indent=2), encoding="utf-8")
                print(json.dumps({k: v for k, v in row.items() if k not in
                                  {"raw_replies", "files", "prompt_tail"}}, ensure_ascii=False), flush=True)
    finally:
        provider.close()
        provider_controls.end_task_context()
    summary = {
        "arms": {
            arm: {
                "count": sum(row.get("arm") == arm for row in rows),
                "errors": sum(row.get("arm") == arm and "error" in row for row in rows),
                "success": sum(row.get("arm") == arm and row.get("success") is True for row in rows),
                "first_run_is_selected": sum(row.get("arm") == arm and row.get("first_run_is_selected") is True for row in rows),
            }
            for arm in arms
        }
    }
    payload = {
        "probe": "verification_candidate_prompt_ab", "provider": args.provider,
        "metadata": {"model": getattr(provider, "model", "unknown"),
                     "native_tools": getattr(provider, "native_tools", False),
                     "api_protocol": getattr(provider, "api_protocol", "unknown"),
                     "timeout": args.timeout, "max_turns": args.max_turns,
                     "arms_in_order": arms, "python": sys.version,
                     "prompt_chars_semantics": "recorded logical message text; excludes serialized history/tool schemas; includes local terminal acknowledgment; not wire tokens or HTTP requests"},
        "rows": rows, "summary": summary,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"report: {args.output}")
    return 0 if rows and all(row.get("success") and row.get("first_run_is_selected") for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
