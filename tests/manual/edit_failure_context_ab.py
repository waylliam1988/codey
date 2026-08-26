"""Live A/B for bounded context in failed replacement feedback.

This probe does not change production behavior. It simulates a file becoming
stale after the model reads it, then compares the current generic edit error
with a bounded current-file excerpt appended to that error.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from codey.toolchain import runtime as tool_runtime
from codey.agents import runner as agent
from codey.providers import controls as provider_controls
from codey.agents.tools import AgentToolFns
from codey.runtime.events import RunEvent, render_run_event
from codey.providers.registry import connect_provider, provider_ids
from tests.manual.project_task_context import render_production_project_map


ARMS = ("baseline", "context")
TARGET = "config.py"
ORIGINAL_LINE = "    timeout = settings.get('request_timeout', 10)\n"
STALE_BLOCK = (
    "    timeout = settings.get(\n"
    "        'request_timeout',\n"
    "        10,\n"
    "    ) # seconds\n"
)
EXPECTED_LINE = "        30,\n"

FILES = {
    TARGET: (
        "def load_timeout(settings):\n"
        "    retries = settings.get('request_retries', 10)\n"
        f"{ORIGINAL_LINE}"
        "    return timeout, retries\n"
    ),
    "test_config.py": (
        "import unittest\n"
        "from config import load_timeout\n\n"
        "class ConfigTests(unittest.TestCase):\n"
        "    def test_request_timeout_default_is_thirty(self):\n"
        "        self.assertEqual(load_timeout({}), (30, 10))\n\n"
        "if __name__ == '__main__':\n"
        "    unittest.main()\n"
    ),
}
TASK = (
    "Change only the request_timeout default from 10 to 30. Preserve the "
    "request_retries default and any unrelated text. Run python -m unittest."
)


def _write_project(root: Path) -> None:
    for rel, content in FILES.items():
        (root / rel).write_text(content, encoding="utf-8")


class _EditProbe:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.original = tool_runtime.edit_file
        self.injected = False

    def __call__(self, root: Path, rel: str, blocks):
        if rel != TARGET or self.injected:
            return self.original(root, rel, blocks)
        self.injected = True
        path = self.root / TARGET
        content = path.read_text(encoding="utf-8")
        if ORIGINAL_LINE not in content:
            return tool_runtime.ToolOutcome.error("probe setup failed: original line missing")
        path.write_text(content.replace(ORIGINAL_LINE, STALE_BLOCK, 1), encoding="utf-8")
        return self.original(root, rel, blocks)


def _run_check(root: Path) -> bool:
    completed = subprocess.run(
        (sys.executable, "-B", "-m", "unittest"),
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=30,
    )
    return completed.returncode == 0


def _run_arm(provider, arm: str, max_turns: int) -> dict[str, Any]:
    events: list[RunEvent] = []
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _write_project(root)
        probe = _EditProbe(root)
        original_context = tool_runtime._render_edit_failure_context
        if arm == "baseline":
            tool_runtime._render_edit_failure_context = lambda _content, _search: ""
        try:
            result = agent.run(
                provider,
                root,
                TASK,
                max_turns=max_turns,
                on_event=events.append,
                fresh_chat=True,
                provider_id=getattr(provider, "id", ""),
                project_map=render_production_project_map(root, task=TASK),
                tool_fns=AgentToolFns(edit_file=probe),
            )
        finally:
            tool_runtime._render_edit_failure_context = original_context

        content = (root / TARGET).read_text(encoding="utf-8")
        final_tests_passed = _run_check(root)

    failed_edit_index = next(
        (
            index
            for index, event in enumerate(events)
            if event.kind == "tool"
            and event.call is not None
            and event.call.name == "edit"
            and event.outcome is not None
            and not event.outcome.ok
        ),
        -1,
    )
    reads_after_failure = 0
    if failed_edit_index >= 0:
        reads_after_failure = sum(
            1
            for event in events[failed_edit_index + 1 :]
            if event.kind == "tool" and event.call is not None and event.call.name == "read"
        )
    edit_attempts = sum(
        1
        for event in events
        if event.kind == "tool" and event.call is not None and event.call.name == "edit"
    )
    report = {
        "arm": arm,
        "stop_reason": result.stop_reason,
        "summary": result.summary[:500],
        "turns": result.turns,
        "tool_calls": sum(1 for event in events if event.kind == "tool"),
        "edit_attempts": edit_attempts,
        "reads_after_failure": reads_after_failure,
        "final_tests_passed": final_tests_passed,
        "target_correct": EXPECTED_LINE in content,
        "external_comment_preserved": "# seconds" in content,
        "unrelated_default_preserved": "request_retries', 10" in content,
    }
    if result.stop_reason != "done" or not final_tests_passed or EXPECTED_LINE not in content:
        report["event_tail"] = [render_run_event(event) for event in events[-10:]]
    return report


def run_probe(
    provider_id: str,
    port: int,
    max_turns: int,
    arms: tuple[str, ...] = ARMS,
) -> dict[str, Any]:
    provider_controls.begin_task_context(f"edit-failure-context-ab:{provider_id}")
    provider = None
    try:
        provider = connect_provider(provider_id, port=port)
        rows = []
        for arm in arms:
            try:
                row = _run_arm(provider, arm, max_turns)
            except Exception as exc:
                row = {"arm": arm, "error": f"{type(exc).__name__}: {exc}"}
            rows.append(row)
            if "error" in row:
                print(f"[{arm}] error={row['error']}")
            else:
                print(
                    f"[{arm}] success={row['final_tests_passed'] and row['target_correct']} "
                    f"turns={row['turns']} tools={row['tool_calls']} "
                    f"reads_after_failure={row['reads_after_failure']}"
                )
        return {"provider": provider_id, "arms": rows}
    finally:
        try:
            if provider is not None:
                provider.close()
        finally:
            provider_controls.end_task_context()


def run_self_test() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _write_project(root)
        path = root / TARGET
        path.write_text(
            path.read_text(encoding="utf-8").replace(ORIGINAL_LINE, STALE_BLOCK),
            encoding="utf-8",
        )
        hint = tool_runtime._render_edit_failure_context(
            path.read_text(encoding="utf-8"),
            ORIGINAL_LINE,
        )
        assert "4 |" in hint
        assert "# seconds" in hint
        assert len(hint) <= tool_runtime.EDIT_FAILURE_MAX_CHARS
    print("self-test passed")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Live A/B for bounded edit failure context.")
    parser.add_argument("--provider", choices=provider_ids(), default="stepfun")
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--max-turns", type=int, default=10)
    parser.add_argument("--arm", choices=ARMS, action="append")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if args.self_test:
        run_self_test()
        return 0
    arms = tuple(args.arm or ARMS)
    print(
        json.dumps(
            run_probe(args.provider, args.port, args.max_turns, arms),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())