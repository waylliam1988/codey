"""RunOperationStore: durable program counter for one run (0.5.1)."""

from __future__ import annotations

import ast
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from codey import run_operation as run_operation_module
from codey.run_operation import (
    KIND,
    MAX_OPERATION_BYTES,
    PHASE_ACCEPTED,
    PHASE_COMPLETION_PROOF_RECORDED,
    PHASE_REPAIR_CONTEXT_ADMITTED,
    PHASE_REPAIR_RUNNING,
    PHASE_REPAIR_SETTLED,
    PHASE_TERMINAL,
    PHASE_WRITER_RUNNING,
    PHASE_WRITER_SETTLED,
    SCHEMA_VERSION,
    RunOperationState,
    RunOperationStore,
    RunOperationTransitionError,
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


SESSION = "session-op"
RUN = "run-op-1"

ROOT = Path(__file__).resolve().parents[1]


def _store(td: Path) -> RunOperationStore:
    return RunOperationStore(Path(td) / "state")


def _start(store: RunOperationStore) -> RunOperationState:
    started = store.start(
        session_id=SESSION,
        run_id=RUN,
        project="/tmp/project",
        provider_id="deepseek",
        turn_budget=8,
        max_repair_rounds=1,
    )
    assert started is not None
    return started


def _drive(store: RunOperationStore, *transitions) -> RunOperationState:
    """Commit each transition in order and return the final durable state."""

    state = _start(store)
    for transition in transitions:
        state = store.commit(SESSION, RUN, transition)
        assert state is not None
    return state


def _writer_settled(store: RunOperationStore) -> RunOperationState:
    return _drive(
        store,
        lambda state: mark_writer_running(state, provider_id="deepseek"),
        lambda state: mark_writer_settled(state, provider_id="deepseek", turns_used=4, stop_reason="done"),
    )


def _failed_proof(store: RunOperationStore) -> RunOperationState:
    _writer_settled(store)
    next_state = store.commit(
        SESSION,
        RUN,
        lambda state: mark_completion_proof_recorded(
            state,
            proof_ref="completion_proof:fail1",
            proof_status="failed",
            proof_satisfied=False,
        ),
    )
    assert next_state is not None
    return next_state


def _clean_path(store: RunOperationStore) -> RunOperationState:
    """accepted -> terminal with a satisfied proof, the happy path."""

    _writer_settled(store)
    recorded = store.commit(
        SESSION,
        RUN,
        lambda state: mark_completion_proof_recorded(
            state,
            proof_ref="completion_proof:abc123",
            proof_status="complete",
            proof_satisfied=True,
        ),
    )
    assert recorded is not None
    terminal = store.commit(
        SESSION,
        RUN,
        lambda state: mark_terminal(
            state,
            stop_reason="done",
            summary_chars=42,
            turns=4,
            max_turns=8,
            provider="deepseek",
        ),
    )
    assert terminal is not None
    return terminal


def _repair_path(store: RunOperationStore) -> RunOperationState:
    """The full repair arm: proof fails, one bounded round runs, proof passes."""

    state = _failed_proof(store)
    for transition in (
        lambda state: mark_repair_context_admitted(state, context_ref="sha256:" + "a" * 64),
        lambda state: mark_repair_running(state, provider_id="deepseek"),
        lambda state: mark_repair_settled(state, provider_id="deepseek", stop_reason="done", turns_used=6),
        lambda state: mark_completion_proof_recorded(
            state,
            proof_ref="completion_proof:ok2",
            proof_status="complete",
            proof_satisfied=True,
        ),
        lambda state: mark_terminal(
            state,
            stop_reason="done",
            summary_chars=10,
            turns=6,
            max_turns=8,
            provider="deepseek",
        ),
    ):
        next_state = store.commit(SESSION, RUN, transition)
        assert next_state is not None
        state = next_state
    return state


class RoundTripTests(unittest.TestCase):
    def test_clean_path_round_trips_every_phase(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = _store(Path(td))
            state = _clean_path(store)

            reloaded = store.load(SESSION, RUN)
            self.assertIsNotNone(reloaded)
            assert reloaded is not None
            self.assertEqual(reloaded, state)
            self.assertEqual(reloaded.phase, PHASE_TERMINAL)
            self.assertIsNotNone(reloaded.terminal)
            assert reloaded.terminal is not None
            self.assertEqual(reloaded.terminal.stop_reason, "done")
            self.assertEqual(reloaded.terminal.summary_chars, 42)

    def test_repair_path_round_trips_every_phase(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = _store(Path(td))
            state = _repair_path(store)

            reloaded = store.load(SESSION, RUN)
            assert reloaded is not None
            self.assertEqual(reloaded, state)
            self.assertEqual(reloaded.repair_rounds, 1)
            self.assertTrue(reloaded.repair_context_ref.startswith("sha256:"))
            self.assertEqual(reloaded.completion_proof_status, "complete")
            self.assertIs(reloaded.completion_proof_satisfied, True)

    def test_payload_schema_fields_are_stable(self) -> None:
        state = RunOperationState(
            session_id=SESSION,
            run_id=RUN,
            project_ref="/tmp/project",
            provider_id="deepseek",
            turn_budget=8,
            max_repair_rounds=1,
            phase=PHASE_ACCEPTED,
            started_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        payload = state.to_payload()

        self.assertEqual(payload["schema_version"], SCHEMA_VERSION)
        self.assertEqual(payload["kind"], KIND)
        self.assertEqual(RunOperationState.from_payload(payload), state)


class FailClosedReaderTests(unittest.TestCase):
    def test_missing_file_loads_none(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(_store(Path(td)).load(SESSION, RUN))

    def test_bad_schema_and_kind_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = _store(Path(td))
            _start(store)
            path = store.path_for(SESSION, RUN)
            for payload in (
                {"schema_version": 999, "kind": KIND},
                {"schema_version": SCHEMA_VERSION, "kind": "not_run_operation_state"},
                {"kind": KIND},
                ["not", "a", "dict"],
            ):
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.subTest(payload=payload):
                    self.assertIsNone(store.load(SESSION, RUN))

    def test_unknown_phase_and_missing_ids_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = _store(Path(td))
            _start(store)
            path = store.path_for(SESSION, RUN)
            good = json.loads(path.read_text(encoding="utf-8"))
            for key, value in (("phase", "teleported"), ("run_id", ""), ("session_id", "")):
                payload = dict(good)
                payload[key] = value
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.subTest(field=key):
                    self.assertIsNone(store.load(SESSION, RUN))

    def test_wrong_run_id_at_same_path_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = _store(Path(td))
            _start(store)
            path = store.path_for(SESSION, RUN)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["run_id"] = "run-op-2"
            path.write_text(json.dumps(payload), encoding="utf-8")

            self.assertIsNone(store.load(SESSION, RUN))
            self.assertIsNone(store.commit(SESSION, RUN, mark_writer_running))

    def test_oversize_state_fails_closed_and_never_writes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = _store(Path(td))
            _start(store)
            path = store.path_for(SESSION, RUN)
            path.write_text("{" + (" " * (MAX_OPERATION_BYTES + 1)) + "}", encoding="utf-8")

            self.assertIsNone(store.load(SESSION, RUN))
            self.assertIsNone(store.commit(SESSION, RUN, mark_writer_running))
            self.assertGreater(path.stat().st_size, MAX_OPERATION_BYTES)

    def test_non_terminal_payload_cannot_carry_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = _store(Path(td))
            _start(store)
            path = store.path_for(SESSION, RUN)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["terminal"] = {"stop_reason": "done"}
            path.write_text(json.dumps(payload), encoding="utf-8")

            self.assertIsNone(store.load(SESSION, RUN))

    def test_start_refuses_to_clobber_existing_state(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = _store(Path(td))
            first = _start(store)
            second = store.start(
                session_id=SESSION,
                run_id=RUN,
                project="/tmp/other",
                provider_id="qwen",
                turn_budget=3,
                max_repair_rounds=1,
            )

            self.assertIsNone(second)
            reloaded = store.load(SESSION, RUN)
            assert reloaded is not None
            self.assertEqual(reloaded, first)

    def test_start_refuses_to_clobber_an_invalid_existing_file(self) -> None:
        # A corrupted leftover register is evidence, not garbage: start()
        # must never overwrite it, even though load() would fail closed.
        with tempfile.TemporaryDirectory() as td:
            store = _store(Path(td))
            path = store.path_for(SESSION, RUN)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{"schema_version": 999}', encoding="utf-8")

            started = store.start(
                session_id=SESSION,
                run_id=RUN,
                project="",
                provider_id="deepseek",
                turn_budget=8,
                max_repair_rounds=1,
            )

            self.assertIsNone(started)
            self.assertEqual(
                path.read_text(encoding="utf-8"), '{"schema_version": 999}'
            )


class StartLockTests(unittest.TestCase):
    def test_concurrent_starts_produce_exactly_one_register(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = _store(Path(td))
            results: list[RunOperationState | None] = []
            errors: list[Exception] = []
            barrier = threading.Barrier(4)

            def starter() -> None:
                try:
                    barrier.wait()
                    results.append(
                        store.start(
                            session_id=SESSION,
                            run_id=RUN,
                            project="",
                            provider_id="deepseek",
                            turn_budget=8,
                            max_repair_rounds=1,
                        )
                    )
                except Exception as exc:  # pragma: no cover - failure capture
                    errors.append(exc)

            threads = [threading.Thread(target=starter) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(errors, [])
            winners = [state for state in results if state is not None]
            self.assertEqual(len(winners), 1)
            reloaded = store.load(SESSION, RUN)
            self.assertIsNotNone(reloaded)
            assert reloaded is not None
            self.assertEqual(reloaded.phase, PHASE_ACCEPTED)


class ProjectRefTests(unittest.TestCase):
    def test_start_derives_a_stable_ref_never_a_raw_path(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project = Path(td) / "project"
            project.mkdir()
            store = _store(Path(td))
            started = store.start(
                session_id=SESSION,
                run_id=RUN,
                project=str(project),
                provider_id="deepseek",
                turn_budget=8,
                max_repair_rounds=1,
            )
            assert started is not None

            from codey.storage.local_store import project_key

            self.assertEqual(
                started.project_ref, f"project:{project_key(str(project))}"
            )
            self.assertNotIn(str(project), json.dumps(started.to_payload()))

    def test_empty_project_yields_an_empty_ref(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            started = _store(Path(td)).start(
                session_id=SESSION,
                run_id=RUN,
                project="",
                provider_id="deepseek",
                turn_budget=8,
                max_repair_rounds=1,
            )
            assert started is not None
            self.assertEqual(started.project_ref, "")


class StrictReaderTests(unittest.TestCase):
    """Anything not canonical schema v1 fails closed -- no coercion."""

    def _canonical(self) -> dict:
        return _fresh_state(PHASE_ACCEPTED).to_payload()

    def _rejects(self, mutate) -> None:
        payload = self._canonical()
        mutate(payload)
        self.assertIsNone(RunOperationState.from_payload(payload))

    def test_bool_never_passes_as_int(self) -> None:
        for key in ("turn_budget", "writer_attempt", "turns_used", "repair_rounds"):
            with self.subTest(field=key):
                self._rejects(lambda payload, key=key: payload.__setitem__(key, True))

    def test_numeric_strings_never_pass_as_int(self) -> None:
        for key in ("turn_budget", "turns_used", "repair_rounds"):
            with self.subTest(field=key):
                self._rejects(lambda payload, key=key: payload.__setitem__(key, "3"))

    def test_negative_ints_fail_closed(self) -> None:
        self._rejects(lambda payload: payload.__setitem__("turns_used", -1))

    def test_required_fields_must_exist(self) -> None:
        for key in (
            "project_ref",
            "provider_id",
            "started_at",
            "updated_at",
            "session_id",
            "run_id",
            "phase",
            "writer_attempt",
        ):
            with self.subTest(field=key):
                self._rejects(lambda payload, key=key: payload.pop(key))

    def test_empty_identity_fields_fail_closed(self) -> None:
        for key in ("session_id", "run_id", "started_at", "updated_at"):
            with self.subTest(field=key):
                self._rejects(lambda payload, key=key: payload.__setitem__(key, ""))

    def test_satisfied_must_be_a_bool_when_present(self) -> None:
        self._rejects(
            lambda payload: payload.__setitem__("completion_proof_satisfied", "yes")
        )

    def test_over_length_field_fails_closed(self) -> None:
        self._rejects(lambda payload: payload.__setitem__("blocked_reason", "x" * 200))

    def test_terminal_snapshot_is_strict_too(self) -> None:
        state = mark_terminal(
            mark_writer_running(_fresh_state(PHASE_ACCEPTED), provider_id="deepseek"),
            stop_reason="done",
            summary_chars=3,
            turns=2,
            max_turns=8,
            provider="deepseek",
        )
        canonical = state.to_payload()
        self.assertEqual(RunOperationState.from_payload(canonical), state)

        for key, value in (
            ("summary_chars", "abc"),
            ("turns", True),
            ("finished_at", ""),
            ("stop_reason", None),
        ):
            payload = json.loads(json.dumps(canonical))
            payload["terminal"][key] = value
            with self.subTest(field=key):
                self.assertIsNone(RunOperationState.from_payload(payload))


class TransitionTableTests(unittest.TestCase):
    def test_repair_running_cannot_skip_admission(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = _store(Path(td))
            state = mark_writer_settled(
                mark_writer_running(_start(store), provider_id="deepseek"),
                provider_id="deepseek",
                turns_used=2,
                stop_reason="done",
            )
            with self.assertRaises(RunOperationTransitionError):
                mark_repair_running(state, provider_id="deepseek")

    def test_repair_rounds_cannot_exceed_budget(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = _store(Path(td))
            state = mark_repair_context_admitted(
                mark_completion_proof_recorded(
                    mark_writer_settled(
                        mark_writer_running(_start(store), provider_id="deepseek"),
                        provider_id="deepseek",
                        turns_used=4,
                        stop_reason="done",
                    ),
                    proof_ref="p1",
                    proof_status="failed",
                    proof_satisfied=False,
                ),
                context_ref="sha256:" + "b" * 64,
            )
            state = mark_repair_running(state, provider_id="deepseek")
            state = mark_repair_settled(state, provider_id="deepseek", stop_reason="done")
            state = mark_completion_proof_recorded(
                state,
                proof_ref="p2",
                proof_status="failed",
                proof_satisfied=False,
            )
            state = mark_repair_context_admitted(state, context_ref="sha256:" + "c" * 64)
            with self.assertRaises(RunOperationTransitionError):
                mark_repair_running(state, provider_id="deepseek")

    def test_every_skip_transition_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = _store(Path(td))
            accepted = _start(store)
            illegal = [
                (mark_repair_context_admitted, {"context_ref": "sha256:" + "d" * 64}),
                (mark_repair_running, {"provider_id": "deepseek"}),
                (mark_repair_settled, {"stop_reason": "done", "provider_id": "deepseek"}),
                (mark_completion_blocked, {"reason": "unobserved"}),
            ]
            for fn, kwargs in illegal:
                with self.subTest(transition=fn.__name__):
                    with self.assertRaises(RunOperationTransitionError):
                        fn(accepted, **kwargs)  # type: ignore[arg-type]

            settled = mark_writer_settled(
                mark_writer_running(accepted, provider_id="deepseek"),
                provider_id="deepseek",
                turns_used=1,
                stop_reason="done",
            )
            with self.assertRaises(RunOperationTransitionError):
                mark_completion_blocked(settled, reason="unobserved")

    def test_completion_verdict_requires_recorded_proof_phase(self) -> None:
        state = RunOperationState(
            session_id=SESSION,
            run_id=RUN,
            project_ref="",
            provider_id="deepseek",
            turn_budget=8,
            max_repair_rounds=1,
            phase=PHASE_COMPLETION_PROOF_RECORDED,
            started_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        marked = mark_completion_blocked(state, reason="unobserved")
        self.assertEqual(marked.blocked_reason, "unobserved")
        self.assertEqual(marked.phase, PHASE_COMPLETION_PROOF_RECORDED)


class TerminalImmutabilityTests(unittest.TestCase):
    def test_terminal_idempotent_for_same_payload(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = _store(Path(td))
            state = _clean_path(store)
            again = mark_terminal(
                state,
                stop_reason="done",
                summary_chars=42,
                turns=4,
                max_turns=8,
                provider="deepseek",
            )
            self.assertEqual(again, state)

    def test_terminal_rejects_a_different_payload(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = _store(Path(td))
            state = _clean_path(store)
            with self.assertRaises(RunOperationTransitionError):
                mark_terminal(
                    state,
                    stop_reason="stopped",
                    summary_chars=42,
                    turns=3,
                    max_turns=8,
                    provider="deepseek",
                )

    def test_terminal_default_blocked_reason_comes_from_state(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = _store(Path(td))
            state = mark_completion_blocked(
                mark_completion_proof_recorded(
                    mark_writer_settled(
                        mark_writer_running(_start(store), provider_id="deepseek"),
                        provider_id="deepseek",
                        turns_used=4,
                        stop_reason="done",
                    ),
                    proof_ref="p1",
                    proof_status="failed",
                    proof_satisfied=False,
                ),
                reason="unobserved",
            )
            state = mark_terminal(
                state,
                stop_reason="blocked",
                summary_chars=20,
                turns=4,
                max_turns=8,
                provider="deepseek",
            )
            assert state.terminal is not None
            self.assertEqual(state.terminal.blocked_reason, "unobserved")

    def test_non_terminal_rejects_terminal_field_in_payload(self) -> None:
        # Covered by the reader; here the writer side is locked too: a
        # non-terminal state never serializes a terminal member.
        with tempfile.TemporaryDirectory() as td:
            store = _store(Path(td))
            state = _start(store)
            self.assertNotIn("terminal", state.to_payload())


class PayloadHygieneTests(unittest.TestCase):
    def test_free_text_fields_are_clipped(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = _store(Path(td))
            running = mark_writer_running(_start(store), provider_id="x" * 500)
            self.assertEqual(running.to_payload()["provider_id"], "x" * 80)

            recorded = mark_completion_proof_recorded(
                mark_writer_settled(
                    running,
                    provider_id="deepseek",
                    turns_used=1,
                    stop_reason="done",
                ),
                proof_ref="p" * 500,
                proof_status="failed",
                proof_satisfied=False,
            )
            self.assertEqual(recorded.to_payload()["completion_proof_ref"], "p" * 120)

            blocked = mark_completion_blocked(recorded, reason="r" * 500)
            self.assertEqual(blocked.to_payload()["blocked_reason"], "r" * 80)

    def test_payload_vocabulary_cannot_hold_raw_content(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = _store(Path(td))
            state = _repair_path(store)
            serialized = json.dumps(state.to_payload(), ensure_ascii=False)

            for key in (
                "prompt",
                "reply",
                "stdout",
                "stderr",
                "diff",
                "source",
                "task",
                "error",
                "message",
                "text",
                "summary",
            ):
                # Match JSON keys exactly: a substring check would false-hit
                # e.g. repair_context_ref. summary_chars stays legal because
                # only the character COUNT is stored, mirroring
                # RunLedger.finish.
                with self.subTest(key=key):
                    self.assertNotIn(f'"{key}":', serialized)

    def test_store_writes_are_locked_and_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = _store(Path(td))
            _start(store)
            with (
                mock.patch(
                    "codey.run_operation.with_file_lock",
                    wraps=run_operation_module.with_file_lock,
                ) as lock_spy,
                mock.patch(
                    "codey.run_operation.write_json_atomic",
                    wraps=run_operation_module.write_json_atomic,
                ) as write_spy,
            ):
                store.commit(SESSION, RUN, lambda state: mark_writer_running(state, provider_id="deepseek"))

            self.assertEqual(lock_spy.call_count, 1)
            self.assertEqual(write_spy.call_count, 1)
            self.assertEqual(
                write_spy.call_args_list[-1].kwargs.get("max_bytes"),
                MAX_OPERATION_BYTES,
            )
            reloaded = store.load(SESSION, RUN)
            assert reloaded is not None
            self.assertEqual(reloaded.phase, PHASE_WRITER_RUNNING)

    def test_concurrent_commits_stay_valid(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = _store(Path(td))
            _start(store)
            errors: list[Exception] = []

            def writer() -> None:
                try:
                    for _ in range(5):
                        # Terminal is idempotent for the same payload, so the
                        # racing commits are legal from every position; the
                        # lock keeps the file a valid state throughout.
                        result = store.commit(
                            SESSION,
                            RUN,
                            lambda state: mark_terminal(
                                state,
                                stop_reason="stopped",
                                summary_chars=0,
                                turns=0,
                                max_turns=8,
                                provider="deepseek",
                            ),
                        )
                        assert result is not None
                except Exception as exc:  # pragma: no cover - failure capture
                    errors.append(exc)

            threads = [threading.Thread(target=writer) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(errors, [])
            reloaded = store.load(SESSION, RUN)
            self.assertIsNotNone(reloaded)
            assert reloaded is not None
            self.assertEqual(reloaded.phase, PHASE_TERMINAL)


def _fresh_state(phase: str, **overrides: object) -> RunOperationState:
    """A pure in-memory state; the mark_* functions need no store."""

    return RunOperationState(
        session_id=SESSION,
        run_id=RUN,
        project_ref="",
        provider_id="deepseek",
        turn_budget=8,
        max_repair_rounds=1,
        phase=phase,
        started_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        **overrides,  # type: ignore[arg-type]
    )


class RecoveryTextTests(unittest.TestCase):
    def test_progress_text_covers_every_non_terminal_phase(self) -> None:
        texts = {
            PHASE_ACCEPTED: "Writing was interrupted",
            PHASE_WRITER_RUNNING: "Writing was interrupted",
            PHASE_WRITER_SETTLED: "Completion check was interrupted",
            PHASE_COMPLETION_PROOF_RECORDED: "Completion check was interrupted",
            PHASE_REPAIR_CONTEXT_ADMITTED: "Stopped during repair",
            PHASE_REPAIR_RUNNING: "Stopped during repair",
            # A settled repair is no longer running one; what was
            # interrupted is the post-repair completion check.
            PHASE_REPAIR_SETTLED: "Completion check was interrupted",
        }
        for phase, expected in texts.items():
            with self.subTest(phase=phase):
                self.assertEqual(operation_progress_text(_fresh_state(phase)), expected)

    def test_satisfied_proof_reads_as_finishing_not_checking(self) -> None:
        satisfied = mark_completion_proof_recorded(
            _fresh_state(PHASE_WRITER_SETTLED),
            proof_ref="p1",
            proof_status="complete",
            proof_satisfied=True,
        )
        self.assertEqual(operation_progress_text(satisfied), "Finishing was interrupted")
        self.assertEqual(
            operation_progress_text(_fresh_state(PHASE_COMPLETION_PROOF_RECORDED)),
            "Completion check was interrupted",
        )

    def test_terminal_and_missing_state_stay_quiet(self) -> None:
        terminal = mark_terminal(
            mark_writer_running(_fresh_state(PHASE_ACCEPTED), provider_id="deepseek"),
            stop_reason="stopped",
            summary_chars=0,
            turns=0,
            max_turns=8,
            provider="deepseek",
        )
        self.assertEqual(operation_progress_text(terminal), "")
        self.assertEqual(operation_progress_text(None), "")


class StoreLifecycleTests(unittest.TestCase):
    def test_delete_session_removes_only_that_bucket(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = _store(Path(td))
            _start(store)
            other = store.start(
                session_id="session-other",
                run_id=RUN,
                project="",
                provider_id="deepseek",
                turn_budget=8,
                max_repair_rounds=1,
            )
            assert other is not None

            store.delete_session(SESSION)

            self.assertIsNone(store.load(SESSION, RUN))
            self.assertIsNotNone(store.load("session-other", RUN))

    def test_commit_on_missing_state_is_none_without_write(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = _store(Path(td))
            result = store.commit(SESSION, RUN, mark_writer_running)

            self.assertIsNone(result)
            self.assertFalse(store.path_for(SESSION, RUN).exists())


class ImportBoundaryTests(unittest.TestCase):
    def test_run_operation_is_a_storage_leaf(self) -> None:
        path = ROOT / "codey" / "run_operation.py"
        imports = {
            node.module or ""
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(node, ast.ImportFrom)
        } | {
            name.name.split(".")[0]
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(node, ast.Import)
            for name in node.names
        }
        internal = sorted(name for name in imports if name == "codey" or name.startswith("codey."))
        self.assertEqual(
            internal,
            ["codey.storage.file_lock", "codey.storage.local_store"],
        )
        forbidden = {
            "codey.agents",
            "codey.app",
            "codey.completion",
            "codey.ghost",
            "codey.providers",
            "codey.research",
            "codey.toolchain",
            "subprocess",
            "socket",
            "urllib",
        }
        self.assertTrue(forbidden.isdisjoint(imports), sorted(forbidden & imports))


if __name__ == "__main__":
    unittest.main()
