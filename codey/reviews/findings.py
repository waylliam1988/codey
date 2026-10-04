"""Finding normalization, location validation and conservative dedupe."""
from __future__ import annotations

from codey.utils.change_paths import safe_change_path
from codey.workspace.change_set import ChangeSet


def canonical_changed_paths(change_set: ChangeSet | None) -> set[str]:
    if change_set is None:
        return set()
    return set(change_set.changed_paths())


def is_actionable_path(path: str, change_set: ChangeSet | None) -> bool:
    if not isinstance(path, str) or not path.strip():
        return False
    normalized = safe_change_path(path)
    if not normalized:
        return False
    if change_set is None:
        return True
    return change_set.file_for_path(normalized) is not None


def canonical_path(path: str, change_set: ChangeSet | None) -> str:
    normalized = safe_change_path(path)
    if not normalized:
        return ""
    if change_set is None:
        return normalized
    matched = change_set.file_for_path(normalized)
    if matched is None:
        return ""
    return matched.path
