"""Project retry task text into each final API request window."""
from __future__ import annotations

import re

TASK_HEADING = "User task (verbatim):\n"
_RETRY_HEADER = re.compile(r"Codey retry task \(([0-9]{1,8}) characters\):\n")


def retry_task_prompt(task: str, repair: str, *, scoped: bool) -> str:
    heading = f"Codey retry task ({len(task)} characters):\n" if scoped else TASK_HEADING
    return heading + task + "\n\n" + repair


def _retry_parts(text: str) -> tuple[str, str] | None:
    match = _RETRY_HEADER.match(text)
    if match is None:
        return None
    end = match.end() + int(match[1])
    if text[end:end + 2] != "\n\n":
        return None
    return text[match.end():end], text[end + 2:]


def project_retry_tasks(items: list[dict[str, object]]) -> None:
    retries: list[tuple[dict[str, object], str, str]] = []
    originals: list[str] = []
    for item in items:
        content = item.get("content")
        if item.get("role") != "user" or not isinstance(content, str):
            continue
        if content.startswith(TASK_HEADING):
            originals.append(content.removeprefix(TASK_HEADING))
        elif (parts := _retry_parts(content)) is not None:
            retries.append((item, *parts))
    retained: set[str] = set()
    for item, task, repair in reversed(retries):
        if task in retained or any(text == task or text.startswith(task + "\n\n") for text in originals):
            item["content"] = repair
        else:
            item["content"] = TASK_HEADING + task + "\n\n" + repair
        retained.add(task)
