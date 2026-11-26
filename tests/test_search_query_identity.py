"""Query identity tolerates whitespace while retaining the original request."""
from __future__ import annotations

from urllib.parse import urlencode

import pytest

from codey.research.search_contracts import SearchState
from codey.research.search_page import SearchObservation
from codey.research.search_profiles import load_profiles
from codey.research.search_provenance import query_consistency
from codey.types.json_value import JsonObject


@pytest.mark.parametrize("encoded,query,expected", [
    ("q=python+pathlib", "python  pathlib", True),
    ("q=python++pathlib", "python\tpathlib", True),
    ("q=C%2B%2B+reference", "C++  reference", True),
    ("q=C+++reference", "C++ reference", False),
    ("q=Python+pathlib", "python pathlib", False),
    ("q=pathlib+python", "python pathlib", False),
    ("q=%22python+pathlib%22", "python pathlib", False),
    ("q=python&q=python", "python", False),
    ("q=&q=python", "python", False),
    ("q=python&q=", "python", False),
    ("q=", "python", False),
    ("form=homepage", "python", None),
])
def test_url_query_identity(encoded: str, query: str, expected: bool | None) -> None:
    assert query_consistency("https://cn.bing.com/search?" + encoded, query) is expected


@pytest.mark.parametrize("url_query", ["python  pathlib", "python pathlib", None])
def test_collapsed_input_matches_only_the_submitted_query(url_query: str | None) -> None:
    query = "python  pathlib"
    url = "https://cn.bing.com/search" + ("?" + urlencode({"q": url_query}) if url_query is not None else "")
    value: JsonObject = {"url": url, "query": "python pathlib", "challenge": False, "http_status": 200,
        "rows": [{"url": "https://docs.python.org/3/library/pathlib.html", "title": "Official", "snippet": ""}],
        "empty": "", "blocks": 1}
    observed = SearchObservation(value, {"document_id": "new"})
    outcome = observed.classify(query, load_profiles()[0], 3, submitted_document={"document_id": "old"})
    assert outcome is not None and outcome.state == SearchState.HITS
    assert outcome.attempts[0].requested_query == outcome.attempts[0].effective_query == query
    if url_query is None:
        assert observed.classify(query, load_profiles()[0], 3, submitted_document=observed.document) is None
    value["query"] = "python sqlite3"
    mismatch = observed.classify(query, load_profiles()[0], 3, expired=True,
                                 submitted_document={"document_id": "old"})
    assert mismatch is not None and mismatch.failure_kind == "search_query_mismatch"


def test_old_url_is_not_overridden_by_a_matching_input() -> None:
    value: JsonObject = {"url": "https://cn.bing.com/search?q=old", "query": "new query",
        "challenge": False, "http_status": 200}
    observed = SearchObservation(value, {"document_id": "new"})
    assert observed.classify("new query", load_profiles()[0], 3) is None
    outcome = observed.classify("new query", load_profiles()[0], 3, expired=True)
    assert outcome is not None and outcome.failure_kind == "search_query_mismatch"
