"""Safe context compaction for OpenAI-style message lists.

Deterministic and side-effect free. Never splits an
``assistant(tool_calls) -> tool`` group, never drops the system prompt, the
latest user request, or the most recent complete tool groups. Large tool
outputs enter the summary as receipts, never verbatim.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from codey.agents.handoff import estimate_tokens

SUMMARY_PREFIX_TEXT = (
    "[compacted context summary: earlier turns were compressed to stay within "
    "the model context window. Only facts needed to continue are kept below.]"
)
MAX_SUMMARY_SOURCE_CHARS = 24_000


def estimate_message_tokens(message: Mapping[str, object]) -> int:
    role = str(message.get("role") or "")
    content = message.get("content")
    text = content if isinstance(content, str) else str(content or "")
    total = estimate_tokens(f"{role}\n{text}")
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        import json as _json

        try:
            total += estimate_tokens(_json.dumps(tool_calls, ensure_ascii=False, sort_keys=True))
        except Exception:
            total += estimate_tokens(str(tool_calls))
    name = message.get("name")
    if isinstance(name, str) and name:
        total += estimate_tokens(name)
    return total


def estimate_messages_tokens(messages: Sequence[Mapping[str, object]]) -> int:
    return sum(estimate_message_tokens(m) for m in messages)


def estimate_tools_tokens(tools: Sequence[Mapping[str, object]] | None) -> int:
    if not tools:
        return 0
    import json as _json

    try:
        return estimate_tokens(_json.dumps(list(tools), ensure_ascii=False, sort_keys=True))
    except Exception:
        return estimate_tokens(str(list(tools)))


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
    if not _has_tool_calls(first):
        return True
    calls = first.get("tool_calls")
    expected = len(calls) if isinstance(calls, list) else 0
    actual = sum(1 for i in group[1:] if _is_tool_message(messages[i]))
    return actual >= expected


def find_safe_cut(
    messages: Sequence[Mapping[str, object]],
    *,
    reserve_tokens: int,
    keep_recent_tokens: int,
    context_window_tokens: int,
    tools_tokens: int = 0,
) -> int:
    """Return a message index cut point; prefix ``[:cut]`` may be compacted.

    Returns 0 when nothing should be compacted. The cut never lands inside a
    tool group and never consumes the system prompt (index 0 when it is a
    system message) or the trailing recent window.
    """

    total = len(messages)
    if total <= 2:
        return 0
    used = estimate_messages_tokens(messages) + tools_tokens
    if used + reserve_tokens <= context_window_tokens:
        return 0
    groups = group_messages_for_compaction(messages)
    # Accumulate recent token budget from the tail over complete groups only.
    kept: set[int] = set()
    running = 0
    for group in reversed(groups):
        if not is_tool_group_complete(messages, group):
            # Incomplete trailing group must be kept verbatim, but it does not
            # count toward the keep budget (it is uncompressible state).
            kept.update(group)
            continue
        group_tokens = sum(estimate_message_tokens(messages[i]) for i in group)
        if running + group_tokens > keep_recent_tokens and kept:
            break
        running += group_tokens
        kept.update(group)
        if running >= keep_recent_tokens:
            break
    start = 1 if total and str(messages[0].get("role") or "") == "system" else 0
    candidates = sorted(set(range(total)) - kept)
    # Only compact complete groups fully before the kept tail.
    cut = start
    for group in groups:
        if any(i in kept for i in group):
            break
        if not is_tool_group_complete(messages, group):
            break
        if group[0] < start:
            continue
        if set(group) <= set(candidates):
            cut = max(cut, group[-1] + 1)
        else:
            break
    # Never compact when the cut leaves no tail behind the summary.
    if cut >= total:
        return 0
    if cut <= start:
        return cut
    return cut


def summarize_prefix_deterministically(prefix: Sequence[Mapping[str, object]]) -> str:
    lines = [SUMMARY_PREFIX_TEXT]
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
            snippet = text.strip().replace("\n", " ")[:300]
            lines.append(f"- {role}: {snippet}")
        if len("\n".join(lines)) > MAX_SUMMARY_SOURCE_CHARS:
            break
    return "\n".join(lines)


def sanitize_groups_for_summary(groups_text: str, max_chars: int = MAX_SUMMARY_SOURCE_CHARS) -> str:
    text = str(groups_text or "")
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n[truncated]"


def build_compaction_summary_prompt(sanitized: str) -> str:
    return (
        "Summarize the earlier tool session for continuation. Keep only: goal, "
        "key decisions, files read/modified, verification state, next step. "
        "Omit full file bodies and full command output.\n\n"
        f"{sanitized}"
    )


def compact_openai_messages(
    messages: Sequence[Mapping[str, object]],
    summary: str,
    cut_index: int,
) -> list[dict]:
    head = [dict(m) for m in messages[:cut_index]]
    _ = head
    tail = [dict(m) for m in messages[cut_index:]]
    summary_message: dict = {"role": "user", "content": f"{SUMMARY_PREFIX_TEXT}\n{summary}"}
    if tail and str(tail[0].get("role") or "") == "system":
        return [tail[0], summary_message, *tail[1:]]
    first = messages[0] if messages else None
    if first is not None and str(first.get("role") or "") == "system":
        return [dict(first), summary_message, *tail[1 if tail and tail[0] == dict(first) else 0:]]
    return [summary_message, *tail]


def compact_openai_messages_in_place(
    messages: list[dict],
    *,
    tools: Sequence[Mapping[str, object]] | None = None,
    context_window_tokens: int = 128_000,
    reserve_tokens: int = 16_384,
    keep_recent_tokens: int = 20_000,
) -> str:
    cut = find_safe_cut(
        messages,
        reserve_tokens=reserve_tokens,
        keep_recent_tokens=keep_recent_tokens,
        context_window_tokens=context_window_tokens,
        tools_tokens=estimate_tools_tokens(tools),
    )
    if cut <= 0:
        return ""
    prefix = list(messages[:cut])
    summary = summarize_prefix_deterministically(prefix)
    compacted = compact_openai_messages(messages, summary, cut)
    messages.clear()
    messages.extend(compacted)
    return summary


__all__ = [
    "MAX_SUMMARY_SOURCE_CHARS",
    "SUMMARY_PREFIX_TEXT",
    "build_compaction_summary_prompt",
    "compact_openai_messages",
    "compact_openai_messages_in_place",
    "estimate_message_tokens",
    "estimate_messages_tokens",
    "estimate_tools_tokens",
    "find_safe_cut",
    "group_messages_for_compaction",
    "is_tool_group_complete",
    "sanitize_groups_for_summary",
    "summarize_prefix_deterministically",
]
