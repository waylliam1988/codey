"""Durable state and persisted receipt validation for kernel recovery.

This module owns the two boundaries that must agree before an unsafe result
can be replayed: strict receipt decoding and corroboration by the current
workspace revision store.  The context is per ``execute_turn``; callers may
reuse its first snapshot for batch inspection, but unsafe delivery always
requests a fresh state.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from codey.operations.kernel_errors import RecoveryFailed

__all__ = [
    "RecoveryContext",
    "strict_receipt_bool",
    "strict_receipt_exit_code",
    "strict_receipt_text",
    "verified_persisted_identity",
]


@dataclass
class RecoveryContext:
    """Per-turn durable snapshot with explicit refresh semantics."""

    project_path: Any = None
    revision_store: Any = None
    ignored_paths: tuple[str, ...] = ()
    _cached_state: Any | None = None
    _state_loaded: bool = False

    def current_state(self, *, refresh: bool = False) -> Any | None:
        if self._state_loaded and not refresh:
            return self._cached_state
        if self.revision_store is None or self.project_path is None:
            self._state_loaded = True
            self._cached_state = None
            return None
        try:
            state = self.revision_store.current_state(self.project_path, ignored_paths=self.ignored_paths)
        except Exception:
            state = None
        self._cached_state = state
        self._state_loaded = True
        return state

    def workspace_epoch_stable(self) -> bool:
        """Compare the first loaded state with one fresh read."""
        if not self._state_loaded or self.revision_store is None or self.project_path is None:
            return True
        initial = self._cached_state
        latest = self.current_state(refresh=True)
        if initial is None or latest is None:
            return initial is latest
        try:
            return (
                int(getattr(initial, "revision", 0) or 0),
                str(getattr(initial, "fingerprint", "") or ""),
            ) == (
                int(getattr(latest, "revision", 0) or 0),
                str(getattr(latest, "fingerprint", "") or ""),
            )
        except Exception:
            return False


def verified_persisted_identity(
    record: Mapping[str, Any],
    *,
    project_path: Any = None,
    revision_store: Any = None,
    ignored_paths: Any = (),
    recovery_ctx: RecoveryContext | None = None,
) -> Any | None:
    """Return a persisted identity only when the store corroborates it.

    Unsafe replay deliberately refreshes a supplied context.  The batch guard
    may have loaded an earlier snapshot, but a later guarded delivery must
    observe any bump that happened after that guard.
    """
    try:
        persisted_rev = record.get("workspace_revision", None)
        persisted_fp = record.get("workspace_fingerprint", None)
        from codey.operations.kernel_provenance import _is_trusted_identity
        from codey.workspace.revision import WorkspaceIdentity

        persisted_identity = WorkspaceIdentity.trusted_pair(persisted_rev, persisted_fp)
    except Exception:
        return None
    if persisted_identity is None or not _is_trusted_identity(persisted_identity):
        return None
    try:
        if recovery_ctx is not None:
            current = recovery_ctx.current_state(refresh=True)
        else:
            if revision_store is None or project_path is None:
                return None
            ignores = tuple(str(path) for path in (ignored_paths or ())) if ignored_paths else ()
            current = revision_store.current_state(project_path, ignored_paths=ignores)
    except Exception:
        return None
    if current is None:
        return None
    try:
        current_revision = int(getattr(current, "revision", 0) or 0)
        current_fingerprint = str(getattr(current, "fingerprint", "") or "")
    except Exception:
        return None
    if current_revision != int(persisted_identity.revision) or current_fingerprint != str(
        persisted_identity.fingerprint
    ):
        return None
    return persisted_identity


def strict_receipt_text(record: Mapping[str, Any], field: str, *, default: str) -> str:
    if field not in record:
        return default
    value = record[field]
    if type(value) is not str:
        raise RecoveryFailed(f"persisted receipt {field} must be a string")
    return value


def strict_receipt_bool(record: Mapping[str, Any], field: str, *, default: bool) -> bool:
    if field not in record:
        return default
    value = record[field]
    if type(value) is not bool:
        raise RecoveryFailed(f"persisted receipt {field} must be a boolean")
    return value


def strict_receipt_exit_code(record: Mapping[str, Any]) -> int | None:
    if "exit_code" not in record:
        return None
    value = record["exit_code"]
    if type(value) is not int:
        raise RecoveryFailed("persisted receipt exit_code must be an integer")
    return value
