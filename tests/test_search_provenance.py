"""Search states and actual acquisition identity survive durable recovery."""
from __future__ import annotations

import pytest

from codey.research.ledger import ResearchLedger
from codey.research.ledger_receipts import ledger_observation, restore_ledger_observation, search_observation
from codey.research.search_contracts import SearchOutcome, complete_search, failed_search
from codey.research.search_provenance import SearchAttempt, filter_site_results, query_consistency
from codey.research.source_gateway import ResearchSourceGateway
from codey.types.json_value import decode_json_object, encode_transport_value


@pytest.mark.parametrize("url,allowed", [
    ("https://example.com/article", True), ("https://docs.example.com/article", True),
    ("https://EXAMPLE.COM./article", True), ("https://example.com.evil.test/article", False),
    ("https://notexample.com/article", False), ("https://example.com@evil.test/article", False),
    ("https://evil.test/?url=https://example.com", False),
])
def test_site_scope_uses_normalized_hostname_boundaries(url: str, allowed: bool) -> None:
    rows, rejected = filter_site_results("topic site:Example.COM.", [{"url": url}])
    assert bool(rows) is allowed
    assert rejected == (() if allowed else (url,))


def test_site_scope_normalizes_idna_and_never_claims_missing_query_is_matched() -> None:
    rows, rejected = filter_site_results("site:bücher.example", [
        {"url": "https://xn--bcher-kva.example/a"}, {"url": "https://bücher.example.evil.test/a"}])
    assert len(rows) == 1 and len(rejected) == 1
    assert query_consistency("https://www.bing.com/search", "query") is None
    assert query_consistency("https://www.bing.com/search?q=old", "query") is False


@pytest.mark.parametrize("state", ["hits", "empty", "filtered_empty", "failed"])
def test_search_record_preserves_final_state_and_attempts_on_restart(state: str) -> None:
    query = "topic site:example.com"
    attempt = SearchAttempt(query, "bing", state=state, effective_query=query,
        failure_kind="search_page_blank" if state == "failed" else "",
        rejected_urls=("https://outside.example/a",) if state == "filtered_empty" else ())
    ledger = ResearchLedger()
    rows = [{"title": "Source", "url": "https://example.com/current", "snippet": "Observed"}] if state == "hits" else []
    ledger.record_search(query, rows, attempts=(attempt,), state=state, failure_kind=attempt.failure_kind)
    restored = ResearchLedger()
    restore_ledger_observation(restored, ledger_observation(ledger, "web_search"))
    assert restored.searches == ledger.searches


def test_legacy_empty_receipt_is_rejected_instead_of_confirmed_no_results() -> None:
    ledger = ResearchLedger()
    ledger.record_search("query", [])
    payload = ledger_observation(ledger, "web_search")
    row = decode_json_object(payload["search"])
    for key in ("version", "state", "failure_kind", "attempts"):
        row.pop(key)
    with pytest.raises(ValueError, match="observation fields"):
        search_observation({"search": row})
    row["version"] = 99
    row["state"] = "unknown"
    row["failure_kind"] = ""
    row["attempts"] = []
    with pytest.raises(ValueError, match="version"):
        search_observation({"search": row})


def test_old_attempt_is_rejected_without_inventing_success() -> None:
    from codey.research.search_provenance import decode_search_attempt
    attempt = SearchAttempt("query", "bing")
    row = decode_json_object(encode_transport_value(attempt))
    for key in ("version", "state", "effective_query", "coverage", "detail", "diagnostics"):
        row.pop(key)
    with pytest.raises(ValueError, match="invalid search provenance"):
        decode_search_attempt(row)


def test_current_search_receipts_do_not_invent_missing_fields() -> None:
    ledger = ResearchLedger()
    ledger.record_search("query", [])
    payload = ledger_observation(ledger, "web_search")
    row = decode_json_object(payload["search"])
    del row["attempts"]
    with pytest.raises(ValueError):
        search_observation({"search": row})


@pytest.mark.parametrize("failure", [False, True])
def test_gateway_records_failed_attempts_without_minting_source_evidence(failure: bool) -> None:
    class Provider:
        def search(self, query: str, *, limit: int) -> SearchOutcome:
            if failure:
                return failed_search(query, "bing", "search_page_blank")
            return complete_search(query, [{"url": "https://elsewhere.example/a", "title": "Outside scope"}],
                                   SearchAttempt(query, "bing"), limit)
        def fetch(self, url: str) -> dict[str, object]:
            raise AssertionError("search cannot read source content")
    ledger = ResearchLedger()
    outcome = ResearchSourceGateway(Provider(), ledger).search("topic site:example.com")
    assert bool(outcome.error) is failure
    assert len(ledger.searches) == 1
    assert ledger.searches[0].state == ("failed" if failure else "filtered_empty")
    assert not ledger.read_passages and not ledger.opened_sources
