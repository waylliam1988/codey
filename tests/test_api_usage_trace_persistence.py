"""Normalized usage survives restart without connector imports or double accounting."""
import json

from codey.providers.token_accounting import ApiExchangeUsage, ContextBudget, ReportedUsage, RequestContextCount
from codey.runs.trace import RunTraceStore


def test_usage_is_deduplicated_persisted_and_missing_requests_remain_incomplete(tmp_path):
    store = RunTraceStore(tmp_path)
    recorder = store.open(run_id="r", session_id="s", project=None, mode_initial="chat", provider_initial="zen")
    first = ApiExchangeUsage("one", "zen", "fixture", "openai-completions", RequestContextCount(40, "estimated"),
                             ContextBudget(1000, 100, 10, 100), ReportedUsage(100, 50, cached_input_tokens=80,
                             reasoning_output_tokens=30), "reported", "response")
    recorder.record_api_usage(first)
    recorder.record_api_usage(first)
    recorder.record_api_usage(ApiExchangeUsage("two", "zen", "fixture", "openai-completions", first.context,
                                              first.budget, ReportedUsage(), "missing", "unknown"))
    payload = json.loads(store.path_for("s", "r").read_text(encoding="utf8"))
    assert payload["api_usage_totals"] == {"requests": 2, "known_input_tokens": 100,
                                          "known_output_tokens": 50, "incomplete_requests": 1}
    assert len(payload["api_usage"]) == 2
    assert payload["api_usage"][1]["usage"]["input_tokens"] is None
    assert "hello" not in str(payload["api_usage"])


def test_bounded_request_rows_do_not_truncate_totals_or_latest_context(tmp_path):
    from codey.runs.trace_schema import MAX_API_USAGE_ROWS

    store = RunTraceStore(tmp_path)
    recorder = store.open(run_id="r", session_id="s", project=None, mode_initial="chat", provider_initial="local")
    for index in range(MAX_API_USAGE_ROWS + 2):
        recorder.record_api_usage(ApiExchangeUsage(str(index), "local", "fixture", "openai-completions",
                                  RequestContextCount(index, "tokenizer"), ContextBudget(1000, 100, 0, 100),
                                  ReportedUsage(10, 3), "reported", "response"))
    payload = json.loads(store.path_for("s", "r").read_text(encoding="utf8"))
    assert len(payload["api_usage"]) == MAX_API_USAGE_ROWS
    assert payload["api_usage_truncated"] is True
    assert payload["api_usage_totals"]["requests"] == MAX_API_USAGE_ROWS + 2
    assert payload["api_usage_totals"]["known_input_tokens"] == (MAX_API_USAGE_ROWS + 2) * 10
    assert payload["api_usage_latest"]["context"]["value"] == MAX_API_USAGE_ROWS + 1
