"""One legacy mapping/attribute boundary for completion resource admission."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields, replace
from typing import TypeVar

from codey.completion.engine import CompletionEvidence
from codey.completion.inputs import AnalysisRunEvidence, CompletionChangeSet
from codey.completion.verification_policy import VerificationCandidate
from codey.knowledge.store import KnowledgeStore
from codey.operations.completion_context import CompletionContext
from codey.operations.research_prerequisites import ResearchPrerequisiteFacts
from codey.operations.task_session import TaskSession
from codey.research.ledger import ResearchLedger
from codey.research.staging import StagedKnowledgeStore
from codey.runtime.observe.execution_evidence import ExecutionEvidence
from codey.types.json_value import require_json_boolean, require_json_string

T = TypeVar("T")


def _optional_instance(value: object, model: type[T], field: str) -> T | None:
    if value is None or isinstance(value, model):
        return value
    raise ValueError(f"completion {field} must be {model.__name__}")


def _strings(value: object, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (tuple, list)):
        raise ValueError(f"completion {field} must be a string sequence")
    return tuple(require_json_string(item, field) for item in value)


def decode_completion_context(value: object, *, ignored_paths: tuple[str, ...] = ()) -> CompletionContext:
    if value is None:
        return CompletionContext(workspace_ignored_paths=ignored_paths)
    if isinstance(value, CompletionContext):
        return replace(value, workspace_ignored_paths=ignored_paths) if ignored_paths else value
    known = tuple(item.name for item in fields(CompletionContext))
    row: dict[str, object] = {}
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("completion context keys must be strings")
            if key in known:
                row[key] = item
    else:
        # Fixed historical attribute surface, never a generic core accessor.
        for key in known:
            if hasattr(value, key):
                row[key] = getattr(value, key)
    analysis = row.get("analysis_run_payloads", ())
    if not isinstance(analysis, (tuple, list)):
        raise ValueError("completion analysis_run_payloads must be a sequence")
    store = row.get("knowledge_store")
    if store is not None and not isinstance(store, (KnowledgeStore, StagedKnowledgeStore)):
        raise ValueError("completion knowledge_store is invalid")
    changed = row.get("task_changed")
    if changed is not None:
        changed = require_json_boolean(changed, "task_changed")
    changes = row.get("changes")
    prerequisites = row.get("research_prerequisites")
    if isinstance(prerequisites, TaskSession):
        prerequisites = ResearchPrerequisiteFacts.capture(prerequisites)
    return CompletionContext(
        run_id=require_json_string(row.get("run_id", ""), "run_id"),
        task=require_json_string(row.get("task", ""), "task"),
        project=require_json_string(row.get("project", ""), "project"),
        workspace_ignored_paths=_strings(row.get("workspace_ignored_paths", ignored_paths), "workspace_ignored_paths"),
        execution_evidence=_optional_instance(row.get("execution_evidence"), ExecutionEvidence, "execution_evidence"),
        analysis_run_payloads=tuple(AnalysisRunEvidence.from_value(item) for item in analysis),
        research_ledger=_optional_instance(row.get("research_ledger"), ResearchLedger, "research_ledger"),
        research_execution_ledger=_optional_instance(row.get("research_execution_ledger"), ResearchLedger,
                                                     "research_execution_ledger"),
        knowledge_store=store,
        knowledge_project=require_json_string(row.get("knowledge_project", ""), "knowledge_project"),
        knowledge_session_id=require_json_string(row.get("knowledge_session_id", ""), "knowledge_session_id"),
        research_prerequisites=_optional_instance(prerequisites, ResearchPrerequisiteFacts, "research_prerequisites"),
        selected_check=_optional_instance(row.get("selected_check"), VerificationCandidate, "selected_check"),
        project_evaluation=_optional_instance(row.get("project_evaluation"), CompletionEvidence, "project_evaluation"),
        changes=CompletionChangeSet.from_value(changes) if changes is not None else None,
        scope_files=_strings(row.get("scope_files", ()), "scope_files"),
        task_changed=changed,
        checkpoint_green=require_json_boolean(row.get("checkpoint_green", False), "checkpoint_green"),
    )
