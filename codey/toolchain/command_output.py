"""Output presentation never changes command execution or its exit status."""

from __future__ import annotations


def tail_lines(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not 1 <= value <= 2000:
        raise ValueError("output_tail_lines must be an integer between 1 and 2000")
    return value


def output_tail(text: str, count: int | None) -> str:
    if count is None or len(text.splitlines()) <= count:
        return text
    return f"[Showing last {count} lines; execution and exit code are unchanged.]\n" + "\n".join(text.splitlines()[-count:])
