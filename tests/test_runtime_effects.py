from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codey.runtime.effects import (
    KIND,
    PHASE_ACCEPTED,
    PHASE_COMPLETION_PROOF_RECORDED,
    PHASE_REPAIR_CONTEXT_ADMITTED,
    PHASE_REPAIR_RUNNING,
    PHASE_REPAIR_SETTLED,
    PHASE_TERMINAL,
    PHASE_WRITER_RUNNING,
    PHASE_WRITER_SETTLED,
    RuntimeOperationStore,
    RuntimeOperationState,
    RuntimeOperationTransitionError,
    SCHEMA_VERSION,
    mark_completion_blocked,
    mark_completion_proof_recorded,
    mark_repair_context_admitted,
    mark_repair_running,
    mark_repair_settled,
    mark_terminal,
    mark_writer_running,
    mark_writer_settled,
    operation_progress_text,
)
from codey.runtime.reducer import reduce_session
from codey.runtime.session_log import RuntimeSessionLog

PROOF_OK = "completion_proof:0123456789abcdef"
PROOF_FAIL = "completion_proof:fedcba9876543210"
PROOF_BLOCKED = "completion_proof:1111111111111111"
CONTEXT_REF = "sha256:" + "a" * 64


def _state(phase: str, **overrides: object) -> RuntimeOperationState:
    values = {
        "session_id": "s1",
        "run_id": "run-1",
        "operation_id": "task:" + "1" * 24,
        "lane": "run:" + "2" * 24,
        "project_ref": "project:" + "3" * 24,
        "provider_id": "deepseek",
        "turn_budget": 5,
        "max_repair_rounds": 1,
        "phase": phase,
        "started_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:01Z",
        "task_kind": "project",
    }
    values.update(overrides)
    return RuntimeOperationState(**values)


