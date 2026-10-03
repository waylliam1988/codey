"""Kernel-owned workspace provenance (trusted identity only).

Trust matrix (locked by tests):

- executor/frame audit, UI/event metadata -> display only, never trusted
- ``TaskSession.executed`` fields -> verified via durable store only
- ``WorkspaceRevisionStore.bump_state`` -> trusted
- durable ledger exact match -> trusted
- kernel side-channel copy -> trusted
- frame ``workspace_identity`` payload -> never trusted on its own;
  frame recovery is safe-replay only and carries no proof

Only this module may write ``_KERNEL_WORKSPACE_ATTR`` or the event
proof attribute (enforced by the architecture test). All recovery entries
go through ``attach_trusted_workspace`` with an already-verified
``TrustedWorkspaceProof``; they never derive trust from display audit or
from event metadata. Event metadata workspace keys stay display/logging
only; hooks adopt only ``event_proof``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from codey.runtime.core.models import ToolResult

# Kernel-owned workspace provenance travels outside the executor-controlled
# audit dict. Executor-provided ``workspace_revision``/``workspace_fingerprint``
# are stripped at the kernel boundary; only ``_with_trusted_workspace_state`` /
# ``attach_trusted_workspace`` may attach the authoritative pair via both
# audit keys (for durable display) and the private side-channel attribute
# read by the event projection.
_KERNEL_WORKSPACE_ATTR = "_kernel_workspace_identity"
_EVENT_PROOF_ATTR = "_kernel_workspace_proof"
_EXECUTOR_STRIPPED_AUDIT_KEYS = frozenset(
    {"workspace_revision", "workspace_fingerprint", "_kernel_workspace_trusted"}
)
# Closed source allowlist: only these origins may produce a proof that
# ``attach_trusted_workspace`` accepts. Unknown/empty sources are rejected.
_TRUSTED_PROOF_SOURCES = frozenset(
    {
        "bump_state",
        "persisted_revision_store",
        "in_memory_kernel_result",
        "event_side_channel",
    }
)
_TRUSTED_PROOF_CAPABILITY = object()

__all__ = [
    "TrustedWorkspaceProof",
    "_EVENT_PROOF_ATTR",
    "_EXECUTOR_STRIPPED_AUDIT_KEYS",
    "_KERNEL_WORKSPACE_ATTR",
    "_TRUSTED_PROOF_SOURCES",
    "_is_trusted_identity",
    "_kernel_workspace_identity_of",
    "_session_workspace_identity",
    "_trusted_workspace_proof",
    "_with_trusted_workspace_state",
    "attach_proof_to_event",
    "attach_trusted_workspace",
    "event_proof",
    "sync_workspace_state_after_edit",
]


@dataclass(frozen=True, init=False)
class TrustedWorkspaceProof:
    """Already-verified workspace provenance (never constructed from audit).

    ``identity`` must be a trusted ``WorkspaceIdentity`` (exact type, not a
    duck-typed ``trusted=True`` stand-in); ``source`` must be in
    ``_TRUSTED_PROOF_SOURCES``. Recovery code cannot build this from two
    raw ints: only verified adapters in the kernel may create it, and
    ``attach_trusted_workspace`` re-validates both fields fail-closed.
    """

    identity: Any
    source: str = ""
    _capability: object = field(default=None, repr=False, compare=False)

    def __init__(self, identity: Any, source: str = "", *, _capability: object = None) -> None:
        if _capability is not _TRUSTED_PROOF_CAPABILITY:
            raise TypeError("TrustedWorkspaceProof must be created by kernel provenance")
        object.__setattr__(self, "identity", identity)
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "_capability", _capability)


def _trusted_workspace_proof(identity: Any, source: str) -> TrustedWorkspaceProof:
    return TrustedWorkspaceProof(identity=identity, source=source, _capability=_TRUSTED_PROOF_CAPABILITY)


def _validated_trusted_proof(proof: Any) -> tuple[Any, str]:
    """Validate the kernel proof object and return its identity/source."""
    from codey.operations.kernel_errors import RecoveryFailed

    if type(proof) is not TrustedWorkspaceProof:
        raise RecoveryFailed("trusted workspace proof has an invalid type")
    if getattr(proof, "_capability", None) is not _TRUSTED_PROOF_CAPABILITY:
        raise RecoveryFailed("trusted workspace proof was not kernel-created")
    try:
        source = getattr(proof, "source", "")
        identity = getattr(proof, "identity", None)
    except Exception as exc:
        raise RecoveryFailed(f"trusted workspace proof unreadable: {exc}") from exc
    if type(source) is not str or source not in _TRUSTED_PROOF_SOURCES:
        raise RecoveryFailed(f"trusted workspace proof invalid source: {source or '?'}")
    if not _is_trusted_identity(identity):
        raise RecoveryFailed("trusted workspace proof invalid: untrusted identity")
    return identity, source


def _is_trusted_identity(identity: Any) -> bool:
    """True only for a real trusted ``WorkspaceIdentity`` (no duck-typing)."""
    try:
        from codey.workspace.revision import WorkspaceIdentity
    except Exception:
        return False
    try:
        if type(identity) is not WorkspaceIdentity:
            return False
        return type(identity.trusted) is bool and identity.trusted is True
    except Exception:
        return False


def _kernel_workspace_identity_of(result: ToolResult) -> Any | None:
    """Return the kernel-attached trusted identity, else None.

    Only the private side-channel set by ``_with_trusted_workspace_state``
    counts, and only when it is a real trusted ``WorkspaceIdentity``.
    Audit-dict workspace keys alone are never trusted because an
    explicit executor can forge them; duck-typed ``trusted=True`` objects
    are rejected by ``_is_trusted_identity``.
    """
    from codey.operations.kernel_errors import RecoveryFailed

    try:
        identity = getattr(result, _KERNEL_WORKSPACE_ATTR, None)
    except Exception as exc:
        raise RecoveryFailed(f"workspace provenance unreadable: {exc}") from exc
    if identity is None:
        return None
    if _is_trusted_identity(identity):
        return identity
    return None


def attach_proof_to_event(event: Any, proof: TrustedWorkspaceProof) -> None:
    """Attach a verified proof to a ``RunEvent`` side-channel (kernel only).

    Event ``metadata`` workspace keys stay display-only; hooks read only
    this attribute via ``event_proof``. Fail-closed: invalid proofs raise
    ``RecoveryFailed`` instead of attaching a forgeable marker.
    """
    from codey.operations.kernel_errors import RecoveryFailed

    _validated_trusted_proof(proof)
    try:
        object.__setattr__(event, _EVENT_PROOF_ATTR, proof)
    except Exception as exc:
        raise RecoveryFailed(f"event proof attach failed: {exc}") from exc


def event_proof(event: Any) -> TrustedWorkspaceProof | None:
    """Return a validated event proof, distinguishing absent from corrupt.

    An event without the private attribute is an ordinary non-kernel event and
    may use the legacy workspace bump path.  Once the attribute exists, any
    malformed proof is an internal recovery failure and must not be silently
    downgraded to that path.
    """
    from codey.operations.kernel_errors import RecoveryFailed

    missing = object()
    try:
        proof = getattr(event, _EVENT_PROOF_ATTR, missing)
    except Exception as exc:
        raise RecoveryFailed(f"event proof unreadable: {exc}") from exc
    if proof is missing:
        return None
    if proof is None:
        raise RecoveryFailed("event proof attribute is present but empty")
    _validated_trusted_proof(proof)
    assert isinstance(proof, TrustedWorkspaceProof)
    return proof


def attach_trusted_workspace(result: ToolResult, proof: TrustedWorkspaceProof) -> ToolResult:
    """Attach an already-verified proof (sole side-channel writer).

    This is the only function that may ``object.__setattr__`` the private
    ``_KERNEL_WORKSPACE_ATTR``. Callers pass a ``TrustedWorkspaceProof``
    produced by a verified adapter (bump, durable store, in-memory copy);
    raw audit ints are never accepted here.
    """
    from codey.operations.kernel_errors import RecoveryFailed

    identity, _source = _validated_trusted_proof(proof)
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
            ok=result.ok, call=result.call,
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
    return attach_trusted_workspace(result, _trusted_workspace_proof(identity, "bump_state"))


def _session_workspace_identity(session: Any) -> tuple[int, str]:
    from codey.workspace.revision import valid_workspace_revision

    try:
        rev = valid_workspace_revision(getattr(session, "workspace_revision", 0))
    except Exception:
        rev = 0
    try:
        fp = str(getattr(session, "workspace_fingerprint", "") or "")
    except Exception:
        fp = ""
    return rev, fp


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
    it without a second bump. A missing store returns an untrusted pair and
    changes neither session nor evidence. There is no guessed revision path.

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
        try:
            session.set_workspace_state(rev, fp)
        except Exception as exc:
            from codey.operations.kernel_errors import RecoveryFailed

            raise RecoveryFailed(f"session workspace state sync failed: {exc}") from exc
        try:
            if execution_evidence is not None and hasattr(execution_evidence, "set_workspace_state"):
                execution_evidence.set_workspace_state(rev, fp)
        except Exception as exc:
            from codey.operations.kernel_errors import RecoveryFailed

            raise RecoveryFailed(f"execution evidence workspace state sync failed: {exc}") from exc
        return rev, fp
    return 0, ""
