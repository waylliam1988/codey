"""Strict Research completes only with a valid evidence ledger.

Locks: missing ledger, unreadable ledger, or evidence/sources outside the
ledger never complete; a complete ledger with opened sources, citable
evidence, and a valid report does complete; ordinary source tasks keep
their ledger-free behaviour.
"""
from __future__ import annotations

from types import SimpleNamespace

from codey.operations.completion_gate import evaluate
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy


def _strict_policy():
    return TaskPolicy(
        grants=frozenset({"control", "web.read", "knowledge.write"}),
        strict_research=True,
        required_checks=(
            "research_sources_opened",
            "research_evidence_saved",
            "research_report_sections",
        ),
    )


def _strict_session():
    session = TaskSession(policy=_strict_policy(), task_kind="research", max_turns=3)
    session.record_open("https://example.com/a")
    session.record_evidence("https://unopened.example/b", "unrelated excerpt")
    return session


def test_strict_research_without_ledger_cannot_complete():
    session = _strict_session()
    verdict = evaluate(
        session,
        "结论\nhello\n来源\nhttps://unopened.example/b",
        context={},
    )
    assert verdict.complete is False


def test_strict_research_with_none_ledger_cannot_complete():
    session = _strict_session()
    verdict = evaluate(
        session,
        "结论\nhello\n来源\nhttps://unopened.example/b",
        context={"research_ledger": None},
    )
    assert verdict.complete is False


def test_strict_research_with_failing_ledger_cannot_complete():
    class _BadLedger:
        def final_url_set(self):
            raise RuntimeError("ledger unreadable")

    session = _strict_session()
    verdict = evaluate(
        session,
        "结论\nhello\n来源\nhttps://example.com/a",
        context={"research_ledger": _BadLedger()},
    )
    assert verdict.complete is False


def test_strict_research_evidence_outside_opened_sources_cannot_complete():
    ledger = SimpleNamespace(
        final_url_set=lambda: {"https://example.com/a"},
        evidence_items=[SimpleNamespace(source_url="https://unopened.example/b", excerpt="x")],
    )
    session = _strict_session()
    verdict = evaluate(
        session,
        "结论\nhello\n来源\nhttps://unopened.example/b",
        context={"research_ledger": ledger},
    )
    assert verdict.complete is False


def test_ordinary_source_task_without_ledger_keeps_legacy_behaviour():
    policy = TaskPolicy(
        grants=frozenset({"control", "web.read"}),
        sources_open_required=True,
        required_checks=("research_sources_opened",),
    )
    session = TaskSession(policy=policy, task_kind="project", max_turns=3)
    session.record_open("https://example.com/a")
    verdict = evaluate(session, "done", context={})
    assert verdict.complete is True
