"""Official pathlib excerpts contain Python API names, not additional websites."""

import pytest

from codey.research.provenance import provenance_problem


@pytest.mark.parametrize("api", ["os.path.isreserved()", "`os.path.isreserved()`", "os.path.isreserved"])
def test_pathlib_excerpt_does_not_claim_an_unopened_source_domain(api):
    excerpt = f"This method is deprecated; use {api} to detect reserved paths on Windows."
    assert provenance_problem(excerpt, opened_sources={"https://docs.python.org/3/library/pathlib.html"},
                              search_result_urls=set()) is None


@pytest.mark.parametrize("source", ["https://os.path.isreserved/page", "https://unopened.example.org/page",
                                   "unopened.example.org", "`unopened.example.org`"])
def test_actual_unopened_urls_and_named_websites_still_fail(source):
    assert provenance_problem(f"Source: {source}", opened_sources={"https://docs.python.org/3/library/pathlib.html"},
                              search_result_urls=set()) is not None
