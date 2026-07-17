"""Live A/B probe for the production default post-edit verification gate."""

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
from codey.project_map import render_project_map
from codey.providers.registry import connect_provider, provider_ids
from codey.verification_policy import (
    VerificationCandidate,
    check_covers_selected_candidate,
    discover_verification_candidates,
    select_verification_candidate,
)


ARMS = ("baseline", "current")
@dataclass(frozen=True)
class Case:
    name: str
    task: str
    files: dict[str, str]
    expected_changes: tuple[str, ...]
    independent_check: Callable[[Path], bool]
    facts: tuple[VerificationCandidate, ...] = ()


def _run_process(root: Path, command: tuple[str, ...], cwd: str = ".") -> bool:
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


def _text_equals(path: str, expected: str) -> Callable[[Path], bool]:
    return lambda root: (root / path).read_text(encoding="utf-8") == expected


CASES = (
    Case(
        name="python-pytest",
        task="Change only clamp's upper bound from 10 to 8.",
        files={
            "limits.py": "def clamp(value):\n    return min(10, max(0, value))\n",
            "test_limits.py": (
                "from limits import clamp\n\n"
                "def test_upper_bound():\n"
                "    assert clamp(20) == 8\n"
            ),
            "pytest.ini": "[pytest]\n",
        },
        expected_changes=("limits.py",),
        independent_check=lambda root: _run_process(root, (sys.executable, "-B", "-m", "pytest", "-q")),
    ),
    Case(
        name="npm-test",
        task="Change only the default greeting from hello to hi.",
        files={
            "app.js": "export function greeting() { return 'hello'; }\n",
            "test.js": (
                "import { greeting } from './app.js';\n"
                "if (greeting() !== 'hi') process.exit(1);\n"
            ),
            "package.json": json.dumps({
                "type": "module",
                "scripts": {"test": "node test.js"},
            }, indent=2) + "\n",
        },
        expected_changes=("app.js",),
        independent_check=_text_equals(
            "app.js",
            "export function greeting() { return 'hi'; }\n",
        ),
    ),
    Case(
        name="docs-only",
        task="Change the README heading from Old Guide to Quick Guide.",
        files={
            "README.md": "# Old Guide\n\nWelcome.\n",
            "pytest.ini": "[pytest]\n",
            "test_smoke.py": "def test_ok():\n    assert True\n",
        },
        expected_changes=("README.md",),
        independent_check=_text_equals("README.md", "# Quick Guide\n\nWelcome.\n"),
    ),
    Case(
        name="no-trusted-check",
        task="Change only DEFAULT_SIZE from 4 to 6.",
        files={"settings.py": "DEFAULT_SIZE = 4\n"},
        expected_changes=("settings.py",),
        independent_check=_text_equals("settings.py", "DEFAULT_SIZE = 6\n"),
    ),
    Case(
        name="monorepo-nearest",
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
        expected_changes=("frontend/src/app.js",),
        independent_check=_text_equals(
            "frontend/src/app.js",
            "export function greeting() { return 'hi'; }\n",
        ),
    ),
)


def _write_case(root: Path, case: Case) -> None:
    for rel, content in case.files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _changed_files(root: Path, original: dict[str, str]) -> tuple[str, ...]:
    return tuple(
        sorted(
            rel
            for rel, before in original.items()
            if not (root / rel).is_file()
            or (root / rel).read_text(encoding="utf-8") != before
        )
    )


