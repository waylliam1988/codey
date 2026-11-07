"""Typed completion inputs crossing into the kernel completion gates.

External callers may still provide a mapping while they migrate. The mapping
is normalized once at the kernel boundary; completion code receives this
record and no longer needs to guess whether a value is a dict or an object.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from codey.knowledge.store import KnowledgeStore
    from codey.research.ledger import ResearchLedger
    from codey.runtime.observe.execution_evidence import ExecutionEvidence


@dataclass(frozen=True)
class CompletionContext:
    run_id: str = ""
    task: str = ""
    project: str = ""
    workspace_ignored_paths: tuple[str, ...] = ()
    execution_evidence: ExecutionEvidence | None = None
    analysis_run_payloads: tuple[object, ...] = ()
    research_ledger: ResearchLedger | None = None
    research_execution_ledger: ResearchLedger | None = None
    knowledge_store: KnowledgeStore | None = None
    knowledge_project: str = ""
    knowledge_session_id: str = ""
    research_prerequisites: object | None = None
    selected_check: str | None = None
    project_evaluation: object | None = None
    changes: object | None = None
    scope_files: tuple[str, ...] = ()
    task_changed: bool | None = None
    checkpoint_green: bool = False
    extras: Mapping[str, object] = field(default_factory=dict, repr=False)

    @classmethod
    def from_value(cls, value: object, *, ignored_paths: tuple[str, ...] = ()) -> CompletionContext:
        if value is None:
            return cls(workspace_ignored_paths=ignored_paths)
        if isinstance(value, cls):
            if ignored_paths and value.workspace_ignored_paths != ignored_paths:
                return cls(**{**value.__dict__, "workspace_ignored_paths": ignored_paths})
            return value
        if not isinstance(value, Mapping):
            value = {
                key: getattr(value, key)
                for key in (
                    "run_id", "task", "project", "workspace_ignored_paths", "execution_evidence",
                    "analysis_run_payloads", "research_ledger", "research_execution_ledger",
                    "knowledge_store", "knowledge_project", "knowledge_session_id",
                    "research_prerequisites", "selected_check", "project_evaluation", "changes",
                    "scope_files", "task_changed", "checkpoint_green",
                )
                if hasattr(value, key)
            }
        known = {
            "run_id", "task", "project", "workspace_ignored_paths", "execution_evidence",
            "analysis_run_payloads", "research_ledger", "research_execution_ledger",
            "knowledge_store", "knowledge_project", "knowledge_session_id",
            "research_prerequisites", "selected_check", "project_evaluation", "changes",
            "scope_files", "task_changed", "checkpoint_green",
        }
        raw_paths = value.get("workspace_ignored_paths", ignored_paths)
        paths = tuple(str(item) for item in raw_paths) if raw_paths is not None else ()
        raw_scope = value.get("scope_files", ())
        scope = tuple(str(item) for item in raw_scope) if raw_scope is not None else ()
        return cls(
            run_id=str(value.get("run_id") or ""),
            task=str(value.get("task") or ""),
            project=str(value.get("project") or ""),
            workspace_ignored_paths=paths,
            execution_evidence=value.get("execution_evidence"),
            analysis_run_payloads=tuple(value.get("analysis_run_payloads") or ()),
            research_ledger=value.get("research_ledger"),
            research_execution_ledger=value.get("research_execution_ledger"),
            knowledge_store=value.get("knowledge_store"),
            knowledge_project=str(value.get("knowledge_project") or ""),
            knowledge_session_id=str(value.get("knowledge_session_id") or ""),
            research_prerequisites=value.get("research_prerequisites"),
            selected_check=value.get("selected_check") if isinstance(value.get("selected_check"), str) else None,
            project_evaluation=value.get("project_evaluation"),
            changes=value.get("changes"),
            scope_files=scope,
            task_changed=value.get("task_changed") if isinstance(value.get("task_changed"), bool) else None,
            checkpoint_green=value.get("checkpoint_green") is True,
            extras={key: item for key, item in value.items() if key not in known},
        )

    def get(self, key: str, default: object = None) -> object:
        if hasattr(self, key):
            return getattr(self, key)
        return self.extras.get(key, default)


CompletionContextInput = CompletionContext | Mapping[str, object]

__all__ = ["CompletionContext", "CompletionContextInput"]
