from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from codey import agent, provider_controls
from codey.events import RunEvent, render_run_event
from codey.providers.registry import DEFAULT_PROVIDER_ID, connect_provider, provider_ids
from tests.manual.project_task_context import render_production_project_map


GUARD_MESSAGE = "read_file required before editing existing file:"
ARMS = ("baseline", "guard")


@dataclass(frozen=True)
class GuardCase:
    name: str
    task: str
    files: dict[str, str]
    check_command: tuple[str, ...]


CASES = (
    GuardCase(
        name="similar-config-constant",
        task=(
            "Change only the parallel tool call limit from 4 to 3. "
            "Do not change the accidental tool call limit. Run python -m unittest."
        ),
        files={
            "limits.py": (
                "MAX_ACCIDENTAL_TOOL_CALLS = 8\n"
                "MAX_PARALLEL_CALLS = 4\n\n"
                "def validate_parallel(count):\n"
                "    return count <= MAX_PARALLEL_CALLS\n\n"
                "def validate_accidental(count):\n"
                "    return count <= MAX_ACCIDENTAL_TOOL_CALLS\n"
            ),
            "test_limits.py": (
                "import unittest\n"
                "from limits import validate_accidental, validate_parallel\n\n"
                "class LimitTests(unittest.TestCase):\n"
                "    def test_parallel_limit(self):\n"
                "        self.assertTrue(validate_parallel(3))\n"
                "        self.assertFalse(validate_parallel(4))\n\n"
                "    def test_accidental_limit_stays_eight(self):\n"
                "        self.assertTrue(validate_accidental(8))\n"
                "        self.assertFalse(validate_accidental(9))\n\n"
                "if __name__ == '__main__':\n"
                "    unittest.main()\n"
            ),
        },
        check_command=(sys.executable, "-B", "-m", "unittest"),
    ),
    GuardCase(
        name="similar-route-handlers",
        task=(
            "Fix only admin_route so disabled users get HTTP 403. "
            "Leave user_route behavior unchanged. Run python -m unittest."
        ),
        files={
            "routes.py": (
                "def user_route(user):\n"
                "    if not user.get('enabled', False):\n"
                "        return 401\n"
                "    return 200\n\n"
                "def admin_route(user):\n"
                "    if not user.get('enabled', False):\n"
                "        return 401\n"
                "    if not user.get('admin', False):\n"
                "        return 403\n"
                "    return 200\n"
            ),
            "test_routes.py": (
                "import unittest\n"
                "from routes import admin_route, user_route\n\n"
                "class RouteTests(unittest.TestCase):\n"
                "    def test_user_route_unchanged(self):\n"
                "        self.assertEqual(user_route({'enabled': False}), 401)\n\n"
                "    def test_admin_disabled_user_is_forbidden(self):\n"
                "        self.assertEqual(admin_route({'enabled': False, 'admin': True}), 403)\n\n"
                "if __name__ == '__main__':\n"
                "    unittest.main()\n"
            ),
        },
        check_command=(sys.executable, "-B", "-m", "unittest"),
    ),
    GuardCase(
        name="rename-function-callers",
        task=(
            "Rename build_payload to build_request_payload and update callers. "
            "Keep the public behavior unchanged. Run python -m unittest."
        ),
        files={
            "payloads.py": (
                "def build_payload(user_id, active):\n"
                "    return {'user_id': user_id, 'active': active}\n\n"
                "def send_payload(client, user_id):\n"
                "    return client.send(build_payload(user_id, True))\n"
            ),
            "test_payloads.py": (
                "import unittest\n"
                "import payloads\n\n"
                "class PayloadTests(unittest.TestCase):\n"
                "    def test_renamed_builder_exists(self):\n"
                "        self.assertEqual(\n"
                "            payloads.build_request_payload('u1', True),\n"
                "            {'user_id': 'u1', 'active': True},\n"
                "        )\n\n"
                "    def test_sender_uses_renamed_builder(self):\n"
                "        class Client:\n"
                "            def __init__(self):\n"
                "                self.sent = None\n"
                "            def send(self, payload):\n"
                "                self.sent = payload\n"
                "                return 'ok'\n"
                "        client = Client()\n"
                "        self.assertEqual(payloads.send_payload(client, 'u2'), 'ok')\n"
                "        self.assertEqual(client.sent, {'user_id': 'u2', 'active': True})\n\n"
                "if __name__ == '__main__':\n"
                "                    unittest.main()\n"
            ),
        },
        check_command=(sys.executable, "-B", "-m", "unittest"),
    ),
)


