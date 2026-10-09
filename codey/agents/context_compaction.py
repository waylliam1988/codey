"""Safe context compaction for OpenAI-style message lists.

Deterministic and side-effect free. Never splits an
``assistant(tool_calls) -> tool`` group, never drops the system prompt, the
latest user request, or the most recent complete tool groups. Large tool
outputs enter the summary as receipts, never verbatim.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

SUMMARY_PREFIX_TEXT = (
    "[compacted context summary: earlier turns were compressed to stay within "
    "the model context window. Only facts needed to continue are kept below.]"
)
MAX_SUMMARY_SOURCE_CHARS = 24_000


def _is_tool_message(message: Mapping[str, object]) -> bool:
    return str(message.get("role") or "") == "tool"


def _has_tool_calls(message: Mapping[str, object]) -> bool:
    calls = message.get("tool_calls")
    return isinstance(calls, list) and len(calls) > 0


def group_messages_for_compaction(messages: Sequence[Mapping[str, object]]) -> list[list[int]]:
    """Group message indices so tool groups stay atomic.

    An ``assistant`` message with ``tool_calls`` and its following ``tool``
    messages form one group. Everything else is a singleton group.
    """

    groups: list[list[int]] = []
    index = 0
    total = len(messages)
    while index < total:
        message = messages[index]
        if _has_tool_calls(message):
            group = [index]
            index += 1
            while index < total and _is_tool_message(messages[index]):
                group.append(index)
                index += 1
            groups.append(group)
        else:
            # A stray tool message without a leading assistant call still
            # joins the previous group when one exists, so replay order is kept.
            if _is_tool_message(message) and groups and _has_tool_calls(messages[groups[-1][0]]):
                groups[-1].append(index)
                index += 1
            else:
                groups.append([index])
                index += 1
    return groups


def is_tool_group_complete(messages: Sequence[Mapping[str, object]], group: Sequence[int]) -> bool:
    if not group:
        return False
    first = messages[group[0]]
    calls = first.get("tool_calls")
    if calls is not None and not isinstance(calls, list):
        return False
    if not _has_tool_calls(first):
        return not _is_tool_message(first)
    if first.get("role") != "assistant" or not isinstance(calls, list):
        return False
    expected = [call.get("id") if isinstance(call, Mapping) else None for call in calls]
    results = [messages[i] for i in group[1:]]
    actual = [message.get("tool_call_id") for message in results]
    if any(not _is_tool_message(message) for message in results):
        return False
    if any(type(value) is not str or not value.strip() for value in expected + actual):
        return False
    return len(set(expected)) == len(expected) == len(actual) == len(set(actual)) and set(expected) == set(actual)


def summarize_prefix_deterministically(prefix: Sequence[Mapping[str, object]]) -> str:
    """Summarize a compacted prefix; returns the body only.

    The framing line lives in exactly one place: compact_openai_messages
    prepends SUMMARY_PREFIX_TEXT when it builds the replacement message.
    """
    lines: list[str] = []
    for message in prefix:
        role = str(message.get("role") or "")
        if role == "system":
            continue
        content = message.get("content")
        text = content if isinstance(content, str) else str(content or "")
        calls = message.get("tool_calls")
        if isinstance(calls, list) and calls:
            names: list[str] = []
            for item in calls:
                if isinstance(item, dict):
                    fn = item.get("function")
                    if isinstance(fn, dict) and fn.get("name"):
                        names.append(str(fn.get("name")))
                    elif item.get("name"):
                        names.append(str(item.get("name")))
            lines.append(f"- assistant tool_calls: {', '.join(names) or 'unknown'}")
        elif role == "tool":
            receipt = text.strip().replace("\n", " ")[:200]
            lines.append(f"- tool result: {receipt}")
        elif text.strip():
            text = text.removeprefix(SUMMARY_PREFIX_TEXT).strip()
            snippet = text.strip().replace("\n", " ")[:300]
            lines.append(f"- {role}: {snippet}")
        if len("\n".join(lines)) > MAX_SUMMARY_SOURCE_CHARS:
            break
    return "\n".join(lines)


def compact_openai_messages(
    messages: Sequence[Mapping[str, object]],
    summary: str,
    cut_index: int,
) -> list[dict[str, Any]]:
    tail = [dict(m) for m in messages[cut_index:]]
    summary_message: dict[str, Any] = {"role": "user", "content": f"{SUMMARY_PREFIX_TEXT}\n{summary}"}
    if tail and str(tail[0].get("role") or "") == "system":
        return [tail[0], summary_message, *tail[1:]]
    prefix = [dict(m) for m in messages[:cut_index] if m.get("role") in {"system", "developer"}]
    return [*prefix, summary_message, *tail]