class RuntimeOperationStoreTests(unittest.TestCase):
    def test_runtime_phase_projection_round_trips_from_session_log(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            log = RuntimeSessionLog(Path(td))
            store = RuntimeOperationStore(log)
            state = store.start(
                session_id="s1",
                run_id="run-1",
                project=".",
                provider_id="deepseek",
                turn_budget=5,
                max_repair_rounds=1,
                task_kind="project",
            )
            assert state is not None
            state = store.commit(
                "s1",
                "run-1",
                lambda item: mark_writer_running(item, provider_id="deepseek"),
            )
            assert state is not None
            state = store.commit(
                "s1",
                "run-1",
                lambda item: mark_writer_settled(
                    item,
                    provider_id="deepseek",
                    turns_used=2,
                    stop_reason="done",
                ),
            )
            assert state is not None
            state = store.commit(
                "s1",
                "run-1",
                lambda item: mark_completion_proof_recorded(
                    item,
                    proof_ref="completion_proof:0123456789abcdef",
                    proof_status="complete",
                    proof_satisfied=True,
                ),
            )
            assert state is not None

            loaded = store.load("s1", "run-1")

        assert loaded is not None
        self.assertEqual(loaded.phase, PHASE_COMPLETION_PROOF_RECORDED)
        self.assertEqual(loaded.turns_used, 2)
        self.assertEqual(operation_progress_text(loaded), "Finishing was interrupted")

    def test_terminal_commit_settles_runtime_operation_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            log = RuntimeSessionLog(Path(td))
            store = RuntimeOperationStore(log)
            state = store.start(
                session_id="s1",
                run_id="run-1",
                project="",
                provider_id="deepseek",
                turn_budget=5,
                max_repair_rounds=1,
                task_kind="chat",
            )
            assert state is not None
            state = store.commit(
                "s1",
                "run-1",
                lambda item: mark_terminal(
                    item,
                    stop_reason="done",
                    summary_chars=4,
                    turns=0,
                    max_turns=5,
                    provider="deepseek",
                ),
            )
            projection = reduce_session(log.read("s1"))

        assert state is not None
        self.assertEqual(state.phase, PHASE_TERMINAL)
        self.assertEqual(
            projection.operations[state.operation_id].outcome,
            "completed",
        )
        self.assertEqual(projection.lanes[state.lane].open_operation_id, "")

    def test_terminal_commit_projects_non_done_runtime_outcomes(self) -> None:
        cases = (
            ("stopped", "aborted"),
            ("approval", "suspended"),
            ("blocked", "failed"),
            ("error", "failed"),
        )
        for stop_reason, expected_outcome in cases:
            with self.subTest(stop_reason=stop_reason):
                with tempfile.TemporaryDirectory() as td:
                    log = RuntimeSessionLog(Path(td))
                    store = RuntimeOperationStore(log)
                    state = store.start(
                        session_id="s1",
                        run_id="run-1",
                        project="",
                        provider_id="deepseek",
                        turn_budget=5,
                        max_repair_rounds=1,
                        task_kind="chat",
                    )
                    assert state is not None
                    state = store.commit(
                        "s1",
                        "run-1",
                        lambda item: mark_terminal(
                            item,
                            stop_reason=stop_reason,
                            summary_chars=4,
                            turns=0,
                            max_turns=5,
                            provider="deepseek",
                        ),
                    )
                    assert state is not None
                    projection = reduce_session(log.read("s1"))

                self.assertEqual(
                    projection.operations[state.operation_id].outcome,
                    expected_outcome,
                )

    def test_runtime_phase_payload_is_closed_schema_v1(self) -> None:
        payload = _state(PHASE_ACCEPTED).to_payload()
        self.assertEqual(payload["schema_version"], SCHEMA_VERSION)
        self.assertEqual(payload["kind"], KIND)
        self.assertEqual(RuntimeOperationState.from_payload(payload), _state(PHASE_ACCEPTED))

        for key, value in (
            ("schema_version", 2),
            ("kind", "legacy"),
            ("raw_prompt", "never"),
        ):
            with self.subTest(key=key):
                mutated = dict(payload)
                mutated[key] = value
                self.assertIsNone(RuntimeOperationState.from_payload(mutated))

    def test_runtime_phase_reader_rejects_non_canonical_facts(self) -> None:
        base = mark_completion_proof_recorded(
            _state(PHASE_WRITER_SETTLED, turns_used=1, stop_reason="done"),
            proof_ref=PROOF_FAIL,
            proof_status="failed",
            proof_satisfied=False,
        ).to_payload()
        for key, value in (
            ("project_ref", "E:/raw/project"),
            ("completion_proof_ref", "completion_proof:not-hex"),
            ("completion_proof_status", "running"),
            ("completion_proof_satisfied", 0),
            ("repair_context_ref", "sha256:" + "g" * 64),
            ("provider_id", ""),
            ("task_kind", ""),
        ):
            with self.subTest(key=key):
                mutated = dict(base)
                mutated[key] = value
                self.assertIsNone(RuntimeOperationState.from_payload(mutated))

    def test_runtime_phase_reader_enforces_repair_and_verdict_invariants(self) -> None:
        for status, satisfied in (
            ("complete", True),
            ("complete_with_limitations", False),
            ("blocked", False),
        ):
            with self.subTest(status=status):
                payload = _state(
                    PHASE_REPAIR_RUNNING,
                    completion_proof_ref=PROOF_OK,
                    completion_proof_status=status,
                    completion_proof_satisfied=satisfied,
                    repair_context_ref=CONTEXT_REF,
                    repair_rounds=1,
                    turns_used=1,
                    stop_reason="done",
                ).to_payload()
                self.assertIsNone(RuntimeOperationState.from_payload(payload))

        payload = _state(
            PHASE_COMPLETION_PROOF_RECORDED,
            completion_proof_ref=PROOF_FAIL,
            completion_proof_status="failed",
            completion_proof_satisfied=False,
            repair_context_ref=CONTEXT_REF,
            turns_used=1,
            stop_reason="done",
        ).to_payload()
        self.assertIsNone(RuntimeOperationState.from_payload(payload))

        payload = _state(
            PHASE_REPAIR_RUNNING,
            completion_proof_ref=PROOF_FAIL,
            completion_proof_status="failed",
            completion_proof_satisfied=False,
            repair_context_ref=CONTEXT_REF,
            repair_rounds=1,
            blocked_reason="provider_failure",
            turns_used=1,
            stop_reason="done",
        ).to_payload()
        self.assertIsNone(RuntimeOperationState.from_payload(payload))

    def test_terminal_stop_before_repair_requires_the_admitting_failed_proof(self) -> None:
        for status, satisfied in (
            ("complete", True),
            ("complete_with_limitations", False),
            ("blocked", False),
        ):
            with self.subTest(status=status):
                payload = mark_terminal(
                    _state(PHASE_WRITER_RUNNING),
                    stop_reason="stopped",
                    summary_chars=0,
                    turns=0,
                    max_turns=5,
                    provider="deepseek",
                ).to_payload()
                payload["repair_context_ref"] = CONTEXT_REF
                payload["completion_proof_ref"] = PROOF_OK
                payload["completion_proof_status"] = status
                payload["completion_proof_satisfied"] = satisfied
                self.assertIsNone(RuntimeOperationState.from_payload(payload))

        admitted = mark_repair_context_admitted(
            mark_completion_proof_recorded(
                _state(PHASE_WRITER_SETTLED, turns_used=1, stop_reason="done"),
                proof_ref=PROOF_FAIL,
                proof_status="failed",
                proof_satisfied=False,
            ),
            context_ref=CONTEXT_REF,
        )
        stopped = mark_terminal(
            admitted,
            stop_reason="stopped",
            summary_chars=0,
            turns=1,
            max_turns=5,
            provider="deepseek",
        )
        self.assertEqual(RuntimeOperationState.from_payload(stopped.to_payload()), stopped)

    def test_store_ignores_phase_effects_with_mismatched_state_identity(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            log = RuntimeSessionLog(Path(td))
            store = RuntimeOperationStore(log)
            started = store.start(
                session_id="s1",
                run_id="run-1",
                project=".",
                provider_id="deepseek",
                turn_budget=5,
                max_repair_rounds=1,
                task_kind="project",
            )
            assert started is not None
            poisoned = mark_writer_running(started, provider_id="deepseek")
            payload = poisoned.to_payload()
            payload["operation_id"] = "task:" + "9" * 24
            log.append(
                "s1",
                lane=started.lane,
                operation_id=started.operation_id,
                kind="operation_effect",
                payload={
                    "effect_kind": "run_phase",
                    "ref": "run_phase:writer_running",
                    "state": payload,
                },
            )

            loaded = store.load("s1", "run-1")

        self.assertEqual(loaded, started)

    def test_runtime_phase_reader_and_writer_state_sets_match(self) -> None:
        proofs = (
            ("", "", None),
            (PROOF_OK, "complete", True),
            ("completion_proof:2222222222222222", "complete_with_limitations", False),
            (PROOF_FAIL, "failed", False),
            (PROOF_BLOCKED, "blocked", False),
        )
        reachable = _reachable_state_keys(proofs)
        accepted = _accepted_state_keys(proofs)

        self.assertEqual(accepted - reachable, set())
        self.assertEqual(reachable - accepted, set())


def _state_key(state: RuntimeOperationState) -> tuple[object, ...]:
    return (
        state.phase,
        bool(state.completion_proof_ref),
        state.completion_proof_status,
        state.completion_proof_satisfied,
        state.repair_rounds,
        bool(state.repair_context_ref),
        bool(state.blocked_reason),
        state.terminal is not None,
    )


def _reachable_state_keys(
    proofs: tuple[tuple[str, str, bool | None], ...],
) -> set[tuple[object, ...]]:
    queue = [_state(PHASE_ACCEPTED)]
    seen: set[tuple[object, ...]] = set()
    while queue:
        state = queue.pop(0)
        key = _state_key(state)
        if key in seen:
            continue
        seen.add(key)
        queue.extend(_next_states(state, proofs))
    return seen


def _next_states(
    state: RuntimeOperationState,
    proofs: tuple[tuple[str, str, bool | None], ...],
) -> list[RuntimeOperationState]:
    out: list[RuntimeOperationState] = []

    def add(fn) -> None:
        try:
            out.append(fn(state))
        except RuntimeOperationTransitionError:
            return

    add(lambda item: mark_writer_running(item, provider_id="deepseek"))
    add(
        lambda item: mark_writer_settled(
            item,
            provider_id="deepseek",
            turns_used=1,
            stop_reason="done",
        )
    )
    for ref, status, satisfied in proofs:
        if satisfied is None:
            continue
        add(
            lambda item, ref=ref, status=status, satisfied=satisfied: mark_completion_proof_recorded(
                item,
                proof_ref=ref,
                proof_status=status,
                proof_satisfied=satisfied,
            )
        )
    add(lambda item: mark_completion_blocked(item, reason="blocked"))
    add(lambda item: mark_repair_context_admitted(item, context_ref=CONTEXT_REF))
    add(lambda item: mark_repair_running(item, provider_id="deepseek"))
    add(lambda item: mark_repair_settled(item, provider_id="deepseek", stop_reason="done"))
    add(
        lambda item: mark_repair_settled(
            item,
            provider_id="deepseek",
            stop_reason="",
            blocked_reason="provider_failure",
        )
    )
    add(
        lambda item: mark_terminal(
            item,
            stop_reason="done",
            summary_chars=1,
            turns=1,
            max_turns=5,
            provider="deepseek",
        )
    )
    add(
        lambda item: mark_terminal(
            item,
            stop_reason="done",
            summary_chars=1,
            turns=1,
            max_turns=5,
            provider="deepseek",
            blocked_reason="blocked",
        )
    )
    return out


def _accepted_state_keys(
    proofs: tuple[tuple[str, str, bool | None], ...],
) -> set[tuple[object, ...]]:
    accepted: set[tuple[object, ...]] = set()
    for phase in (
        PHASE_ACCEPTED,
        PHASE_WRITER_RUNNING,
        PHASE_WRITER_SETTLED,
        PHASE_COMPLETION_PROOF_RECORDED,
        PHASE_REPAIR_CONTEXT_ADMITTED,
        PHASE_REPAIR_RUNNING,
        PHASE_REPAIR_SETTLED,
        PHASE_TERMINAL,
    ):
        for ref, status, satisfied in proofs:
            for rounds in (0, 1):
                for context_ref in ("", CONTEXT_REF):
                    for verdict in ("", "blocked"):
                        turns = 0 if phase in (PHASE_ACCEPTED, PHASE_WRITER_RUNNING) else 1
                        stop = "" if phase in (PHASE_ACCEPTED, PHASE_WRITER_RUNNING) else "done"
                        payload = _state(
                            phase,
                            turns_used=turns,
                            stop_reason=stop,
                            completion_proof_ref=ref,
                            completion_proof_status=status,
                            repair_rounds=rounds,
                            repair_context_ref=context_ref,
                            blocked_reason=verdict,
                        ).to_payload()
                        if satisfied is not None:
                            payload["completion_proof_satisfied"] = satisfied
                        if phase == PHASE_TERMINAL:
                            payload["terminal"] = {
                                "stop_reason": "done",
                                "summary_chars": 1,
                                "turns": 1,
                                "max_turns": 5,
                                "provider": "deepseek",
                                "blocked_reason": verdict,
                                "finished_at": "2026-01-01T00:00:02Z",
                            }
                        loaded = RuntimeOperationState.from_payload(payload)
                        if loaded is not None:
                            accepted.add(_state_key(loaded))
    return accepted


if __name__ == "__main__":
    unittest.main()
