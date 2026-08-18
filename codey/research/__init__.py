"""Research runner and tools integrated into Codey."""

from codey.research.advisors import EvidenceNote, EvidencePack, run_research_advisors
from codey.research.browser_search import BrowserSearchProvider
from codey.research.connector_search import ConnectorAwareSearchProvider
from codey.research.provenance import provenance_problem
from codey.research.report_quality import ReportQualityReview, review_report_quality
from codey.research.runner import ResearchRunResult, ResearchRunner
from codey.research.tools import ResearchTools

__all__ = [
    "BrowserSearchProvider",
    "ConnectorAwareSearchProvider",
    "EvidenceNote",
    "EvidencePack",
    "ReportQualityReview",
    "ResearchRunResult",
    "ResearchRunner",
    "ResearchTools",
    "provenance_problem",
    "run_research_advisors",
    "review_report_quality",
]
