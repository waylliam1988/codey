"""Recovery delivery + slot replay + batch guards (fail-closed tri-state).

Slot control uses the typed ``RecoverySlotResult`` disposition
(``NO_MATCH``/``RECOVERED``/``MISMATCH``/``FAILED``); display
``model_text`` prefixes are never parsed for control. ``NO_MATCH`` means
proceed, ``RECOVERED`` is a verified value ``execute_turn`` may consume,
``MISMATCH`` aborts with mismatch errors, ``FAILED`` aborts with
recovery-failure errors.

All rebuilds funnel through ``kernel_recovery_result`` unified builders;
no entry constructs ``ToolResult`` directly and none derives trust from
display audit. One ``RecoveryContext`` per ``execute_turn`` shares a single
durable workspace snapshot; contexts are never cached across turns.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from codey.operations.kernel_errors import RecoveryFailed
from codey.operations.task_session import turn_effect_id
from codey.runtime.core.models import ToolCall, ToolResult

__all__ = [
    "RecoveryCheckResult",
    "RecoveryContext",
    "RecoverySlotResult",
    "_batch_aborted_results",
    "_batch_recovery_failed_results",
    "_check_batch_recovery",
    "_delivered_slot_result",
    "_guarded_slot_result",
    "_is_recovery_error_text",
    "_is_recovery_failed_text",
    "_is_recovery_mismatch_text",
    "_is_unsafe_tool",
    "_recovery_failed_result",
    "_recovery_mismatch_result",
    "_replay_settled_slot",
    "_same_effect_call",
    "_skip_unsettled",
    "_verified_persisted_identity",
    "apply_recovery_first",
    "delivered_slot_typed",
    "replay_slot_typed",
]


@dataclass(frozen=True)
class RecoveryCheckResult:
    """Tri-state batch pre-check: NO_MATCH / MISMATCH / FAILED."""

    kind: str = "NO_MATCH"
    message: str = ""

    @property
    def matched(self) -> bool:
        return self.kind in ("MISMATCH", "FAILED")

    @property
    def failed(self) -> bool:
        return self.kind == "FAILED"


@dataclass(frozen=True)
class RecoverySlotResult:
    """Typed per-slot recovery control (never parsed from display text)."""

    disposition: str = "NO_MATCH"
    result: ToolResult | None = None


@dataclass
class RecoveryContext:
    """Per-``execute_turn`` durable snapshot (never cached across turns)."""

    project_path: Any = None
    revision_store: Any = None
    ignored_paths: tuple[str, ...] = ()
    _cached_state: Any | None = None
    _state_loaded: bool = False

    def current_state(self) -> Any | None:
        if self._state_loaded:
            return self._cached_state
        self._state_loaded = True
        if self.revision_store is None or self.project_path is None:
            self._cached_state = None
            return None
        try:
            self._cached_state = self.revision_store.current_state(
                self.project_path, ignored_paths=self.ignored_paths
            )
        except Exception:
            self._cached_state = None
        return self._cached_state


def apply_recovery_first(
    session: Any,
    native: bool,
    pending_initial: list[Any],
    prompt: str,
    pending_native_messages: list[dict[str, Any]] | None,
    *,
    provider_session_changed: bool,
    format_results: Any,
    native_tool_messages: Any,
) -> tuple[str, list[dict[str, Any]] | None]:
    """Deliver recovered results before accepting new model tool calls.

    Any formatting or native-message construction failure raises
    :class:`RecoveryFailed`: the caller must terminate as a recovery failure
    without sending the initial prompt. Silently dropping recovered results
    and continuing the model dialogue is forbidden.
    """
    if not pending_initial:
        return prompt, pending_native_messages
    try:
        if provider_session_changed:
            return (
                "Continue the unfinished task using the latest local tool results below.\n\n"
                + format_results(pending_initial, session),
                None,
            )
        if native:
            recovered_messages = native_tool_messages(pending_initial, session)
            if recovered_messages:
                return prompt, recovered_messages
        return (
            "Continue the unfinished task using the latest local tool results below.\n\n"
            + format_results(pending_initial, session),
            pending_native_messages,
        )
    except RecoveryFailed:
        raise
    except Exception as exc:
        raise RecoveryFailed(f"recovery delivery failed: {exc}") from exc


def _is_unsafe_tool(name: str) -> bool:
    """True when a replay without full provenance must fail closed."""
    try:
        from codey.toolchain.tool_spec import spec_for_tool

        spec = spec_for_tool(str(name or "").strip().lower())
    except Exception:
        return True
    if spec is None:
        return True
    return str(getattr(spec, "replay_class", "unsafe") or "unsafe").strip().lower() != "safe"


def _same_effect_call(stored_name: str, stored_digest: str, call: ToolCall) -> bool:
    from codey.operations.kernel_result import _call_args_digest

    name = str(getattr(call, "name", "") or "").strip().lower()
    if str(stored_name or "").strip().lower() != name:
        return False
    if not stored_digest:
        # Legacy records without a digest cannot prove sameness; treat as
        # mismatch for unsafe tools to avoid mis-delivery. Safe reads fall
        # back to re-execution via the caller.
        return False
    return str(stored_digest or "") == _call_args_digest(call)


def _recovery_mismatch_result(call: ToolCall, expected: str, actual: str) -> ToolResult:
    from codey.operations.kernel_recovery_result import build_recovery_mismatch_result

    return build_recovery_mismatch_result(call, expected, actual)


def _recovery_failed_result(call: ToolCall, message: str) -> ToolResult:
    from codey.operations.kernel_recovery_result import build_recovery_error_result

    return build_recovery_error_result(call, message)


def _verified_persisted_identity(
    record: Mapping[str, Any],
    *,
    project_path: Any = None,
    revision_store: Any = None,
    ignored_paths: Any = (),
    recovery_ctx: RecoveryContext | None = None,
) -> Any | None:
    """Verify a persisted unsafe record against the durable revision store.

    Format validation alone is forgeable (``revision=999`` with a well-formed
    fingerprint). The persisted pair is trusted only when a durable
    ``WorkspaceRevisionStore`` corroborates the exact ``(revision,
    fingerprint)`` for the same project. Without a store/project, or on any
    mismatch/read failure, returns ``None`` so the caller fails closed.
    When ``recovery_ctx`` is supplied its cached snapshot is reused so one
    ``execute_turn`` scans once, not once per slot.
    """
    try:
        persisted_rev: Any = record.get("workspace_revision", None)
        persisted_fp: Any = record.get("workspace_fingerprint", None)
        from codey.operations.kernel_provenance import _is_trusted_identity
        from codey.workspace.revision import WorkspaceIdentity

        persisted_identity = WorkspaceIdentity.trusted_pair(persisted_rev, persisted_fp)
    except Exception:
        return None
    if persisted_identity is None or not _is_trusted_identity(persisted_identity):
        return None
    try:
        if recovery_ctx is not None:
            current = recovery_ctx.current_state()
        else:
            if revision_store is None or project_path is None:
                return None
            try:
                ignores = tuple(str(p) for p in (ignored_paths or ())) if ignored_paths else ()
            except Exception:
                ignores = ()
            try:
                current = revision_store.current_state(project_path, ignored_paths=ignores)
            except Exception:
                return None
    except Exception:
        return None
    if current is None:
        return None
    try:
        cur_rev = int(getattr(current, "revision", 0) or 0)
        cur_fp = str(getattr(current, "fingerprint", "") or "")
    except Exception:
        return None
    if cur_rev != int(persisted_identity.revision) or cur_fp != str(persisted_identity.fingerprint):
        return None
    return persisted_identity


def _replay_memory_slot(
    full: Any, call: ToolCall,
) -> RecoverySlotResult:
    """Replay one in-memory settled result (typed)."""
    from codey.operations.kernel_recovery_result import (
        build_recovered_result,
        spec_from_memory_result,
    )

    stored_name = str(getattr(full.call, "name", "") or "")
    try:
        from codey.runtime.effects.effect_records import compute_args_digest as _digest

        stored_digest = str(_digest(full.call.args if isinstance(full.call.args, dict) else {}) or "")
    except Exception:
        stored_digest = ""
    if not _same_effect_call(stored_name, stored_digest, call):
        return RecoverySlotResult(
            disposition="MISMATCH",
            result=_recovery_mismatch_result(call, stored_name, stored_digest),
        )
    call_id = str(getattr(call, "call_id", "") or full.call.call_id or "")
    try:
        want = ToolCall(name=full.call.name, args=dict(full.call.args), call_id=call_id)
    except Exception as exc:
        return RecoverySlotResult(
            disposition="FAILED",
            result=_recovery_failed_result(call, f"settled replay rebuild failed: {exc}"),
        )
    try:
        spec = spec_from_memory_result(full, want)
        return RecoverySlotResult(
            disposition="RECOVERED", result=build_recovered_result(spec)
        )
    except RecoveryFailed as exc:
        return RecoverySlotResult(
            disposition="FAILED", result=_recovery_failed_result(call, str(exc))
        )
    except Exception as exc:
        return RecoverySlotResult(
            disposition="FAILED",
            result=_recovery_failed_result(call, f"settled replay rebuild failed: {exc}"),
        )


def _replay_persisted_unsafe_slot(
    record: Mapping[str, Any],
    call: ToolCall,
    name: str,
    call_id: str,
    *,
    project_path: Any = None,
    revision_store: Any = None,
    ignored_paths: Any = (),
    recovery_ctx: RecoveryContext | None = None,
) -> RecoverySlotResult:
    """Replay one persisted unsafe record after durable verification (typed)."""
    from codey.operations.kernel_provenance import _is_trusted_identity
    from codey.operations.kernel_recovery_result import (
        build_recovered_result,
        spec_from_persisted_record,
    )

    persisted_identity = _verified_persisted_identity(
        record,
        project_path=project_path,
        revision_store=revision_store,
        ignored_paths=ignored_paths,
        recovery_ctx=recovery_ctx,
    )
    if persisted_identity is None or not _is_trusted_identity(persisted_identity):
        try:
            want_err = ToolCall(
                name=str(record.get("name", "") or name),
                args=dict(call.args if isinstance(call.args, dict) else {}),
                call_id=call_id,
            )
        except Exception:
            want_err = call
        return RecoverySlotResult(
            disposition="FAILED",
            result=_recovery_failed_result(
                want_err,
                "persisted unsafe result lacks verified workspace provenance; "
                "refusing to replay as success",
            ),
        )
    try:
        want = ToolCall(
            name=str(record.get("name", "") or name),
            args=dict(call.args if isinstance(call.args, dict) else {}),
            call_id=call_id,
        )
        spec = spec_from_persisted_record(
            record, want, verified_identity=persisted_identity,
        )
        return RecoverySlotResult(
            disposition="RECOVERED", result=build_recovered_result(spec)
        )
    except RecoveryFailed as exc:
        return RecoverySlotResult(
            disposition="FAILED", result=_recovery_failed_result(call, str(exc))
        )
    except Exception as exc:
        return RecoverySlotResult(
            disposition="FAILED",
            result=_recovery_failed_result(call, f"persisted replay rebuild failed: {exc}"),
        )


def replay_slot_typed(
    session: Any,
    identity: str,
    call: ToolCall,
    name: str,
    active_turn: int,
    *,
    project_path: Any = None,
    revision_store: Any = None,
    ignored_paths: Any = (),
    recovery_ctx: RecoveryContext | None = None,
) -> RecoverySlotResult:
    """Typed settled-slot replay (disposition, never text parsing)."""
    ctx = recovery_ctx
    if ctx is None and (project_path is not None or revision_store is not None):
        try:
            ignores = tuple(str(p) for p in (ignored_paths or ())) if ignored_paths else ()
        except Exception:
            ignores = ()
        ctx = RecoveryContext(
            project_path=project_path, revision_store=revision_store, ignored_paths=ignores
        )
    if identity not in (session.executed or {}):
        return RecoverySlotResult(disposition="NO_MATCH", result=None)
    full = session._memory_results.get(identity)
    if full is not None:
        return _replay_memory_slot(full, call)
    record = session.executed[identity]
    stored_name = str(record.get("name", "") or "")
    stored_digest = str(record.get("args_digest", "") or "")
    if not _same_effect_call(stored_name, stored_digest, call):
        return RecoverySlotResult(
            disposition="MISMATCH",
            result=_recovery_mismatch_result(call, stored_name, stored_digest or "unknown-args"),
        )
    call_id = str(getattr(call, "call_id", "") or record.get("call_id", ""))
    was_ok = bool(record.get("ok", False))
    if was_ok and _is_unsafe_tool(name):
        return _replay_persisted_unsafe_slot(
            record, call, name, call_id,
            project_path=project_path, revision_store=revision_store,
            ignored_paths=ignored_paths, recovery_ctx=ctx,
        )
    # Safe success replays the excerpt verbatim (even when it starts with an
    # error-like prefix: disposition stays RECOVERED, never FAILED). Prior
    # failures replay as errors but still count as RECOVERED receipts.
    try:
        want = ToolCall(
            name=str(record.get("name", "") or name),
            args=dict(call.args if isinstance(call.args, dict) else {}),
            call_id=call_id,
        )
    except Exception as exc:
        return RecoverySlotResult(
            disposition="FAILED",
            result=_recovery_failed_result(call, f"settled replay rebuild failed: {exc}"),
        )
    if not was_ok:
        return RecoverySlotResult(
            disposition="RECOVERED",
            result=_recovery_failed_result(
                want, f"already settled in turn {active_turn}; see prior delivery"
            ),
        )
    try:
        excerpt = str(record.get("excerpt", "") or "")
    except Exception as exc:
        return RecoverySlotResult(
            disposition="FAILED",
            result=_recovery_failed_result(call, f"persisted replay unreadable: {exc}"),
        )
    try:
        from codey.operations.kernel_recovery_result import (
            build_recovered_result as _build,
        )
        from codey.operations.kernel_recovery_result import (
            spec_from_persisted_record as _spec_persisted,
        )

        spec = _spec_persisted(
            {"excerpt": excerpt, "name": str(record.get("name", "") or name)},
            want,
            verified_identity=None,
        )
        return RecoverySlotResult(
            disposition="RECOVERED", result=_build(spec)
        )
    except RecoveryFailed as exc:
        return RecoverySlotResult(
            disposition="FAILED", result=_recovery_failed_result(call, str(exc))
        )
    except Exception as exc:
        return RecoverySlotResult(
            disposition="FAILED",
            result=_recovery_failed_result(call, f"persisted replay rebuild failed: {exc}"),
        )


def _replay_settled_slot(
    session: Any,
    identity: str,
    call: ToolCall,
    name: str,
    active_turn: int,
    *,
    project_path: Any = None,
    revision_store: Any = None,
    ignored_paths: Any = (),
    recovery_ctx: RecoveryContext | None = None,
) -> ToolResult | None:
    """Compat wrapper: typed replay, returning the result or None on NO_MATCH."""
    slot = replay_slot_typed(
        session, identity, call, name, active_turn,
        project_path=project_path, revision_store=revision_store,
        ignored_paths=ignored_paths, recovery_ctx=recovery_ctx,
    )
    if slot.disposition == "NO_MATCH":
        return None
    return slot.result


def delivered_slot_typed(
    delivered_map: Mapping[str, ToolResult],
    identity: str,
    call: ToolCall,
) -> RecoverySlotResult:
    """Typed delivered-slot lookup (disposition, never text parsing)."""
    from codey.operations.kernel_recovery_result import (
        build_recovered_result,
        spec_from_delivered_result,
    )

    if identity not in delivered_map:
        return RecoverySlotResult(disposition="NO_MATCH", result=None)
    stored = delivered_map[identity]
    try:
        stored_call = getattr(stored, "call", None)
        stored_name = str(getattr(stored_call, "name", "") or "") if stored_call is not None else ""
        try:
            from codey.runtime.effects.effect_records import compute_args_digest as _d

            stored_args = getattr(stored_call, "args", {}) if stored_call is not None else {}
            stored_digest = str(_d(stored_args if isinstance(stored_args, dict) else {}) or "")
        except Exception:
            stored_digest = ""
    except Exception:
        stored_call, stored_name, stored_digest = None, "", ""
    if stored_call is None or not _same_effect_call(stored_name, stored_digest, call):
        return RecoverySlotResult(
            disposition="MISMATCH",
            result=_recovery_mismatch_result(call, stored_name or "unknown", stored_digest or "unknown-args"),
        )
    try:
        spec = spec_from_delivered_result(stored, call)
        return RecoverySlotResult(
            disposition="RECOVERED", result=build_recovered_result(spec)
        )
    except RecoveryFailed as exc:
        return RecoverySlotResult(
            disposition="FAILED", result=_recovery_failed_result(call, str(exc))
        )
    except Exception as exc:
        return RecoverySlotResult(
            disposition="FAILED",
            result=_recovery_failed_result(call, f"delivered rebuild failed: {exc}"),
        )


def _delivered_slot_result(
    delivered_map: Mapping[str, ToolResult],
    identity: str,
    call: ToolCall,
) -> ToolResult | None:
    """Compat wrapper: typed delivered lookup, result or None on NO_MATCH."""
    slot = delivered_slot_typed(delivered_map, identity, call)
    if slot.disposition == "NO_MATCH":
        return None
    return slot.result


def _is_recovery_mismatch_text(text: object) -> bool:
    """Legacy display helper (kept for external callers; control uses disposition)."""
    return str(text or "").startswith("ERROR: recovery mismatch")


def _is_recovery_failed_text(text: object) -> bool:
    """Legacy display helper (kept for external callers; control uses disposition)."""
    text_str = str(text or "")
    return text_str.startswith("ERROR: recovery failed") or text_str.startswith(
        "ERROR: persisted unsafe result lacks verified workspace provenance"
    )


def _is_recovery_error_text(text: object) -> bool:
    return _is_recovery_mismatch_text(text) or _is_recovery_failed_text(text)


def _check_batch_recovery(
    session: Any,
    calls: list[ToolCall],
    delivered_map: Mapping[str, ToolResult],
    identity_ref: str,
    active_turn: int,
    base_index: int,
    *,
    project_path: Any = None,
    revision_store: Any = None,
    ignored_paths: Any = (),
    recovery_ctx: RecoveryContext | None = None,
) -> RecoveryCheckResult:
    """Pre-check the whole batch; tri-state, no side effects.

    Reads typed ``RecoverySlotResult.disposition``; display text is never
    parsed. ``NO_MATCH``/``RECOVERED`` proceed; ``MISMATCH``/``FAILED`` abort
    without side effects or executor calls.
    """
    try:
        ignores = tuple(str(p) for p in (ignored_paths or ())) if ignored_paths else ()
    except Exception:
        ignores = ()
    ctx = recovery_ctx
    if ctx is None:
        ctx = RecoveryContext(
            project_path=project_path, revision_store=revision_store, ignored_paths=ignores
        )
    for offset, call in enumerate(calls or []):
        name = str(getattr(call, "name", "") or "").strip().lower()
        identity = turn_effect_id(identity_ref or "adhoc", active_turn, base_index + offset)
        try:
            delivered_slot = delivered_slot_typed(delivered_map, identity, call)
        except Exception as exc:
            return RecoveryCheckResult(kind="FAILED", message=f"delivered check failed: {exc}")
        if delivered_slot.disposition == "FAILED":
            text = str(getattr(delivered_slot.result, "model_text", "") or "")
            return RecoveryCheckResult(kind="FAILED", message=text or "delivered check failed")
        if delivered_slot.disposition == "MISMATCH":
            text = str(getattr(delivered_slot.result, "model_text", "") or "")
            return RecoveryCheckResult(kind="MISMATCH", message=text or "delivered mismatch")
        try:
            replayed_slot = replay_slot_typed(
                session, identity, call, name, active_turn,
                project_path=project_path, revision_store=revision_store,
                ignored_paths=ignores, recovery_ctx=ctx,
            )
        except Exception as exc:
            return RecoveryCheckResult(kind="FAILED", message=f"replay check failed: {exc}")
        if replayed_slot.disposition == "FAILED":
            text = str(getattr(replayed_slot.result, "model_text", "") or "")
            return RecoveryCheckResult(kind="FAILED", message=text or "replay check failed")
        if replayed_slot.disposition == "MISMATCH":
            text = str(getattr(replayed_slot.result, "model_text", "") or "")
            return RecoveryCheckResult(kind="MISMATCH", message=text or "replay mismatch")
    return RecoveryCheckResult(kind="NO_MATCH")


def _batch_aborted_results(calls: list[ToolCall]) -> list[ToolResult]:
    """Native-legal errors for every call id when the batch is aborted; no settlement."""
    results: list[ToolResult] = []
    for call in calls or []:
        results.append(_recovery_mismatch_result(call, "batch", "batch-aborted"))
    return results


def _batch_recovery_failed_results(calls: list[ToolCall], message: str) -> list[ToolResult]:
    results: list[ToolResult] = []
    for call in calls or []:
        results.append(_recovery_failed_result(call, message))
    return results


def _skip_unsettled(intent_sink: Any, identity: str, name: str) -> bool:
    if intent_sink is None:
        return False
    from codey.toolchain.tool_spec import spec_for_tool

    spec = spec_for_tool(name)
    return bool(intent_sink.has_unsettled(identity)) and (spec is None or spec.replay_class != "safe")


def _guarded_slot_result(
    session: Any,
    identity: str,
    call: ToolCall,
    name: str,
    active_turn: int,
    intent_sink: Any,
    controller_allowed: Any,
    *,
    project_path: Any = None,
    revision_store: Any = None,
    ignored_paths: Any = (),
    recovery_ctx: RecoveryContext | None = None,
) -> ToolResult | None:
    """Policy/controller/replay guards; None means proceed to real execution.

    A durable ``session.executed`` receipt always wins over a pending intent:
    it replays without re-executing (settlement reconciliation). Only when
    no receipt exists does a pending unsafe intent block execution.
    """
    from codey.operations.kernel_protocol import _policy_allows
    from codey.operations.kernel_result import _error_result

    try:
        replayed_slot = replay_slot_typed(
            session, identity, call, name, active_turn,
            project_path=project_path, revision_store=revision_store,
            ignored_paths=ignored_paths, recovery_ctx=recovery_ctx,
        )
    except Exception as exc:
        return _recovery_failed_result(call, f"replay check failed: {exc}")
    if replayed_slot.disposition != "NO_MATCH":
        return replayed_slot.result
    if _skip_unsettled(intent_sink, identity, name):
        return _error_result(call, f"interrupted {name} not re-executed; see prior intent")
    if not _policy_allows(session.policy, name):
        return _error_result(call, f"disallowed tool for this task policy: {name or '?'}")
    if controller_allowed is not None:
        try:
            from codey.operations.kernel_protocol import _controller_allows as _allows_ctl

            if not _allows_ctl(name, {str(n or "").strip().lower() for n in controller_allowed}):
                return _error_result(call, f"{name} is not allowed by the current controller state")
        except Exception:
            # Fail closed: controller evaluation failure never means unlimited.
            return _error_result(call, "controller state unavailable; cannot authorize tool")
    return None
