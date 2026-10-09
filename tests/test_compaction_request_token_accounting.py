"""Auxiliary usage contributes to cost but cannot replace the conversation context measurement."""
from codey.providers.token_accounting import ApiExchangeUsage, ContextBudget, ReportedUsage, RequestContextCount
from codey.runs.trace import RunTraceStore


def test_auxiliary_usage_does_not_overwrite_latest_conversation_context(tmp_path):
    recorder = RunTraceStore(tmp_path).open(run_id="r", session_id="s", project=None, mode_initial="chat", provider_initial="local")
    budget = ContextBudget(8192, 1024, 0, 2000)
    recorder.record_api_usage(ApiExchangeUsage("answer", "local", "m", "openai-completions", RequestContextCount(100, "tokenizer"),
                                               budget, ReportedUsage(100, 10), "reported", "response"))
    recorder.record_api_usage(ApiExchangeUsage("summary", "local", "m", "openai-completions", RequestContextCount(700, "tokenizer"),
                                               budget, ReportedUsage(700, 50), "reported", "response", "compaction"))
    assert recorder.manifest.api_usage_latest["exchange_id"] == "answer"
    assert recorder.manifest.api_usage_totals["known_input_tokens"] == 800
