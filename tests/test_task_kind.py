from __future__ import annotations

import pytest

from codey.task.kind import (
    conversation_mode,
    resolve_task_kind,
    startup_failover_mode,
    trace_mode,
    ui_mode,
    writer_failover_mode,
)
from codey.task.model import TaskSubmission


def _submission(intent: str, project: str | None) -> TaskSubmission:
    return TaskSubmission(
        session_id="session-1",
        project=project,
        task="Do the work",
        max_turns=3,
        continue_task=False,
        provider_id="deepseek",
        intent=intent,
    )


@pytest.mark.parametrize(
    ("intent", "with_project", "without_project"),
    [
        ("auto", "project", "chat"),
        ("chat", "chat", "chat"),
        ("hybrid", "hybrid", "research"),
        ("planning", "planning_readonly", "chat"),
        ("planning_readonly", "planning_readonly", "chat"),
        ("project", "project", "chat"),
        ("readonly", "planning_readonly", "chat"),
        ("research", "research", "research"),
        ("review", "review", "chat"),
        ("unknown", "project", "chat"),
    ],
)
def test_resolve_task_kind_is_one_table(intent: str, with_project: str, without_project: str) -> None:
    assert resolve_task_kind(_submission(intent, "E:/work/project")) == with_project
    assert resolve_task_kind(_submission(intent, None)) == without_project


def test_resolve_task_kind_normalizes_intent_text() -> None:
    assert resolve_task_kind(_submission("  HYBRID  ", "E:/work/project")) == "hybrid"
    assert resolve_task_kind(_submission("", "E:/work/project")) == "project"


@pytest.mark.parametrize(
    (
        "kind",
        "startup",
        "writer",
        "conversation_project",
        "conversation_no_project",
        "ui_project",
        "ui_no_project",
        "trace_project",
        "trace_no_project",
    ),
    [
        ("chat", "chat", "chat", "chat", "chat", "chat", "chat", "chat", "chat"),
        ("hybrid", "research", "project", "project", "chat", "hybrid", "hybrid", "hybrid", "chat"),
        (
            "planning_readonly",
            "planning_readonly",
            "planning_readonly",
            "planning",
            "planning",
            "planning",
            "planning",
            "planning",
            "planning",
        ),
        ("project", "project", "project", "project", "chat", "agent", "chat", "project", "chat"),
        ("research", "research", "research", "research", "research", "research", "research", "research", "research"),
        ("review", "review", "review", "chat", "chat", "review", "review", "review", "review"),
        ("unknown", "unknown", "unknown", "chat", "chat", "agent", "chat", "project", "chat"),
    ],
)
def test_mode_projections_are_one_table(
    kind: str,
    startup: str,
    writer: str,
    conversation_project: str,
    conversation_no_project: str,
    ui_project: str,
    ui_no_project: str,
    trace_project: str,
    trace_no_project: str,
) -> None:
    project = "E:/work/project"

    assert startup_failover_mode(kind) == startup
    assert writer_failover_mode(kind) == writer
    assert conversation_mode(kind, project) == conversation_project
    assert conversation_mode(kind, None) == conversation_no_project
    assert ui_mode(kind, project) == ui_project
    assert ui_mode(kind, None) == ui_no_project
    assert trace_mode(kind, project) == trace_project
    assert trace_mode(kind, None) == trace_no_project
