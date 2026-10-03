"""Execution tools expose structured output; no text-only open facade remains."""
from codey.research.tools import ResearchTools


def test_research_tools_has_no_open_url_text_facade():
    assert not hasattr(ResearchTools, "open_url_text")
