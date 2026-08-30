"""Task kind projection from the submitted contract."""

from __future__ import annotations

from codey.task.model import TaskSubmission


def resolve_task_kind(request: TaskSubmission) -> str:
    intent = (request.intent or "auto").strip().lower()
    if intent in {"planning", "planning_readonly", "readonly"}:
        return "planning_readonly" if request.project else "chat"
    if intent in {"research", "project", "hybrid", "chat", "review"}:
        if intent == "hybrid" and not request.project:
            return "research"
        if intent == "project" and not request.project:
            return "chat"
        if intent == "review" and not request.project:
            return "chat"
        return intent
    return "project" if request.project else "chat"


def startup_failover_mode(task_kind: str) -> str:
    return "research" if task_kind == "hybrid" else task_kind


def writer_failover_mode(task_kind: str) -> str:
    return "project" if task_kind == "hybrid" else task_kind


def conversation_mode(task_kind: str, project: str | None) -> str:
    if task_kind == "research":
        return "research"
    if task_kind == "planning_readonly":
        return "planning"
    if task_kind in {"project", "hybrid"}:
        return "project" if project else "chat"
    return "chat"


def ui_mode(kind: str, project: str | None) -> str:
    if kind == "chat":
        return "chat"
    if kind == "research":
        return "research"
    if kind == "hybrid":
        return "hybrid"
    if kind == "planning_readonly":
        return "planning"
    if kind == "review":
        return "review"
    return "agent" if project else "chat"


def trace_mode(kind: str, project: str | None) -> str:
    if kind == "planning_readonly":
        return "planning"
    if kind in {"chat", "research", "project", "hybrid", "review"}:
        if kind in {"project", "hybrid"} and not project:
            return "chat"
        return kind
    return "project" if project else "chat"


__all__ = [
    "conversation_mode",
    "resolve_task_kind",
    "startup_failover_mode",
    "trace_mode",
    "ui_mode",
    "writer_failover_mode",
]
