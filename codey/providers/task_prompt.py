"""Carry the kernel's task text boundaries into the final API request view."""
from __future__ import annotations

from dataclasses import dataclass

from codey.types.json_value import decode_json_object, require_json_boolean, require_json_integer, require_json_string
from codey.utils.object_fields import object_fields, sequence_items

TASK_HEADING = "User task (verbatim):\n"
TASK_PROMPT_KEY = "_codey_task_prompt"


@dataclass(frozen=True)
class TaskPromptSpan:
    run_id: str
    end: int
    retry: bool

    @classmethod
    def for_task(cls, run_id: str, task: str, *, retry: bool) -> TaskPromptSpan | None:
        return cls(run_id, len(TASK_HEADING) + len(task), retry) if task else None

    @classmethod
    def read(cls, value: object) -> TaskPromptSpan:
        row = decode_json_object(value)
        run_id = require_json_string(row.get("run_id"), "task prompt run")
        end = require_json_integer(row.get("end"), "task prompt end")
        retry = require_json_boolean(row.get("retry"), "task prompt retry")
        if not run_id or end <= len(TASK_HEADING) or set(row) != {"run_id", "end", "retry"}:
            raise ValueError("invalid task prompt span")
        return cls(run_id, end, retry)

    def task(self, text: str) -> str:
        if not text.startswith(TASK_HEADING) or text[self.end:self.end + 2] != "\n\n":
            raise ValueError("task prompt span differs from the frozen text")
        return text[len(TASK_HEADING):self.end]


def _text_field(item: dict[str, object]) -> tuple[dict[str, object], str]:
    content = item.get("content")
    if (sequence_items(content) and content and object_fields(part := content[0])
            and part.get("type") in ("text", "input_text")):
        return part, "text"
    return item, "content"


def project_retry_tasks(items: list[dict[str, object]]) -> None:
    """Project only kernel-marked task spans, after selecting the outgoing window."""
    retries: list[tuple[dict[str, object], str, str, TaskPromptSpan, tuple[str, str]]] = []
    retained: set[tuple[str, str]] = set()
    for item in items:
        raw = item.pop(TASK_PROMPT_KEY, None)
        if raw is None or item.get("role") != "user":
            continue
        field, key = _text_field(item)
        text = require_json_string(field.get(key), "task prompt text")
        span = TaskPromptSpan.read(raw)
        identity = (span.run_id, span.task(text))
        if span.retry:
            retries.append((field, key, text, span, identity))
        else:
            retained.add(identity)
    for field, key, text, span, identity in reversed(retries):
        if identity in retained:
            field[key] = text[span.end + 2:]
        retained.add(identity)
