"""Small passive health controller for web providers."""

from __future__ import annotations

import contextlib
import secrets
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from codey.providers.diagnostics import (
    FAILURE_AUTHENTICATION_REQUIRED,
    FAILURE_CHALLENGE_REQUIRED,
    FAILURE_CONTROL_MISSING,
    FAILURE_RATE_LIMITED,
    FAILURE_READINESS_STALE,
    FAILURE_RESPONSE_MISSING,
    FAILURE_SUBMISSION_UNCERTAIN,
    FAILURE_TRANSIENT,
    ProviderFailure,
)
from codey.providers.ids import normalize_provider_id
from codey.providers.timeouts import remaining, start_deadline
from codey.runtime.core import cancellation
from codey.storage.local_store import (
    StoreCorruption,
    backup_corrupt_file,
    read_json_strict,
    write_json_atomic,
)

STATE_UNKNOWN = "unknown"
STATE_HEALTHY = "healthy"
STATE_DEGRADED = "degraded"
STATE_OPEN = "open"
STATE_AUTH_REQUIRED = "auth_required"
VALID_STATES = {
    STATE_UNKNOWN,
    STATE_HEALTHY,
    STATE_DEGRADED,
    STATE_OPEN,
    STATE_AUTH_REQUIRED,
}
STRUCTURAL_FAILURES = {
    FAILURE_CONTROL_MISSING,
    FAILURE_RESPONSE_MISSING,
    FAILURE_READINESS_STALE,
}
AUTH_FAILURES = {FAILURE_AUTHENTICATION_REQUIRED, FAILURE_CHALLENGE_REQUIRED}
MAX_HEALTH_BYTES = 64 * 1024
MAX_PROVIDERS = 16
STRUCTURAL_THRESHOLD = 2
TRANSIENT_THRESHOLD = 3
STRUCTURAL_COOLDOWN = 300.0
TRANSIENT_COOLDOWN = 90.0
RATE_LIMIT_COOLDOWN = 300.0
CANARY_TIMEOUT = 45.0


@dataclass(frozen=True)
class ProviderHealth:
    state: str = STATE_UNKNOWN
    consecutive_failures: int = 0
    last_failure_kind: str = ""
    last_success_at: float = 0.0
    last_failure_at: float = 0.0
    circuit_open_until: float = 0.0
    success_count: int = 0
    failure_count: int = 0


