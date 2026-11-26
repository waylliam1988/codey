"""Search errors stay errors all the way to the model-facing tool result."""
from __future__ import annotations

import pytest

from codey.research.search_contracts import SearchOutcome, complete_search, failed_search
from codey.research.search_provenance import SearchAttempt
from codey.research.tools import ResearchTools
from codey.runtime.core.cancellation import DeadlineExceeded, TaskCancelled


class Source:
    def __init__(self, kind: str = "", error: Exception | None = None) -> None:
        self.kind, self.error = kind, error
    def search(self, query: str, *, limit: int) -> SearchOutcome:
        if self.error is not None:
            raise self.error
        if self.kind:
            return failed_search(query, "bing", self.kind)
        return complete_search(query, [], SearchAttempt(query, "bing"), limit, explicit_empty=True)
    def fetch(self, url: str) -> dict[str, object]:
        raise AssertionError("search did not read a source")


def test_browser_timeout_is_error_with_failed_search_record_and_no_evidence() -> None:
    tools = ResearchTools(Source("search_timeout"), None, None)
    result = tools.web_search("query")
    assert not result.ok and result.model_text.startswith("ERROR:")
    assert "search_timeout" in result.model_text
    assert tools.ledger.searches[0].state == "failed"
    assert not tools.search_result_urls and not tools.ledger.read_passages


def test_actual_empty_response_is_a_valid_no_results() -> None:
    tools = ResearchTools(Source(), None, None)
    assert tools.web_search("query").model_text == "no results"
    assert tools.ledger.searches[0].state == "empty"


@pytest.mark.parametrize("error", [TaskCancelled("stopped"), DeadlineExceeded("expired")])
def test_search_tool_propagates_cancellation_without_recording_false_completion(error: Exception) -> None:
    tools = ResearchTools(Source(error=error), None, None)
    with pytest.raises(type(error)):
        tools.web_search("query")
    assert not tools.ledger.searches
