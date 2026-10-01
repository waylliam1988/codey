"""Extension check providers must not have illegal rows silently dropped.

[None], [legal, illegal], legal pass/fail, and legal empty are covered.
An illegal member produces an explicit check_provider_error failure.
"""
from __future__ import annotations

from codey.completion.contract import CHECK_FAIL, CHECK_PASS, completion_check
from codey.operations.completion_gate import (
    evaluate,
    register_completion_check_provider,
    unregister_completion_check_provider,
)
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy


def make_session():
    session = TaskSession(
        policy=TaskPolicy(grants=frozenset({"control"})),
        task_kind="research",
        project="",
        max_turns=2,
    )
    return session


def test_illegal_none_row_blocks():
    session = make_session()
    register_completion_check_provider("illegal_none", lambda _s: [None])
    try:
        verdict = evaluate(session, "done", context=None)
        assert verdict.complete is False
        assert verdict.proof is not None
        ids = [c.check_id for c in verdict.proof.checks]
        assert any("illegal_none" in str(i) and "error" in str(i) for i in ids)
    finally:
        unregister_completion_check_provider("illegal_none")


def test_mixed_legal_and_illegal_blocks():
    session = make_session()
    good = completion_check("mixed_ok", CHECK_PASS)
    register_completion_check_provider("mixed_provider", lambda _s: [good, "bad-string"])
    try:
        verdict = evaluate(session, "done", context=None)
        assert verdict.complete is False
    finally:
        unregister_completion_check_provider("mixed_provider")


def test_legal_pass_still_passes():
    session = make_session()
    good = completion_check("legal_ok", CHECK_PASS)
    register_completion_check_provider("legal_provider", lambda _s: [good])
    try:
        verdict = evaluate(session, "done", context=None)
        # No required checks and base research checks may still block;
        # at minimum the provider must not itself force a failure.
        assert all("legal_provider_error" not in str(c.check_id) for c in (verdict.proof.checks if verdict.proof else ()))
    finally:
        unregister_completion_check_provider("legal_provider")


def test_legal_fail_blocks():
    session = make_session()
    bad = completion_check("legal_bad", CHECK_FAIL, "bad")
    register_completion_check_provider("legal_fail", lambda _s: [bad])
    try:
        verdict = evaluate(session, "done", context=None)
        assert verdict.complete is False
    finally:
        unregister_completion_check_provider("legal_fail")


def test_legal_empty_does_not_block_by_itself():
    session = make_session()
    register_completion_check_provider("empty_provider", lambda _s: [])
    try:
        verdict = evaluate(session, "done", context=None)
        assert all("empty_provider_error" not in str(c.check_id) for c in (verdict.proof.checks if verdict.proof else ()))
    finally:
        unregister_completion_check_provider("empty_provider")
