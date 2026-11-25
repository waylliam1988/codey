"""Shared wire fields for whole-answer submission and exact replay."""

from codey.types.json_value import JsonObject

ANSWER_STATES = ("answered", "partial", "unresolved")
MIN_ANSWER_CHARACTERS = 12
MAX_ANSWER_CHARACTERS = 64000
MAX_UPDATES = 16


def answer_fields(states: tuple[str, ...] = ANSWER_STATES) -> dict[str, JsonObject]:
    return {
        "markdown": {"type": "string", "minLength": MIN_ANSWER_CHARACTERS, "maxLength": MAX_ANSWER_CHARACTERS},
        "state": {"type": "string", "enum": list(states)},
    }


def answer_text_and_state(args: JsonObject) -> tuple[str, str]:
    markdown, state = args.get("markdown"), args.get("state")
    if (
        set(args) not in ({"markdown", "state"}, {"markdown", "state", "knowledge_updates"})
        or not isinstance(state, str)
        or state not in ANSWER_STATES
        or not isinstance(markdown, str)
        or not MIN_ANSWER_CHARACTERS <= len(markdown.strip()) <= MAX_ANSWER_CHARACTERS
    ):
        raise ValueError("Submit markdown (12..64000 characters) and state=answered/partial/unresolved.")
    if "knowledge_updates" in args:
        updates = args["knowledge_updates"]
        if (
            not isinstance(updates, list)
            or len(updates) > MAX_UPDATES
            or any(not isinstance(row, dict) for row in updates)
        ):
            raise ValueError(f"knowledge_updates must be a list of at most {MAX_UPDATES} explicit entries")
    return markdown, state
