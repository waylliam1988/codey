"""Lightweight no-JSON diagnostics for Research protocol repair."""

from __future__ import annotations

from codey.research.tool_contract import (
    PROTOCOL_DIRECT_ANSWER,
    PROTOCOL_NATIVE_SEARCH_LEAK,
    PROTOCOL_NO_JSON,
)

_NATIVE_SEARCH_PHRASES = (
    "i searched the web",
    "i searched online",
    "search results show",
    "web search results",
    "browser search",
    "我搜索了网页",
    "我在网上搜索",
    "我用网页搜索",
    "根据搜索结果",
    "网页搜索结果",
)

_REPORT_HEADINGS = (
    "## 结论",
    "## 关键证据",
    "## 反证与限制",
    "## 来源质量",
    "## 搜索覆盖",
    "## 来源",
)

_ENGLISH_REPORT_HEADINGS = (
    "## conclusion",
    "## key evidence",
    "## sources",
    "## limitations",
)


def classify_no_json_reply(text: str) -> tuple[str, str]:
    stripped = (text or "").strip()
    if not stripped:
        return PROTOCOL_NO_JSON, "no JSON tool call found"
    lowered = stripped[:1200].lower()
    if any(phrase in lowered for phrase in _NATIVE_SEARCH_PHRASES):
        return (
            PROTOCOL_NATIVE_SEARCH_LEAK,
            "reply appears to use the chat website's own search or outside knowledge",
        )
    if _looks_like_direct_report(stripped, lowered):
        return PROTOCOL_DIRECT_ANSWER, "reply was a direct research answer, not a JSON tool call"
    return PROTOCOL_NO_JSON, "no JSON tool call found"


def _looks_like_direct_report(text: str, lowered: str) -> bool:
    heading_count = sum(1 for heading in _REPORT_HEADINGS if heading in text)
    if heading_count >= 2:
        return True
    english_count = sum(1 for heading in _ENGLISH_REPORT_HEADINGS if heading in lowered)
    if english_count >= 2:
        return True
    lead = text[:300]
    return "结论" in lead and any(
        marker in text[:1200] for marker in ("关键证据", "证据", "来源", "限制")
    )
