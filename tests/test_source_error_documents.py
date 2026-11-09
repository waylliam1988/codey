"""Whole JSON failure envelopes are unavailable documents, not article evidence."""
from __future__ import annotations

import pytest

from codey.research.extract import _fetch_page_failure_kind, _usable_fetch_page_text, extract_text


@pytest.mark.parametrize("body", [
    '{"error":{"message":"您当前请求存在异常，暂时限制本次访问。","code":40362}}',
    '{"error":{"message":"The requested resource cannot be accessed.","code":403}}',
    '{"error":"Service is temporarily unable to serve this request.","request_id":"123"}',
])
def test_whole_error_envelope_is_not_readable(body: str) -> None:
    text = extract_text(body)
    assert not _usable_fetch_page_text(text)
    assert _fetch_page_failure_kind(text) == "page_unavailable"


@pytest.mark.parametrize("body", [
    'Documentation example: {"error":{"message":"The requested resource cannot be accessed.","code":403}}',
    '{"error":null,"data":{"measured_quantity":"37 grams"}}',
    '{"article":"This report describes the measured quantity and the error budget.","error":0}',
])
def test_documents_describing_errors_remain_readable(body: str) -> None:
    assert _usable_fetch_page_text(extract_text(body))
