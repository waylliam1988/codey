"""Post-turn Ghost learning loop for explicit user signals."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from codey.ghost.extractor import GhostSignalExtractor, SignalProvider
from codey.ghost.hebbian import GhostHebbianStore
from codey.ghost.inbox import GhostInboxStore
from codey.ghost.schema import clip_signal_text
from codey.ghost.store import GhostSignalStore


DEFAULT_GHOST_LEARNING_TIMEOUT = 35.0
DEFAULT_GHOST_LEARNING_NEW_CHAT_TIMEOUT = 15.0


class ClosableSignalProvider(SignalProvider, Protocol):
    def new_chat(self, timeout: float | None = None) -> None:
        """Start a clean provider chat if the provider supports it."""

    def close(self) -> None:
        """Release the temporary provider session."""


@dataclass(frozen=True)
class GhostLearningTurn:
    mode: str
    user_text: str
    assistant_text: str = ""
    session_id: str = ""
    run_id: str = ""
    project: str = ""
    provider_id: str = ""


@dataclass(frozen=True)
class GhostLearningResult:
    ok: bool
    skipped_reason: str = ""
    extracted_count: int = 0
    signal_audit_written: bool = False
    candidates_changed: int = 0
    accepted_count: int = 0
    reinforced_count: int = 0
    diagnostics: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_event(self, *, run_id: str, session_id: str) -> dict[str, object]:
        return {
            "type": "ghost_learning_done",
            "run_id": clip_signal_text(run_id, 120),
            "session_id": clip_signal_text(session_id, 120),
            "ok": self.ok,
            "skipped_reason": self.skipped_reason,
            "extracted_count": self.extracted_count,
            "signal_audit_written": self.signal_audit_written,
            "candidates_changed": self.candidates_changed,
            "accepted_count": self.accepted_count,
            "reinforced_count": self.reinforced_count,
            "diagnostics": list(self.diagnostics),
            "warnings": list(self.warnings),
        }


class GhostLearningLoop:
    def __init__(
        self,
        *,
        signal_store: GhostSignalStore | None,
        inbox_store: GhostInboxStore | None,
        hebbian_store: GhostHebbianStore | None,
        extractor: GhostSignalExtractor | None = None,
    ) -> None:
        self.signal_store = signal_store
        self.inbox_store = inbox_store
        self.hebbian_store = hebbian_store
        self.extractor = extractor or GhostSignalExtractor()

    def learn_from_turn(
        self,
        turn: GhostLearningTurn,
        *,
        provider_factory: Callable[[str], SignalProvider] | None,
        timeout: float = DEFAULT_GHOST_LEARNING_TIMEOUT,
        new_chat_timeout: float = DEFAULT_GHOST_LEARNING_NEW_CHAT_TIMEOUT,
    ) -> GhostLearningResult:
        if self.signal_store is None or self.inbox_store is None or self.hebbian_store is None:
            return GhostLearningResult(True, skipped_reason="ghost_store_disabled")
        if not self.inbox_store.learning_enabled():
            return GhostLearningResult(True, skipped_reason="learning_disabled")
        if provider_factory is None:
            return GhostLearningResult(True, skipped_reason="provider_factory_missing")
        if not str(turn.user_text or "").strip():
            return GhostLearningResult(True, skipped_reason="empty_user_text")

        provider: SignalProvider | None = None
        try:
            provider = provider_factory(turn.provider_id)
            _start_provider_chat(provider, timeout=new_chat_timeout)
            result = self.extractor.extract(
                provider=provider,
                user_text=turn.user_text,
                assistant_text=turn.assistant_text,
                provider_id=turn.provider_id,
                timeout=timeout,
            )
            audit_written = self.signal_store.append_extraction(
                result,
                session_id=turn.session_id,
                run_id=turn.run_id,
                project=turn.project,
            )
            diagnostics = _bounded_items(result.diagnostics)
            if not audit_written:
                return GhostLearningResult(
                    False,
                    skipped_reason="signal_audit_failed",
                    extracted_count=len(result.signals),
                    signal_audit_written=False,
                    diagnostics=diagnostics,
                )
            if not result.ok:
                return GhostLearningResult(
                    False,
                    skipped_reason="extractor_failed",
                    extracted_count=len(result.signals),
                    signal_audit_written=True,
                    diagnostics=diagnostics,
                )
            if not result.signals:
                return GhostLearningResult(
                    True,
                    skipped_reason="no_signal",
                    extracted_count=0,
                    signal_audit_written=True,
                    diagnostics=diagnostics,
                )
            candidates = self.inbox_store.ingest_signals(
                result,
                session_id=turn.session_id,
                run_id=turn.run_id,
                project=turn.project,
                user_text=turn.user_text,
            )
            if not candidates:
                return GhostLearningResult(
                    True,
                    skipped_reason="no_candidate_change",
                    extracted_count=len(result.signals),
                    signal_audit_written=True,
                    diagnostics=diagnostics,
                    warnings=_bounded_items(self.inbox_store.last_warnings),
                )
            sync_results = self.hebbian_store.sync_from_inbox(self.inbox_store)
            reinforced_count = sum(1 for item in sync_results if item.applied and item.node is not None)
            return GhostLearningResult(
                True,
                extracted_count=len(result.signals),
                signal_audit_written=True,
                candidates_changed=len(candidates),
                accepted_count=sum(1 for item in candidates if item.status == "accepted"),
                reinforced_count=reinforced_count,
                diagnostics=diagnostics,
                warnings=_bounded_items((
                    *self.inbox_store.last_warnings,
                    *getattr(self.hebbian_store, "last_warnings", ()),
                )),
            )
        except Exception as exc:
            return GhostLearningResult(
                False,
                skipped_reason="learning_error",
                diagnostics=(f"{type(exc).__name__}: {clip_signal_text(exc, 160)}",),
            )
        finally:
            _close_provider(provider)


def _start_provider_chat(provider: SignalProvider, *, timeout: float) -> None:
    new_chat = getattr(provider, "new_chat", None)
    if callable(new_chat):
        new_chat(timeout=timeout)


def _close_provider(provider: SignalProvider | None) -> None:
    close = getattr(provider, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def _bounded_items(items: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    out: list[str] = []
    for item in items:
        text = clip_signal_text(item, 200)
        if text:
            out.append(text)
        if len(out) >= 8:
            break
    return tuple(out)


__all__ = [
    "DEFAULT_GHOST_LEARNING_NEW_CHAT_TIMEOUT",
    "DEFAULT_GHOST_LEARNING_TIMEOUT",
    "GhostLearningLoop",
    "GhostLearningResult",
    "GhostLearningTurn",
]
