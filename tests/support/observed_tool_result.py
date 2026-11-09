"""Explicit acquisition fixtures for tests that replace a source executor.

The fixture declares its search hit and read content, then uses the production
observation encoder. It never extracts facts from its display text.
"""
from codey.research.ledger import ResearchLedger
from codey.research.ledger_receipts import ledger_observation
from codey.research.passages import describe_captured_window
from codey.runtime.core.models import ToolCall, ToolResult
from codey.types.json_value import JsonObject


def observed_tool_result(call: ToolCall, text: str, *, audit: JsonObject | None = None) -> ToolResult:
    canonical: JsonObject = {}
    ledger = ResearchLedger()
    if call.name == "web_search":
        ledger.record_search(call.args["query"], [{"url": "https://example.com/a", "title": "Fixture"}])
        canonical = {"research_observation": ledger_observation(ledger, "web_search")}
    elif call.name == "open_url":
        url = call.args["url"]
        ledger.record_open(url, url, "Fixture", text)
        observation = ledger_observation(ledger, "open", url=url)
        canonical = {"research_observation": observation,
                     "read_passages": describe_captured_window(observation, 0, len(text))}
    return ToolResult(call, text, ok=True, canonical=canonical, audit=audit or {})
