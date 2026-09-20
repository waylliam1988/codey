"""Research runner and tools integrated into Codey.

Public convenience exports resolve lazily so importing a leaf (or the
package itself) never pays the runner/browser/pipeline import tax. Cold
paths such as ``import codey.app.api`` only touch leaves; the heavy stack
loads on first real use. Mirrors ``codey.providers``.
"""

from __future__ import annotations

from typing import Any

_EXPORTS: dict[str, tuple[str, str]] = {
    "BrowserSearchProvider": ("codey.research.browser_search", "BrowserSearchProvider"),
    "ConnectorAwareSearchProvider": (
        "codey.research.connector_search",
        "ConnectorAwareSearchProvider",
    ),
    "EvidenceFollowupResult": ("codey.research.evidence_followup", "EvidenceFollowupResult"),
    "EvidenceFollowupRunner": ("codey.research.pipeline", "EvidenceFollowupRunner"),
    "EvidenceNote": ("codey.research.advisors", "EvidenceNote"),
    "EvidencePack": ("codey.research.advisors", "EvidencePack"),
    "FinalizedAnswer": ("codey.research.done_finalizer", "FinalizedAnswer"),
    "PlanExecutionResult": ("codey.research.plan_executor", "PlanExecutionResult"),
    "PlanExecutor": ("codey.research.plan_executor", "PlanExecutor"),
    "ReportQualityReview": ("codey.research.report_quality", "ReportQualityReview"),
    "ResearchIterationRun": ("codey.research.pipeline", "ResearchIterationRun"),
    "ResearchPipeline": ("codey.research.pipeline", "ResearchPipeline"),
    "ResearchPipelineResult": ("codey.research.pipeline", "ResearchPipelineResult"),
    "ResearchRunResult": ("codey.research.runner", "ResearchRunResult"),
    "ResearchRunner": ("codey.research.runner", "ResearchRunner"),
    "ResearchTools": ("codey.research.tools", "ResearchTools"),
    "finalize_done_answer": ("codey.research.done_finalizer", "finalize_done_answer"),
    "merge_evidence_patch": ("codey.research.record_merge", "merge_evidence_patch"),
    "provenance_problem": ("codey.research.provenance", "provenance_problem"),
    "review_report_quality": ("codey.research.report_quality", "review_report_quality"),
    "run_evidence_followup": ("codey.research.evidence_followup", "run_evidence_followup"),
    "run_research_advisors": ("codey.research.advisors", "run_research_advisors"),
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    try:
        module_name, attr_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    module = __import__(module_name, fromlist=[attr_name])
    value = getattr(module, attr_name)
    globals()[name] = value
    return value