def _write_case(root: Path, case: GuardCase) -> None:
    for rel, content in case.files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _changed_files(root: Path, original: dict[str, str]) -> tuple[str, ...]:
    changed: list[str] = []
    for rel, before in original.items():
        path = root / rel
        if not path.exists() or path.read_text(encoding="utf-8") != before:
            changed.append(rel)
    for path in root.rglob("*"):
        if path.is_file():
            rel = path.relative_to(root).as_posix()
            if rel not in original:
                changed.append(rel)
    return tuple(sorted(changed))


def _run_check(root: Path, command: tuple[str, ...]) -> bool:
    completed = subprocess.run(
        command,
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=30,
    )
    return completed.returncode == 0


def _disabled_guard(_root: Path, _rel: str, _known_file_paths: set[str]) -> None:
    return None


def _with_guard(arm: str) -> Callable:
    if arm != "baseline":
        return agent._read_before_edit_outcome
    original = agent._read_before_edit_outcome
    agent._read_before_edit_outcome = _disabled_guard
    return original


def _restore_guard(original: Callable) -> None:
    agent._read_before_edit_outcome = original


def _run_case(provider, root: Path, case: GuardCase, arm: str, max_turns: int) -> dict[str, Any]:
    events: list[RunEvent] = []
    original_guard = _with_guard(arm)
    try:
        result = agent.run(
            provider,
            root,
            case.task,
            max_turns=max_turns,
            on_event=events.append,
            fresh_chat=True,
            provider_id=getattr(provider, "id", ""),
            project_map=render_production_project_map(root, task=case.task),
        )
    finally:
        _restore_guard(original_guard)

    rendered = [render_run_event(event) for event in events]
    guard_blocks = sum(1 for line in rendered if GUARD_MESSAGE in line)
    read_after_block = False
    if guard_blocks:
        blocked = False
        for event in events:
            text = render_run_event(event)
            if GUARD_MESSAGE in text:
                blocked = True
            elif blocked and event.kind == "tool" and event.call is not None and event.call.name == "read":
                read_after_block = True
                break
    final_tests_passed = _run_check(root, case.check_command)
    report = {
        "stop_reason": result.stop_reason,
        "summary": result.summary[:500],
        "turns": result.turns,
        "tool_calls": sum(1 for event in events if event.kind == "tool"),
        "guard_blocks": guard_blocks,
        "read_after_block": read_after_block,
        "final_tests_passed": final_tests_passed,
        "changed_files": list(_changed_files(root, case.files)),
    }
    if not final_tests_passed:
        report["event_tail"] = rendered[-8:]
    return report


def run_probe(
    provider_id: str,
    port: int,
    cases: tuple[GuardCase, ...],
    max_turns: int,
    arms: tuple[str, ...] = ARMS,
) -> dict[str, Any]:
    provider_controls.begin_task_context(f"read-before-edit-ab:{provider_id}")
    provider = None
    try:
        provider = connect_provider(provider_id, port=port)
        results = []
        for case in cases:
            item: dict[str, Any] = {"case": case.name}
            with tempfile.TemporaryDirectory() as td:
                base = Path(td)
                for arm in arms:
                    root = base / arm
                    if root.exists():
                        shutil.rmtree(root)
                    root.mkdir()
                    _write_case(root, case)
                    item[arm] = _run_case(provider, root, case, arm, max_turns)
                    print(
                        f"[{case.name}] {arm}: "
                        f"success={item[arm]['final_tests_passed']} "
                        f"blocks={item[arm]['guard_blocks']} "
                        f"turns={item[arm]['turns']} "
                        f"tools={item[arm]['tool_calls']}"
                    )
            results.append(item)
    finally:
        try:
            if provider is not None:
                provider.close()
        finally:
            provider_controls.end_task_context()

    return {
        "provider": provider_id,
        "cases": results,
        "summary": {
            arm: {
                "final_successes": sum(1 for item in results if item[arm]["final_tests_passed"]),
                "guard_blocks": sum(item[arm]["guard_blocks"] for item in results),
                "turns": sum(item[arm]["turns"] for item in results),
                "tool_calls": sum(item[arm]["tool_calls"] for item in results),
            }
            for arm in arms
        },
    }


def run_self_test() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        case = CASES[0]
        _write_case(root, case)
        assert _run_check(root, case.check_command) is False
        assert _changed_files(root, case.files) == ()
    print("self-test passed")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Live A/B for read-before-edit guard.")
    parser.add_argument("--provider", choices=provider_ids(), default=DEFAULT_PROVIDER_ID)
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--case", choices=[case.name for case in CASES], action="append")
    parser.add_argument("--arm", choices=ARMS, action="append")
    parser.add_argument("--max-turns", type=int, default=12)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)

    if args.self_test:
        run_self_test()
        return 0
    selected = tuple(case for case in CASES if not args.case or case.name in args.case)
    selected_arms = tuple(args.arm or ARMS)
    report = run_probe(args.provider, args.port, selected, args.max_turns, selected_arms)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
