"""Small passive health controller for web providers."""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Callable, Iterable

from codey.local_store import read_json, write_json_atomic
from codey import cancellation
from codey.provider_diagnostics import (
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
from codey.provider_timeouts import remaining, start_deadline


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
    """Persist bounded health facts without running background work."""

    def __init__(
        self,
        state_home: str | Path | None = None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = Path(state_home) / "provider-health.json" if state_home else None
        self.clock = clock
        self._lock = threading.RLock()
        self._health = self._load()

    def get(self, provider_id: str) -> ProviderHealth:
        with self._lock:
            key = _provider_id(provider_id)
            health = self._health.get(key, ProviderHealth())
            if health.state == STATE_OPEN and health.circuit_open_until <= self.clock():
                health = replace(
                    health,
                    state=STATE_DEGRADED,
                    circuit_open_until=0.0,
                )
                self._health[key] = health
                self._save()
            return health

    def is_available(self, provider_id: str) -> bool:
        return self.get(provider_id).state not in {STATE_OPEN, STATE_AUTH_REQUIRED}

    def needs_canary(self, provider_id: str) -> bool:
        health = self.get(provider_id)
        return health.state == STATE_DEGRADED and bool(health.last_failure_kind)

    def prepare_user_selected(self, provider_id: str) -> ProviderHealth:
        """Allow an explicit user retry to verify that login/challenge was cleared."""
        with self._lock:
            key = _provider_id(provider_id)
            current = self.get(key)
            if current.state != STATE_AUTH_REQUIRED:
                return current
            return self._store(
                key,
                replace(current, state=STATE_DEGRADED, circuit_open_until=0.0),
            )

    def allows_revival(self, provider_id: str) -> bool:
        health = self.get(provider_id)
        return (
            health.state == STATE_OPEN
            and health.last_failure_kind in STRUCTURAL_FAILURES
        )

    def record_success(self, provider_id: str, *, canary: bool = False) -> ProviderHealth:
        with self._lock:
            key = _provider_id(provider_id)
            current = self.get(key)
            state = STATE_DEGRADED if canary else STATE_HEALTHY
            updated = replace(
                current,
                state=state,
                consecutive_failures=0,
                last_failure_kind="",
                last_success_at=self.clock(),
                circuit_open_until=0.0,
                success_count=current.success_count + 1,
            )
            return self._store(key, updated)

    def record_failure(self, provider_id: str, failure: ProviderFailure) -> ProviderHealth:
        with self._lock:
            key = _provider_id(provider_id)
            current = self.get(key)
            now = self.clock()
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
            updated = replace(
                current,
                state=state,
                consecutive_failures=count,
                last_failure_kind=failure.kind,
                last_failure_at=now,
                circuit_open_until=open_until,
                failure_count=current.failure_count + 1,
            )
            return self._store(key, updated)

    def record_canary_failure(
        self,
        provider_id: str,
        failure: ProviderFailure,
    ) -> ProviderHealth:
        """A failed half-open probe immediately reopens its circuit."""
        with self._lock:
            updated = self.record_failure(provider_id, failure)
            if updated.state in {STATE_OPEN, STATE_AUTH_REQUIRED}:
                return updated
            cooldown = (
                STRUCTURAL_COOLDOWN
                if failure.kind in STRUCTURAL_FAILURES
                else TRANSIENT_COOLDOWN
            )
            return self._store(
                _provider_id(provider_id),
                replace(
                    updated,
                    state=STATE_OPEN,
                    circuit_open_until=self.clock() + cooldown,
                ),
            )

    def select(
        self,
        preferred: str,
        provider_ids: Iterable[str],
        *,
        excluded: Iterable[str] = (),
    ) -> str | None:
        with self._lock:
            blocked = {_provider_id(item) for item in excluded}
            ordered = [_provider_id(preferred)]
            ordered.extend(_provider_id(item) for item in provider_ids)
            seen: set[str] = set()
            for provider_id in ordered:
                if not provider_id or provider_id in seen or provider_id in blocked:
                    continue
                seen.add(provider_id)
                if self.is_available(provider_id):
                    return provider_id
            return None

    def _store(self, provider_id: str, health: ProviderHealth) -> ProviderHealth:
        self._health[provider_id] = health
        self._save()
        return health

    def _load(self) -> dict[str, ProviderHealth]:
        if self.path is None:
            return {}
        payload = read_json(self.path, max_bytes=MAX_HEALTH_BYTES) or {}
        records = payload.get("providers")
        if not isinstance(records, dict):
            return {}
        health: dict[str, ProviderHealth] = {}
        for raw_id, raw in list(records.items())[:MAX_PROVIDERS]:
            provider_id = _provider_id(raw_id)
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

    def _save(self) -> None:
        if self.path is None:
            return
        providers = {
            provider_id: asdict(health)
            for provider_id, health in list(sorted(self._health.items()))[:MAX_PROVIDERS]
        }
        try:
            write_json_atomic(
                self.path,
                {"schema_version": 1, "providers": providers},
                max_bytes=MAX_HEALTH_BYTES,
            )
        except (OSError, ValueError):
            pass


def _provider_id(value: object) -> str:
    text = str(value or "").strip().lower()
    return text if text.replace("-", "").replace("_", "").isalnum() else ""


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