class ProviderSupervisor:
    """Persist bounded health facts without running background work.

    Every state change is one atomic read-modify-write: the latest disk
    state is loaded, a single success/failure/expiry event is applied, and
    the result is written back while holding the in-process lock and the
    file lock in one fixed order. Two instances sharing a directory
    therefore accumulate each other's events instead of overwriting them
    with precomputed snapshots.
    """

    def __init__(
        self,
        state_home: str | Path | None = None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = Path(state_home) / "provider-health.json" if state_home else None
        self.clock = clock
        self._lock = threading.RLock()
        self._last_save_error = ""
        self._health = self._load()

    @property
    def last_save_error(self) -> str:
        """Last disk persistence failure, surfaced via provider status."""
        with self._lock:
            return self._last_save_error

    @contextlib.contextmanager
    def _exclusive(self):
        """Hold the in-process lock and the file lock in one fixed order.

        All reads that must see fresh cross-instance state (get/select)
        and all read-modify-write updates go through here.
        """
        from codey.storage.file_lock import with_file_lock

        with self._lock:
            if self.path is None:
                yield
            else:
                with with_file_lock(self.path):
                    yield

    def _refresh_locked(self) -> None:
        """Reload disk truth. Caller holds _exclusive()."""
        if self.path is not None:
            self._health = self._load()

    def _write_locked(self) -> None:
        """Persist memory truth. Caller holds _exclusive()."""
        if self.path is None:
            return
        try:
            bounded = dict(sorted(self._health.items())[:MAX_PROVIDERS])
            self._health = bounded
            providers = {
                provider_id: asdict(health)
                for provider_id, health in bounded.items()
            }
            write_json_atomic(
                self.path,
                {"schema_version": 1, "providers": providers},
                max_bytes=MAX_HEALTH_BYTES,
            )
        except (OSError, ValueError) as exc:
            self._last_save_error = f"{type(exc).__name__}: {exc}"[:200]
        else:
            self._last_save_error = ""

    def _update(
        self,
        provider_id: str,
        mutate: Callable[[str, ProviderHealth, float], ProviderHealth],
    ) -> ProviderHealth:
        """Apply one event to the latest disk state and write it back."""
        key = normalize_provider_id(provider_id)
        with self._exclusive():
            self._refresh_locked()
            now = self.clock()
            current = self._health.get(key, ProviderHealth())
            expired = _expire_open(current, now)
            if expired is not None:
                current = expired
            updated = mutate(key, current, now)
            self._health[key] = updated
            self._write_locked()
            return updated

    def get(self, provider_id: str) -> ProviderHealth:
        key = normalize_provider_id(provider_id)
        with self._exclusive():
            # Cross-instance freshness: never answer from a stale cache.
            self._refresh_locked()
            health = self._health.get(key, ProviderHealth())
            expired = _expire_open(health, self.clock())
            if expired is not None:
                self._health[key] = expired
                self._write_locked()
                return expired
            return health

    def is_available(self, provider_id: str) -> bool:
        return self.get(provider_id).state not in {STATE_OPEN, STATE_AUTH_REQUIRED}

    def needs_canary(self, provider_id: str) -> bool:
        health = self.get(provider_id)
        return health.state == STATE_DEGRADED and bool(health.last_failure_kind)

    def prepare_user_selected(self, provider_id: str) -> ProviderHealth:
        """Allow an explicit user retry to verify that login/challenge was cleared."""

        def _allow(_key: str, current: ProviderHealth, _now: float) -> ProviderHealth:
            if current.state != STATE_AUTH_REQUIRED:
                return current
            return replace(current, state=STATE_DEGRADED, circuit_open_until=0.0)

        return self._update(provider_id, _allow)

    def allows_revival(self, provider_id: str) -> bool:
        health = self.get(provider_id)
        return (
            health.state == STATE_OPEN
            and health.last_failure_kind in STRUCTURAL_FAILURES
        )

    def record_success(self, provider_id: str, *, canary: bool = False) -> ProviderHealth:
        return self._update(
            provider_id,
            lambda _key, current, now: _apply_success(current, canary=canary, now=now),
        )

    def record_failure(self, provider_id: str, failure: ProviderFailure) -> ProviderHealth:
        return self._update(
            provider_id,
            lambda _key, current, now: _apply_failure(current, failure, now=now),
        )

    def record_canary_failure(
        self,
        provider_id: str,
        failure: ProviderFailure,
    ) -> ProviderHealth:
        """A failed half-open probe immediately reopens its circuit.

        One atomic event: the failure and the forced reopen share a single
        base state and a single counter increment.
        """
        return self._update(
            provider_id,
            lambda _key, current, now: _apply_canary_failure(current, failure, now=now),
        )

    def select(
        self,
        preferred: str,
        provider_ids: Iterable[str],
        *,
        excluded: Iterable[str] = (),
    ) -> str | None:
        # Decide from one fresh snapshot: refresh under both locks (with
        # expiry persisted) so routing never uses a stale in-memory cache,
        # then compute without holding the locks.
        with self._exclusive():
            self._refresh_locked()
            now = self.clock()
            changed = False
            for provider_id, health in list(self._health.items()):
                expired = _expire_open(health, now)
                if expired is not None:
                    self._health[provider_id] = expired
                    changed = True
            if changed:
                self._write_locked()
            snapshot = dict(self._health)
        blocked = {normalize_provider_id(item) for item in excluded}
        ordered = [normalize_provider_id(preferred)]
        ordered.extend(normalize_provider_id(item) for item in provider_ids)
        seen: set[str] = set()
        for provider_id in ordered:
            if not provider_id or provider_id in seen or provider_id in blocked:
                continue
            seen.add(provider_id)
            health = snapshot.get(provider_id, ProviderHealth())
            if health.state == STATE_OPEN and health.circuit_open_until <= now:
                health = replace(health, state=STATE_DEGRADED, circuit_open_until=0.0)
            if health.state not in {STATE_OPEN, STATE_AUTH_REQUIRED}:
                return provider_id
        return None

    def _load(self) -> dict[str, ProviderHealth]:
        if self.path is None:
            return {}
        try:
            payload = read_json_strict(self.path, max_bytes=MAX_HEALTH_BYTES) or {}
        except StoreCorruption:
            backup_corrupt_file(self.path)
            return {}
        records = payload.get("providers")
        if not isinstance(records, dict):
            return {}
        health: dict[str, ProviderHealth] = {}
        for raw_id, raw in list(records.items())[:MAX_PROVIDERS]:
            provider_id = normalize_provider_id(raw_id)
            if not provider_id or not isinstance(raw, dict):
                continue
            try:
                state = str(raw.get("state") or STATE_UNKNOWN)
                if state not in VALID_STATES:
                    continue
                health[provider_id] = ProviderHealth(
                    state=state,
                    consecutive_failures=max(0, int(raw.get("consecutive_failures") or 0)),
                    last_failure_kind=str(raw.get("last_failure_kind") or "")[:40],
                    last_success_at=max(0.0, float(raw.get("last_success_at") or 0.0)),
                    last_failure_at=max(0.0, float(raw.get("last_failure_at") or 0.0)),
                    circuit_open_until=max(0.0, float(raw.get("circuit_open_until") or 0.0)),
                    success_count=max(0, int(raw.get("success_count") or 0)),
                    failure_count=max(0, int(raw.get("failure_count") or 0)),
                )
            except (TypeError, ValueError):
                continue
        return health

def _expire_open(health: ProviderHealth, now: float) -> ProviderHealth | None:
    """An expired OPEN cools to DEGRADED; None when no transition applies."""
    if health.state == STATE_OPEN and health.circuit_open_until <= now:
        return replace(health, state=STATE_DEGRADED, circuit_open_until=0.0)
    return None


def _apply_success(
    current: ProviderHealth,
    *,
    canary: bool,
    now: float,
) -> ProviderHealth:
    state = STATE_DEGRADED if canary else STATE_HEALTHY
    return replace(
        current,
        state=state,
        consecutive_failures=0,
        last_failure_kind="",
        last_success_at=now,
        circuit_open_until=0.0,
        success_count=current.success_count + 1,
    )


def _apply_failure(
    current: ProviderHealth,
    failure: ProviderFailure,
    *,
    now: float,
) -> ProviderHealth:
    same_family = _failure_family(current.last_failure_kind) == _failure_family(
        failure.kind
    )
    count = current.consecutive_failures + 1 if same_family else 1
    state = STATE_DEGRADED
    open_until = 0.0
    if failure.kind in AUTH_FAILURES:
        state = STATE_AUTH_REQUIRED
    elif failure.kind == FAILURE_RATE_LIMITED:
        state = STATE_OPEN
        open_until = now + RATE_LIMIT_COOLDOWN
    elif failure.kind in STRUCTURAL_FAILURES and count >= STRUCTURAL_THRESHOLD:
        state = STATE_OPEN
        open_until = now + STRUCTURAL_COOLDOWN
    elif failure.kind == FAILURE_TRANSIENT and count >= TRANSIENT_THRESHOLD:
        state = STATE_OPEN
        open_until = now + TRANSIENT_COOLDOWN
    elif failure.kind == FAILURE_SUBMISSION_UNCERTAIN:
        state = STATE_DEGRADED
    return replace(
        current,
        state=state,
        consecutive_failures=count,
        last_failure_kind=failure.kind,
        last_failure_at=now,
        circuit_open_until=open_until,
        failure_count=current.failure_count + 1,
    )


def _apply_canary_failure(
    current: ProviderHealth,
    failure: ProviderFailure,
    *,
    now: float,
) -> ProviderHealth:
    updated = _apply_failure(current, failure, now=now)
    if updated.state in {STATE_OPEN, STATE_AUTH_REQUIRED}:
        return updated
    cooldown = (
        STRUCTURAL_COOLDOWN
        if failure.kind in STRUCTURAL_FAILURES
        else TRANSIENT_COOLDOWN
    )
    return replace(
        updated,
        state=STATE_OPEN,
        circuit_open_until=now + cooldown,
    )


def _failure_family(kind: str) -> str:
    if kind in STRUCTURAL_FAILURES:
        return "structural"
    if kind in AUTH_FAILURES:
        return "auth"
    return kind


def run_half_open_canary(
    provider_id: str,
    provider: object,
    supervisor: ProviderSupervisor,
) -> bool:
    """Probe one cooled-down provider without exposing task or project data."""
    if not supervisor.needs_canary(provider_id):
        return True
    marker = "SESSION_CHECK_" + secrets.token_hex(8).upper()
    prompt = f"Return exactly this marker and nothing else: {marker}"
    deadline = start_deadline(CANARY_TIMEOUT)
    try:
        with cancellation.deadline_scope(deadline):
            provider.new_chat(timeout=remaining(deadline, CANARY_TIMEOUT))
            reply = provider.send(prompt, timeout=remaining(deadline, CANARY_TIMEOUT))
    except cancellation.TaskCancelled:
        raise
    except cancellation.DeadlineExceeded:
        # Explicit classification: a canary that exhausts its own budget is a
        # transient provider signal, not a generic exception. Never let it
        # masquerade as an unexpected failure kind.
        supervisor.record_canary_failure(
            provider_id,
            ProviderFailure(
                provider_id,
                "canary",
                "",
                "",
                "canary budget exhausted",
                "",
                FAILURE_TRANSIENT,
            ),
        )
        return False
    except Exception as exc:
        failure = getattr(exc, "failure", None)
        if not isinstance(failure, ProviderFailure):
            failure = ProviderFailure(
                provider_id,
                "canary",
                "",
                "",
                "canary action failed",
                "",
                FAILURE_TRANSIENT,
            )
        supervisor.record_canary_failure(provider_id, failure)
        return False
    if str(reply or "").strip() != marker:
        supervisor.record_canary_failure(
            provider_id,
            ProviderFailure(
                provider_id,
                "canary",
                "",
                "",
                "canary response mismatch",
                "",
                FAILURE_RESPONSE_MISSING,
            ),
        )
        return False
    supervisor.record_success(provider_id, canary=True)
    return True
