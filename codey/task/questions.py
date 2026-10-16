"""The shared, bounded contract for an explicit user question."""
from __future__ import annotations

from typing import Any


def question_args(value: dict[str, Any]) -> dict[str, Any]:
    question = value.get("question")
    options = value.get("options")
    if not isinstance(question, str) or not 0 < len(question.strip()) <= 2000:
        raise ValueError("question must be nonempty text of at most 2000 characters")
    if not isinstance(options, list) or not 2 <= len(options) <= 6 or any(
        not isinstance(option, str) or not 0 < len(option.strip()) <= 300 for option in options
    ) or len(set(options)) != len(options):
        raise ValueError("question requires 2-6 distinct text options")
    descriptions = value.get("descriptions", [])
    if not isinstance(descriptions, list) or len(descriptions) > len(options) or any(
        not isinstance(description, str) or len(description) > 500 for description in descriptions
    ):
        raise ValueError("invalid question descriptions")
    free = value.get("allow_free_text", True)
    if type(free) is not bool:
        raise ValueError("allow_free_text must be a boolean")
    return {"question": question, "options": list(options), "descriptions": descriptions, "allow_free_text": free}
