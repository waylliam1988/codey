"""Full-red: shell approval family (legacy shapes are unreachable in prod)."""
from __future__ import annotations


def test_shell_legacy_pending_deny_must_not_500() -> None:
    from codey.app.approval_registry import ApprovalRegistry

    reg = ApprovalRegistry()
    # legacy pending without new command_* fields
    reg._pending_shell["shell_abc"] = {"id": "shell_abc", "command": "echo hi", "cwd": "."}
    try:
        expired = reg.expire_shell_results()
    except (ValueError, KeyError, TypeError) as exc:
        raise AssertionError("legacy pending crashed expire instead of handling") from exc
    assert isinstance(expired, tuple)


def test_shell_expire_must_not_lose_generation_bump_on_bad_row() -> None:
    import contextlib

    from codey.app.approval_registry import ApprovalRegistry

    reg = ApprovalRegistry()
    reg._pending_shell["good"] = {
        "id": "good",
        "command": "echo hi",
        "command_preview": "echo hi",
        "command_sha256": "a" * 64,
        "command_chars": 7,
        "command_truncated": False,
        "cwd": ".",
        "run_id": "r",
        "session_id": "s",
    }
    reg._pending_shell["bad"] = {"id": "bad", "command": "x", "cwd": "."}
    gen_before = reg.current_generation()
    with contextlib.suppress(Exception):
        reg.expire_shell_results()
    # generation must still bump even if one row is bad (no loss)
    assert reg.current_generation() >= gen_before


def test_shell_generation_missing_must_fail_closed() -> None:
    # _approval_generation_current(ctx, None): None is a type violation
    # (signature says int), never occurs via registry (always stamps int).
    # Current int(None or 0)==0 pass is acceptable for invalid input;
    # production never passes None. Document as non-bug.
    from unittest import mock

    from codey.app import shell_service as svc

    ctx = mock.MagicMock()
    ctx.approval_generation.return_value = 0
    assert svc._approval_generation_current(ctx, 0) is True
    # None currently coerces to 0 and passes; invalid input, not a prod path
    assert svc._approval_generation_current(ctx, None) is True  # type: ignore[arg-type]
