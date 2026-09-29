"""Kernel-owned workspace provenance (trusted identity only).

Trust matrix (locked by tests):

- executor/frame audit, UI/event metadata -> display only, never trusted
- ``TaskSession.executed`` fields -> verified via durable store only
- ``WorkspaceRevisionStore.bump_state`` -> trusted
- durable ledger exact match -> trusted
- kernel side-channel copy -> trusted
- ``RecoveredToolOutcome.workspace_identity`` (kernel-owned payload)
  verified into ``TrustedWorkspaceProof`` -> trusted

Only this module may write ``_KERNEL_WORKSPACE_ATTR`` (enforced by the
architecture test). All recovery entries go through ``attach_trusted_workspace``
with an already-verified ``TrustedWorkspaceProof``; they never derive trust
from display audit.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import Any

from codey.runtime.core.models import ToolResult

# Kernel-owned workspace provenance travels outside the executor-controlled
# audit dict. Executor-provided ``workspace_revision``/``workspace_fingerprint``
# are stripped at the kernel boundary; only ``with_trusted_workspace_state`` /
# ``attach_trusted_workspace`` may attach the authoritative pair via both
# audit keys (for durable display) and the private side-channel attribute
# read by the event projection.
_KERNEL_WORKSPACE_ATTR = "_kernel_workspace_identity"
_EXECUTOR_STRIPPED_AUDIT_KEYS = frozenset(
    {"workspace_revision", "workspace_fingerprint", "_kernel_workspace_trusted"}
)

__all__ = [
    "TrustedWorkspaceProof",
    "_EXECUTOR_STRIPPED_AUDIT_KEYS",
    "_KERNEL_WORKSPACE_ATTR",
    "_copy_kernel_workspace_provenance",
    "_disk_workspace_fingerprint",
    "_kernel_workspace_identity_of",
    "_session_workspace_identity",
    "_sync_workspace_after_edit",
    "_trusted_workspace_from_result",
    "_with_trusted_workspace_state",
    "attach_trusted_workspace",
    "sync_workspace_state_after_edit",
    "with_trusted_workspace_state",
]


@dataclass(frozen=True)
class TrustedWorkspaceProof:
    """Already-verified workspace provenance (never constructed from audit).

    ``identity`` must be a trusted ``WorkspaceIdentity``; ``source`` names
    the verification origin (``bump_state``, ``persisted_revision_store``,
    ``in_memory_kernel_result``). Recovery code cannot build this from two
    raw ints: only verified adapters in the kernel may create it.
    """

    identity: Any
    source: str = ""


def _kernel_workspace_identity_of(result: ToolResult) -> Any | None:
    """Return the kernel-attached trusted identity, else None.

    Only the private side-channel set by ``_with_trusted_workspace_state``
    counts. Audit-dict workspace keys alone are never trusted because an
    explicit executor can forge them.
    """
    try:
        identity = getattr(result, _KERNEL_WORKSPACE_ATTR, None)
        if identity is None:
            return None
        if bool(getattr(identity, "trusted", False)):
            return identity
    except Exception:
        pass
    return None


def _copy_kernel_workspace_provenance(src: ToolResult, dst: ToolResult) -> None:
    """Carry the kernel side-channel across a ToolResult rebuild."""
    try:
        identity = getattr(src, _KERNEL_WORKSPACE_ATTR, None)
        if identity is not None and bool(getattr(identity, "trusted", False)):
            object.__setattr__(dst, _KERNEL_WORKSPACE_ATTR, identity)
    except Exception:
        pass


def attach_trusted_workspace(result: ToolResult, proof: TrustedWorkspaceProof) -> ToolResult:
    """Attach an already-verified proof (sole side-channel writer).

    This is the only function that may ``object.__setattr__`` the private
    ``_KERNEL_WORKSPACE_ATTR``. Callers pass a ``TrustedWorkspaceProof``
    produced by a verified adapter (bump, durable store, in-memory copy);
    raw audit ints are never accepted here.
    """
    from codey.operations.kernel_errors import RecoveryFailed

    try:
        identity = getattr(proof, "identity", None)
        source = str(getattr(proof, "source", "") or "")
    except Exception as exc:
        raise RecoveryFailed(f"trusted workspace proof unreadable: {exc}") from exc
    if identity is None or not bool(getattr(identity, "trusted", False)):
        raise RecoveryFailed("trusted workspace proof invalid: untrusted identity")
    if not source:
        raise RecoveryFailed("trusted workspace proof invalid: missing source")
    try:
        audit = dict(result.audit) if isinstance(result.audit, dict) else {}
    except Exception as exc:
        raise RecoveryFailed(f"trusted workspace audit unreadable: {exc}") from exc
    try:
        audit = identity.attach_to_audit(audit)
    except Exception as exc:
        raise RecoveryFailed(f"trusted workspace audit attach failed: {exc}") from exc
    try:
        trusted = ToolResult(
            call=result.call,
            model_text=result.model_text,
            truncated=bool(result.truncated),
            presentation=dict(result.presentation) if isinstance(result.presentation, dict) else {},
            audit=audit,
            canonical=dict(result.canonical) if isinstance(result.canonical, dict) else {},
        )
    except Exception as exc:
        raise RecoveryFailed(f"trusted workspace result rebuild failed: {exc}") from exc
    try:
        object.__setattr__(trusted, _KERNEL_WORKSPACE_ATTR, identity)
    except Exception as exc:
        raise RecoveryFailed(f"trusted workspace side-channel attach failed: {exc}") from exc
    return trusted


def _with_trusted_workspace_state(
    result: ToolResult, *, revision: int, fingerprint: str
) -> ToolResult:
    """Attach the authoritative (revision, fingerprint) to one edit result.

    Bump-path helper: validates the pair then delegates to the sole writer
    ``attach_trusted_workspace`` with source ``bump_state``. Only the
    side-channel is trusted downstream; audit keys alone never confer trust.
    Any failure raises ``RecoveryFailed`` so the caller settles as unconfirmed.
    """
    from codey.operations.kernel_errors import RecoveryFailed
    from codey.workspace.revision import WorkspaceIdentity

    try:
        identity = WorkspaceIdentity.trusted_pair(revision, fingerprint)
    except Exception as exc:
        raise RecoveryFailed(f"trusted workspace identity invalid: {exc}") from exc
    if not identity.trusted:
        raise RecoveryFailed("trusted workspace identity invalid: untrusted pair")
    return attach_trusted_workspace(result, TrustedWorkspaceProof(identity=identity, source="bump_state"))


def with_trusted_workspace_state(
    result: ToolResult, *, revision: int, fingerprint: str
) -> ToolResult:
    """Public alias for the trusted attach (fail-closed on failure)."""
    return _with_trusted_workspace_state(result, revision=revision, fingerprint=fingerprint)


def _trusted_workspace_from_result(result: ToolResult) -> tuple[int, str]:
    """Strict kernel provenance: side-channel only, never raw audit."""
    try:
        identity = _kernel_workspace_identity_of(result)
        if identity is not None:
            return int(identity.revision), str(identity.fingerprint)
    except Exception:
        pass
    return 0, ""


def _session_workspace_identity(session: Any) -> tuple[int, str]:
    try:
        rev = int(getattr(session, "workspace_revision", 0) or 0)
    except Exception:
        rev = 0
    try:
        fp = str(getattr(session, "workspace_fingerprint", "") or "")
    except Exception:
        fp = ""
    return rev, fp


def _disk_workspace_fingerprint(project_path: Any, *, ignored_paths: Any = ()) -> str:
    """Real post-edit file identity; empty when it cannot be observed."""
    try:
        if project_path is None:
            return ""
        from pathlib import Path

        from codey.workspace.revision import workspace_fingerprint

        candidate = Path(str(project_path)).expanduser() if not isinstance(project_path, Path) else project_path
        try:
            if not candidate.is_dir():
                return ""
        except Exception:
            return ""
        try:
            ignores = tuple(str(p) for p in (ignored_paths or ())) if ignored_paths else ()
        except Exception:
            ignores = ()
        return str(workspace_fingerprint(candidate, ignored_paths=ignores) or "")
    except Exception:
        return ""


def sync_workspace_state_after_edit(
    session: Any,
    project_path: Any,
    execution_evidence: Any = None,
    *,
    ignored_paths: Any = (),
    revision_store: Any = None,
) -> tuple[int, str]:
    """Sync one authoritative post-edit WorkspaceState to session+evidence.

    Returns a kernel-owned :class:`WorkspaceIdentity` as a ``(revision,
    fingerprint)`` tuple: a fully valid pair is trusted, ``(0, "")`` is
    explicitly untrusted and must never be emitted as new evidence.

    When ``revision_store`` is supplied, the durable ``bump_state`` is the
    single authority (one atomic scan inside the store lock) and the trusted
    pair is returned so result events can carry it to hooks; hooks must adopt
    it without a second bump. Otherwise only the observed fingerprint is
    aligned and the outer hooks bump owns the revision (compat path retained
    for tests only: it scans once here for session alignment and hooks scan
    again for the revision; production must pass a durable store to avoid the
    double scan; the no-store case never emits trusted state).

    A supplied store that fails (exception, invalid revision, or empty
    fingerprint) never falls back to the session-guess path: the session
    keeps its prior identity so the old revision is never paired with the
    new fingerprint as new evidence.
    """
    try:
        ignores = tuple(str(p) for p in (ignored_paths or ())) if ignored_paths else ()
    except Exception:
        ignores = ()
    if revision_store is not None and project_path is not None:
        try:
            state = revision_store.bump_state(project_path, ignored_paths=ignores)
        except Exception:
            return 0, ""
        try:
            from codey.workspace.revision import WorkspaceIdentity
        except Exception:
            return 0, ""
        identity = WorkspaceIdentity.trusted_pair(
            getattr(state, "revision", 0), getattr(state, "fingerprint", "")
        )
        if not identity.trusted:
            return 0, ""
        rev, fp = int(identity.revision), str(identity.fingerprint)
        with contextlib.suppress(Exception):
            session.set_workspace_state(rev, fp)
        try:
            if execution_evidence is not None and hasattr(execution_evidence, "set_workspace_state"):
                execution_evidence.set_workspace_state(rev, fp)
        except Exception:
            pass
        return rev, fp
    _sync_workspace_after_edit(session, project_path, execution_evidence, ignored_paths=ignores)
    return 0, ""


def _sync_workspace_after_edit(
    session: Any,
    project_path: Any,
    execution_evidence: Any = None,
    *,
    ignored_paths: Any = (),
) -> None:
    """Write the same real WorkspaceState to session and outer evidence.

    Called after a confirmed edit result, before any later run receipt.
    The revision counter itself is owned by the outer
    WorkspaceRevisionStore (hooks bump exactly once); here we only sync the
    observed file fingerprint so verification never carries the stale
    pre-edit identity. No inference from the startup-cached fingerprint,
    no second revision bump. ``ignored_paths`` must match the outer store
    config so both scans describe the same files.
    """
    try:
        ignores = tuple(str(p) for p in (ignored_paths or ())) if ignored_paths else ()
    except Exception:
        ignores = ()
    try:
        fp = _disk_workspace_fingerprint(project_path, ignored_paths=ignores)
    except Exception:
        fp = ""
    if not fp:
        return
    try:
        rev = int(getattr(session, "workspace_revision", 0) or 0) or 1
    except Exception:
        rev = 1
    with contextlib.suppress(Exception):
        session.set_workspace_state(rev, fp)
    try:
        if execution_evidence is not None and hasattr(execution_evidence, "set_workspace_state"):
            try:
                outer_rev = int(getattr(execution_evidence, "workspace_revision", 0) or 0) or rev
            except Exception:
                outer_rev = rev
            if outer_rev < rev:
                outer_rev = rev
            execution_evidence.set_workspace_state(outer_rev, fp)
    except Exception:
        pass
