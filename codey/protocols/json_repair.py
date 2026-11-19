"""Generate one suffix-only JSON candidate; callers own authorization."""
from __future__ import annotations

from codey.runtime.core.models import JsonTailClosure as JsonTailClosure
from codey.types.json_value import parse_json_object


def close_json_tail(source: str) -> JsonTailClosure | None:
    """Close only unmatched containers after an already complete value token."""
    if not source.lstrip().startswith("{") or source.rstrip()[-1:] not in {'"', "]", "}"}:
        return None
    stack: list[str] = []
    quoted = escaped = False
    for char in source:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "{[":
            stack.append("}" if char == "{" else "]")
        elif char in "}]" and (not stack or stack.pop() != char):
            return None
    if quoted or not stack:
        return None
    suffix = "".join(reversed(stack))
    candidate = source + suffix
    try:
        parse_json_object(candidate)
    except (ValueError, RecursionError):
        return None
    return JsonTailClosure(source, candidate, suffix)
