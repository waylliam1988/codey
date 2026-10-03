"""Acquisition status is explicit and cannot be inferred from source prose."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from codey.research.ledger import ResearchLedger
from codey.research.source_gateway import ResearchSourceGateway


@pytest.mark.parametrize("prefix", ["ERROR:", "SKIPPED:"])
def test_successful_source_prefix_is_opened_as_evidence(prefix):
    url = "https://example.com/guide"
    page = {"url": url, "title": "Guide", "text": prefix + " example from documentation", "status": "ok"}
    ledger = ResearchLedger()
    gateway = ResearchSourceGateway(SimpleNamespace(fetch=lambda _: page), ledger)
    with patch("codey.research.source_gateway.check_fetch_url", return_value=""):
        result = gateway.open(url)
    assert result.status == "ok"
    assert result.document.text == page["text"]
    assert ledger.final_url_set() == {url}


@pytest.mark.parametrize("status", ["error", "skipped"])
def test_failed_fetch_never_becomes_opened_evidence_without_english_prefix(status):
    url = "https://example.com/guide"
    page = {"url": url, "title": "", "text": "权限不足", "status": status, "detail": "权限不足"}
    ledger = ResearchLedger()
    gateway = ResearchSourceGateway(SimpleNamespace(fetch=lambda _: page), ledger)
    with patch("codey.research.source_gateway.check_fetch_url", return_value=""):
        result = gateway.open(url)
    assert result.status == status
    assert result.detail == "权限不足"
    assert ledger.final_url_set() == set()


def test_fetch_missing_status_is_rejected_without_observing_document():
    url = "https://example.com/guide"
    ledger = ResearchLedger()
    gateway = ResearchSourceGateway(SimpleNamespace(fetch=lambda _: {"url": url, "text": "body"}), ledger)
    with patch("codey.research.source_gateway.check_fetch_url", return_value=""):
        result = gateway.open(url)
    assert result.status == "error"
    assert ledger.final_url_set() == set()


@pytest.mark.parametrize("status", [[], {}, True, 1, "unknown"])
def test_invalid_fetch_status_fails_closed_without_raising(status):
    url = "https://example.com/guide"
    ledger = ResearchLedger()
    gateway = ResearchSourceGateway(SimpleNamespace(fetch=lambda _: {"url": url, "text": "body", "status": status}), ledger)
    with patch("codey.research.source_gateway.check_fetch_url", return_value=""):
        result = gateway.open(url)
    assert result.status == "error"
    assert ledger.final_url_set() == set()
