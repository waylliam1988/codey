"""Short task-completion receipts built from local facts."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TaskReceipt:
    text: str
    changed_count: int
    checks_passed: bool
    restore_available: bool

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "changed_count": self.changed_count,
            "checks_passed": self.checks_passed,
            "restore_available": self.restore_available,
        }


def _file_count_text(count: int) -> str:
    if count <= 0:
        return "No files changed"
    return f"{count} file{'s' if count != 1 else ''} changed"


def build_task_receipt(changes: dict | None, *, checks_passed: bool = False) -> TaskReceipt:
    changes = changes if isinstance(changes, dict) else {}
    try:
        changed_count = int(changes.get("changed_count") or 0)
    except (TypeError, ValueError):
        changed_count = 0
    changed_count = max(0, changed_count)
    restore_available = changes.get("mode") == "snapshot" and changed_count > 0

    parts = [_file_count_text(changed_count)]
    if checks_passed:
        parts.append("checks passed")
    if restore_available:
        parts.append("restore available")

    return TaskReceipt(
        text=" · ".join(parts),
        changed_count=changed_count,
        checks_passed=bool(checks_passed),
        restore_available=restore_available,
    )
