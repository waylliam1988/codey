"""Research runner and tools integrated into Codey."""

from codey.research.advisors import EvidenceNote, EvidencePack, run_research_advisors
from codey.research.browser_search import BrowserSearchProvider
from codey.research.connector_search import ConnectorAwareSearchProvider
from codey.research.done_finalizer import FinalizedAnswer, finalize_done_answer
from codey.research.evidence_followup import EvidenceFollowupResult, run_evidence_followup
from codey.research.pipeline import EvidenceFollowupRunner, ResearchIterationRun, ResearchPipeline, ResearchPipelineResult
from codey.research.plan_executor import PlanExecutionResult, PlanExecutor
from codey.research.provenance import provenance_problem
from codey.research.record_merge import merge_evidence_patch
from codey.research.report_quality import ReportQualityReview, review_report_quality
from codey.research.runner import ResearchRunResult, ResearchRunner
from codey.research.tools import ResearchTools

__all__ = [
    "BrowserSearchProvider",
    "ConnectorAwareSearchProvider",
    "EvidenceFollowupResult",
    "EvidenceFollowupRunner",
    "EvidenceNote",
    "EvidencePack",
    "FinalizedAnswer",
    "PlanExecutionResult",
    "PlanExecutor",
    "ReportQualityReview",
    "ResearchRunResult",
    "ResearchIterationRun",
    "ResearchPipeline",
    "ResearchPipelineResult",
    "ResearchRunner",
    "ResearchTools",
    "finalize_done_answer",
    "merge_evidence_patch",
    "provenance_problem",
    "review_report_quality",
    "run_evidence_followup",
    "run_research_advisors",
]
