from __future__ import annotations

from tests.manual import source_connector_done_ab


def test_trace_bounds_are_owned_by_done_harness() -> None:
    assert isinstance(source_connector_done_ab.MAX_TRACE_BYTES, int)
    assert source_connector_done_ab.MAX_TRACE_BYTES > source_connector_done_ab.MAX_RESULT_BYTES
    assert isinstance(source_connector_done_ab.TRACE_PROMPT_CHARS, int)
    assert isinstance(source_connector_done_ab.TRACE_REPLY_CHARS, int)


def test_source_connector_done_self_test() -> None:
    source_connector_done_ab._self_test()
