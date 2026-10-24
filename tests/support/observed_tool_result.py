"""Explicit acquisition fixtures for tests that replace a source executor.

The fixture declares its search hit and read content, then uses the production
observation encoder. It never extracts facts from its display text.
"""
from codey.research.ledger import ResearchLedger
from codey.research.ledger_receipts import ledger_observation
from codey.runtime.core.models import ToolCall, ToolResult


def observed_tool_result(call: ToolCall, text: str, *, audit=None) -> ToolResult:
    canonical = {}
    ledger = ResearchLedger()
    if call.name == "web_search":
        ledger.record_search(call.args["query"], [{"url": "https://example.com/a", "title": "Fixture"}])
        canonical = {"research_observation": ledger_observation(ledger, "web_search")}
    elif call.name == "open_url":
        url = call.args["url"]
        ledger.record_open(url, url, "Fixture", text)
        canonical = {"research_observation": ledger_observation(ledger, "open", url=url)}
    return ToolResult(call, text, ok=True, canonical=canonical, audit=audit or {})