def _run_arm(provider, case: Case, arm: str, max_turns: int) -> dict[str, Any]:
    events: list[RunEvent] = []
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _write_case(root, case)
        candidates = discover_verification_candidates(root, case.facts)
        selected = select_verification_candidate(candidates, case.expected_changes)
        gate = arm == "current" and selected is not None
        result = agent.run(
            provider,
            root,
            case.task,
            max_turns=max_turns,
            on_event=events.append,
            fresh_chat=True,
            provider_id=getattr(provider, "id", ""),
            project_map=render_project_map(root, task=case.task),
            verification_candidates=candidates if arm == "current" else (),
            verification_candidate_loader=(
                (lambda: discover_verification_candidates(root, case.facts))
                if arm == "current"
                else None
            ),
        )
        changed = _changed_files(root, case.files)
        correct = case.independent_check(root)

    tool_events = [event for event in events if event.kind == "tool" and event.call is not None]
    edit_indexes = [index for index, event in enumerate(tool_events) if event.call.name == "edit" and event.outcome and event.outcome.changed]
    latest_edit = edit_indexes[-1] if edit_indexes else -1
    runs_after_edit = [
        event
        for index, event in enumerate(tool_events)
        if index > latest_edit and event.call.name == "run"
    ]
    selected_runs = [
        event
        for event in runs_after_edit
        if selected is not None
        and check_covers_selected_candidate(
            selected,
            str(event.call.args.get("command") or ""),
            str(event.call.args.get("path") or "."),
            changed or case.expected_changes,
        )
    ]
    selected_passed = any(
        event.outcome is not None and event.outcome.ok and event.outcome.exit_code == 0
        for event in selected_runs
    )
    report = {
        "case": case.name,
        "arm": arm,
        "candidate": vars(selected) if selected else None,
        "gate_enabled": gate,
        "stop_reason": result.stop_reason,
        "turns": result.turns,
        "tool_calls": len(tool_events),
        "changed_files": list(changed),
        "independent_check_passed": correct,
        "runs_after_latest_edit": len(runs_after_edit),
        "selected_check_passed_after_latest_edit": selected_passed,
        "wrong_run_attempts": len(runs_after_edit) - len(selected_runs),
    }
    if result.stop_reason != "done" or not correct:
        report["event_tail"] = [render_run_event(event) for event in events[-10:]]
    return report


def run_probe(
    provider_id: str,
    port: int,
    cases: tuple[Case, ...],
    arms: tuple[str, ...],
    max_turns: int,
) -> dict[str, Any]:
    provider_controls.begin_task_context(f"default-verification-ab:{provider_id}")
    provider = None
    rows = []
    try:
        provider = connect_provider(provider_id, port=port)
        for case in cases:
            for arm in arms:
                try:
                    row = _run_arm(provider, case, arm, max_turns)
                except Exception as exc:
                    row = {"case": case.name, "arm": arm, "error": f"{type(exc).__name__}: {exc}"}
                rows.append(row)
                if "error" in row:
                    print(f"[{case.name} {arm}] {row['error']}")
                else:
                    print(
                        f"[{case.name} {arm}] correct={row['independent_check_passed']} "
                        f"verified={row['selected_check_passed_after_latest_edit']} "
                        f"turns={row['turns']} tools={row['tool_calls']}"
                    )
    finally:
        try:
            if provider is not None:
                provider.close()
        finally:
            provider_controls.end_task_context()
    return {"provider": provider_id, "results": rows}


def run_self_test() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        case = next(item for item in CASES if item.name == "monorepo-nearest")
        _write_case(root, case)
        candidates = discover_verification_candidates(root)
        selected = select_verification_candidate(candidates, case.expected_changes)
        if shutil.which("npm") is not None:
            assert selected is not None
            assert selected.command == "npm test"
            assert selected.cwd == "frontend"
        else:
            assert selected is None
        assert select_verification_candidate(candidates, ("README.md",)) is None
        assert select_verification_candidate((), ("settings.py",)) is None
    print("self-test passed")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Live A/B for default post-edit verification.")
    parser.add_argument("--provider", choices=provider_ids(), default="mimo")
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--case", choices=[case.name for case in CASES], action="append")
    parser.add_argument("--arm", choices=ARMS, action="append")
    parser.add_argument("--max-turns", type=int, default=12)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if args.self_test:
        run_self_test()
        return 0
    cases = tuple(case for case in CASES if not args.case or case.name in args.case)
    arms = tuple(args.arm or ARMS)
    report = run_probe(args.provider, args.port, cases, arms, args.max_turns)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
