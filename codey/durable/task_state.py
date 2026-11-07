"""Pure checkpoint ownership transformations shared by durable operations."""

from collections.abc import Mapping


def release_attempt_state(state: Mapping[str, object]) -> dict[str, object]:
    """Clear volatile scheduler ownership while preserving logical state."""
    released = dict(state)
    released["attempt_token"] = ""
    released["attempt_owner"] = ""
    released["attempt_process_id"] = 0
    return released
