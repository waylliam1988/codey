"""Task kind projection from the submitted contract."""

from __future__ import annotations

from dataclasses import dataclass

from codey.task.model import TaskSubmission


@dataclass(frozen=True)
class _IntentRule:
    with_project: str
    without_project: str


@dataclass(frozen=True)
class _ModeProjection:
    startup: str
    writer: str
    conversation_with_project: str
    conversation_without_project: str
    ui_with_project: str
    ui_without_project: str
    trace_with_project: str
    trace_without_project: str

    def conversation(self, project: str | None) -> str:
        return self.conversation_with_project if project else self.conversation_without_project

    def ui(self, project: str | None) -> str:
        return self.ui_with_project if project else self.ui_without_project

    def trace(self, project: str | None) -> str:
        return self.trace_with_project if project else self.trace_without_project


_INTENT_RULES: dict[str, _IntentRule] = {
    "chat": _IntentRule(with_project="chat", without_project="chat"),
    "hybrid": _IntentRule(with_project="hybrid", without_project="research"),
    "planning": _IntentRule(with_project="planning_readonly", without_project="chat"),
    "planning_readonly": _IntentRule(with_project="planning_readonly", without_project="chat"),
    "project": _IntentRule(with_project="project", without_project="chat"),
    "readonly": _IntentRule(with_project="planning_readonly", without_project="chat"),
    "research": _IntentRule(with_project="research", without_project="research"),
    "review": _IntentRule(with_project="review", without_project="chat"),
}

_MODE_PROJECTIONS: dict[str, _ModeProjection] = {
    "chat": _ModeProjection(
        startup="chat",
        writer="chat",
        conversation_with_project="chat",
        conversation_without_project="chat",
        ui_with_project="chat",
        ui_without_project="chat",
        trace_with_project="chat",
        trace_without_project="chat",
    ),
    "hybrid": _ModeProjection(
        startup="research",
        writer="project",
        conversation_with_project="project",
        conversation_without_project="chat",
        ui_with_project="hybrid",
        ui_without_project="hybrid",
        trace_with_project="hybrid",
        trace_without_project="chat",
    ),
    "planning_readonly": _ModeProjection(
        startup="planning_readonly",
        writer="planning_readonly",
        conversation_with_project="planning",
        conversation_without_project="planning",
        ui_with_project="planning",
        ui_without_project="planning",
        trace_with_project="planning",
        trace_without_project="planning",
    ),
    "project": _ModeProjection(
        startup="project",
        writer="project",
        conversation_with_project="project",
        conversation_without_project="chat",
        ui_with_project="agent",
        ui_without_project="chat",
        trace_with_project="project",
        trace_without_project="chat",
    ),
    "research": _ModeProjection(
        startup="research",
        writer="research",
        conversation_with_project="research",
        conversation_without_project="research",
        ui_with_project="research",
        ui_without_project="research",
        trace_with_project="research",
        trace_without_project="research",
    ),
    "review": _ModeProjection(
        startup="review",
        writer="review",
        conversation_with_project="chat",
        conversation_without_project="chat",
        ui_with_project="review",
        ui_without_project="review",
        trace_with_project="review",
        trace_without_project="review",
    ),
}


def resolve_task_kind(request: TaskSubmission) -> str:
    intent = (request.intent or "auto").strip().lower()
    rule = _INTENT_RULES.get(intent)
    if rule is not None:
        return rule.with_project if request.project else rule.without_project
    return "project" if request.project else "chat"


def startup_failover_mode(task_kind: str) -> str:
    projection = _MODE_PROJECTIONS.get(task_kind)
    return projection.startup if projection is not None else task_kind


def writer_failover_mode(task_kind: str) -> str:
    projection = _MODE_PROJECTIONS.get(task_kind)
    return projection.writer if projection is not None else task_kind


def conversation_mode(task_kind: str, project: str | None) -> str:
    projection = _MODE_PROJECTIONS.get(task_kind)
    return projection.conversation(project) if projection is not None else "chat"


def ui_mode(kind: str, project: str | None) -> str:
    projection = _MODE_PROJECTIONS.get(kind)
    return projection.ui(project) if projection is not None else ("agent" if project else "chat")


def trace_mode(kind: str, project: str | None) -> str:
    projection = _MODE_PROJECTIONS.get(kind)
    return projection.trace(project) if projection is not None else ("project" if project else "chat")


__all__ = [
    "conversation_mode",
    "resolve_task_kind",
    "startup_failover_mode",
    "trace_mode",
    "ui_mode",
    "writer_failover_mode",
]
