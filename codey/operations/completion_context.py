"""Named inputs for completion; compatibility decoding has a separate owner."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from codey.completion.inputs import AnalysisRunEvidence, CompletionChangeSet
from codey.completion.verification_policy import VerificationCandidate

if TYPE_CHECKING:
    from codey.completion.engine import CompletionEvidence
    from codey.knowledge.store import KnowledgeStore
    from codey.operations.research_prerequisites import ResearchPrerequisiteFacts
    from codey.research.ledger import ResearchLedger
    from codey.research.staging import StagedKnowledgeStore
    from codey.runtime.observe.execution_evidence import ExecutionEvidence
    from codey.types.json_value import JsonObject


@dataclass(frozen=True)
class CompletionContext:
    run_id: str = ""
    task: str = ""
    project: str = ""
    workspace_ignored_paths: tuple[str, ...] = ()
    execution_evidence: ExecutionEvidence | None = None
    analysis_run_payloads: tuple[AnalysisRunEvidence, ...] = ()
    research_ledger: ResearchLedger | None = None
    research_execution_ledger: ResearchLedger | None = None
    knowledge_store: KnowledgeStore | StagedKnowledgeStore | None = None
    knowledge_project: str = ""
    knowledge_session_id: str = ""
    research_prerequisites: ResearchPrerequisiteFacts | None = None
    selected_check: VerificationCandidate | None = None
    project_evaluation: CompletionEvidence | None = None
    changes: CompletionChangeSet | None = None
    scope_files: tuple[str, ...] = ()
    task_changed: bool | None = None
    checkpoint_green: bool = False
    browser_document: JsonObject | None = None

    @classmethod
    def from_value(cls, value: object, *, ignored_paths: tuple[str, ...] = ()) -> CompletionContext:
        from codey.operations.completion_context_codec import decode_completion_context

        return decode_completion_context(value, ignored_paths=ignored_paths)


CompletionContextInput = CompletionContext | Mapping[str, object]

__all__ = ["CompletionContext", "CompletionContextInput"]
