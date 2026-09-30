"""Completion gate hygiene: no workspace-less pass, no silent fallbacks.

- A task that requires project verification cannot complete when neither
  the verification row nor the session carries workspace identity.
- Reading ``required_checks`` must fail closed, never be treated as "no
  requirements".
- The gate imports the completion contract limit once at module top; it
  never hardcodes a duplicate fallback.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


def test_workspace_less_verification_does_not_complete() -> None:
    from codey.operations import completion_gate

    session = SimpleNamespace(
        project_changes_required=True,
        policy=SimpleNamespace(required_checks=()),
        edited_files={"a.txt": 3},
        verifications=[
            {
                "command": "pytest -q",
                "cwd": ".",
                "revision": 3,
                "exit_code": 0,
                "passed": True,
            }
        ],
        verification_forbidden=False,
        workspace_fingerprint="",
        workspace_revision=0,
        project="",
        task_kind="project",
        opened_sources=set(),
        evidence=[],
        search_results={},
    )
    verdict = completion_gate.evaluate(session, "done")
    assert verdict.complete is False


def test_required_checks_read_failure_blocks_completion() -> None:
    from codey.completion.contract import CHECK_PASS, completion_check
    from codey.operations import completion_gate

    class ExplodingPolicy:
        @property
        def required_checks(self):  # noqa: D102
            raise RuntimeError("boom")

    session = SimpleNamespace(
        policy=ExplodingPolicy(),
        edited_files={},
        verifications=[],
        verification_forbidden=True,
        task_kind="project",
        opened_sources=set(),
        evidence=[],
        search_results={},
    )
    verdict = completion_gate._required_checks_verdict(
        session, [completion_check("x", CHECK_PASS)]
    )
    assert verdict is not None
    assert verdict.complete is False


def test_gate_does_not_hardcode_completion_limit_fallback() -> None:
    text = (ROOT / "codey/operations/completion_gate.py").read_text(encoding="utf-8-sig")
    assert "_MAX_CHECKS = 12" not in text
    assert "from codey.completion.contract import" in text
    assert "MAX_COMPLETION_CHECKS" in text
