from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _mypy(
    *paths: str,
    follow_imports: str | None = None,
    platform: str | None = None,
) -> str:
    flags = [] if follow_imports is None else ["--follow-imports", follow_imports]
    if platform is not None:
        flags.extend(["--platform", platform])
    result = subprocess.run(
        [sys.executable, "-m", "mypy", "--ignore-missing-imports", *flags, *paths],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout + result.stderr


def test_positive_int_helper_has_no_numeric_overload_diagnostic() -> None:
    output = _mypy("codey/utils/positive_int.py")
    assert "codey\\utils\\positive_int.py" not in output


def test_receipt_payload_reader_has_no_index_diagnostic() -> None:
    output = _mypy("codey/runs/receipt.py")
    assert "codey\\runs\\receipt.py" not in output


def test_provider_registry_accepts_worker_provider_close_contract() -> None:
    output = _mypy("codey/providers/registry.py")
    assert 'Incompatible return value type (got "WorkerChatProvider"' not in output


def test_task_submission_protocol_is_clean_at_application_boundary() -> None:
    output = _mypy(
        "codey/app/task_submit.py",
        "codey/app/server.py",
        "codey/app/headless_runner.py",
        "codey/operations/task_state.py",
        follow_imports="skip",
    )
    assert "codey\\app\\task_submit.py" not in output
    assert "codey\\app\\server.py" not in output
    assert "codey\\app\\headless_runner.py" not in output


def test_dynamic_payload_boundaries_are_clean() -> None:
    output = _mypy(
        "codey/workspace/changes.py",
        "codey/completion/contract.py",
        "codey/runtime/log/entries.py",
        follow_imports="skip",
    )
    assert "codey\\workspace\\changes.py" not in output
    assert "codey\\completion\\contract.py" not in output
    assert "codey\\runtime\\log\\entries.py" not in output


def test_process_tree_adapter_is_clean() -> None:
    output = _mypy("codey/runtime/core/cancellation.py", follow_imports="skip")
    assert "codey\\runtime\\core\\cancellation.py" not in output


def test_process_tree_adapter_is_clean_on_linux_stubs() -> None:
    output = _mypy(
        "codey/runtime/core/cancellation.py",
        follow_imports="skip",
        platform="linux",
    )
    assert "Module has no attribute \"WinDLL\"" not in output
    assert "Module has no attribute \"WinError\"" not in output
    assert "Module has no attribute \"get_last_error\"" not in output
    assert "Module has no attribute \"CREATE_NEW_PROCESS_GROUP\"" not in output


def test_kernel_and_evidence_boundaries_are_clean() -> None:
    output = _mypy(
        "codey/operations/task_loop.py",
        "codey/operations/project_writer_phase.py",
        "codey/operations/project_completion_enforcement.py",
        "codey/research/evidence_ledger.py",
        "codey/runs/trace.py",
        follow_imports="skip",
    )
    for module in (
        "codey\\operations\\task_loop.py",
        "codey\\operations\\project_writer_phase.py",
        "codey\\operations\\project_completion_enforcement.py",
        "codey\\research\\evidence_ledger.py",
        "codey\\runs\\trace.py",
    ):
        assert module not in output
