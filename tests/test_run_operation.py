"""RunOperationStore: durable program counter for one run (0.5.1)."""

from __future__ import annotations

import ast
import json
import os
import tempfile
import threading
import unittest
from dataclasses import replace
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
CONTEXT_REF = "sha256:" + "a" * 64
PROOF_REF_OK = "completion_proof:" + "a" * 16
PROOF_REF_FAIL = "completion_proof:" + "b" * 16
PROOF_REF_FAIL_2 = "completion_proof:" + "c" * 16

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
            proof_ref=PROOF_REF_FAIL,
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
            proof_ref=PROOF_REF_OK,
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
        lambda state: mark_repair_context_admitted(state, context_ref=CONTEXT_REF),
        lambda state: mark_repair_running(state, provider_id="deepseek"),
        lambda state: mark_repair_settled(state, provider_id="deepseek", stop_reason="done", turns_used=6),
        lambda state: mark_completion_proof_recorded(
            state,
            proof_ref=PROOF_REF_OK,
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
            project_ref="project:" + "a" * 24,
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

    def test_padded_identity_on_disk_fails_closed(self) -> None:
        # A trimmed id could never be found again by commits keyed on the
        # original; a padded one must not load as the trimmed original
        # either -- the reader canonicalizes nothing.
        with tempfile.TemporaryDirectory() as td:
            store = _store(Path(td))
            _start(store)
            path = store.path_for(SESSION, RUN)
            good = json.loads(path.read_text(encoding="utf-8"))
            for key, value in (("session_id", " " + SESSION + " "), ("run_id", " " + RUN)):
                payload = dict(good)
                payload[key] = value
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.subTest(field=key):
                    self.assertIsNone(store.load(SESSION, RUN))

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


class StartIdentityTests(unittest.TestCase):
    """Identity is validated, never clipped: no trimmed-register limbo."""

    def test_over_long_identity_is_refused_without_creating_a_file(self) -> None:
        long_id = "x" * (200 + 1)
        for kwargs in (
            {"session_id": long_id},
            {"run_id": long_id},
        ):
            with self.subTest(field=next(iter(kwargs))):
                with tempfile.TemporaryDirectory() as td:
                    store = _store(Path(td))
                    params = {
                        "session_id": SESSION,
                        "run_id": RUN,
                        "project": "",
                        "provider_id": "deepseek",
                        "turn_budget": 8,
                        "max_repair_rounds": 1,
                    }
                    params.update(kwargs)
                    started = store.start(**params)

                    self.assertIsNone(started)
                    self.assertFalse(store.path_for(SESSION, RUN).exists())
                    # The refusal is keyed on the offending id itself: no
                    # register exists under any path to find later.
                    if "session_id" in kwargs:
                        self.assertFalse(store.path_for(long_id, RUN).exists())
                    else:
                        self.assertFalse(store.path_for(SESSION, long_id).exists())

    def test_identity_at_the_length_boundary_starts_commits_and_loads(self) -> None:
        boundary_session = "s" * 200
        boundary_run = "r" * 200
        with tempfile.TemporaryDirectory() as td:
            store = _store(Path(td))
            started = store.start(
                session_id=boundary_session,
                run_id=boundary_run,
                project="",
                provider_id="deepseek",
                turn_budget=8,
                max_repair_rounds=1,
            )
            self.assertIsNotNone(started)

            committed = store.commit(
                boundary_session,
                boundary_run,
                lambda state: mark_writer_running(state, provider_id="deepseek"),
            )
            self.assertIsNotNone(committed)
            reloaded = store.load(boundary_session, boundary_run)
            assert reloaded is not None
            self.assertEqual(reloaded.phase, PHASE_WRITER_RUNNING)

    def test_non_canonical_identity_is_refused(self) -> None:
        for value in ("", "   ", " padded ", None, 123):
            with self.subTest(identity=repr(value)):
                with tempfile.TemporaryDirectory() as td:
                    store = _store(Path(td))
                    started = store.start(
                        session_id=value,
                        run_id=RUN,
                        project="",
                        provider_id="deepseek",
                        turn_budget=8,
                        max_repair_rounds=1,
                    )

                    self.assertIsNone(started)
                    self.assertFalse(store.path_for(SESSION, RUN).exists())


class StartArgumentTests(unittest.TestCase):
    """start() validates every argument, never clips or coerces one."""

    def test_non_canonical_arguments_are_refused_without_writing(self) -> None:
        for kwargs in (
            {"provider_id": ""},
            {"provider_id": " deepseek "},
            {"provider_id": 7},
            {"provider_id": "x" * 81},
            {"turn_budget": "8"},
            {"turn_budget": True},
            {"turn_budget": -1},
            {"turn_budget": None},
            {"max_repair_rounds": "1"},
        ):
            with self.subTest(**kwargs):
                with tempfile.TemporaryDirectory() as td:
                    store = _store(Path(td))
                    params = {
                        "session_id": SESSION,
                        "run_id": RUN,
                        "project": "",
                        "provider_id": "deepseek",
                        "turn_budget": 8,
                        "max_repair_rounds": 1,
                    }
                    params.update(kwargs)
                    started = store.start(**params)

                    self.assertIsNone(started)
                    self.assertFalse(store.path_for(SESSION, RUN).exists())


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

    def test_count_relationships_fail_closed(self) -> None:
        payload = _fresh_state(
            PHASE_WRITER_SETTLED,
            turns_used=9,
            stop_reason="done",
        ).to_payload()
        self.assertIsNone(RunOperationState.from_payload(payload))

        state = mark_terminal(
            mark_writer_running(_fresh_state(PHASE_ACCEPTED), provider_id="deepseek"),
            stop_reason="done",
            summary_chars=3,
            turns=2,
            max_turns=8,
            provider="deepseek",
        )

        terminal_turns = json.loads(json.dumps(state.to_payload()))
        terminal_turns["terminal"]["turns"] = 9
        self.assertIsNone(RunOperationState.from_payload(terminal_turns))

        terminal_budget = json.loads(json.dumps(state.to_payload()))
        terminal_budget["terminal"]["max_turns"] = 7
        self.assertIsNone(RunOperationState.from_payload(terminal_budget))

    def test_terminal_snapshot_unknown_keys_fail_closed(self) -> None:
        # The terminal snapshot is part of the closed schema: an extension
        # field inside it -- a raw prompt, a diff, anything -- fails the
        # whole payload closed, exactly like an unknown top-level key.
        state = mark_terminal(
            mark_writer_running(_fresh_state(PHASE_ACCEPTED), provider_id="deepseek"),
            stop_reason="done",
            summary_chars=3,
            turns=2,
            max_turns=8,
            provider="deepseek",
        )
        for key in ("raw_prompt", "diff", "summary", "summary_text"):
            payload = json.loads(json.dumps(state.to_payload()))
            payload["terminal"][key] = "SHOULD_NEVER_BE_ACCEPTED"
            with self.subTest(key=key):
                self.assertIsNone(RunOperationState.from_payload(payload))

    def test_raw_or_malformed_project_ref_fails_closed(self) -> None:
        for ref in (
            "E:/secret/project",
            "E:\\secret\\project",
            "/tmp/project",
            "project:",
            "project:" + "g" * 24,
            "project:" + "a" * 25,
            "project:" + "a" * 24 + "/etc",
        ):
            with self.subTest(ref=ref):
                self._rejects(lambda payload, ref=ref: payload.__setitem__("project_ref", ref))

    def test_impossible_repair_states_fail_closed(self) -> None:
        # repair_context_admitted / running / settled require the committed
        # context ref; running / settled require at least one committed
        # round; no phase may claim more rounds than the budget allows.
        cases = [
            (PHASE_REPAIR_CONTEXT_ADMITTED, {"repair_context_ref": ""}),
            (PHASE_REPAIR_RUNNING, {"repair_context_ref": ""}),
            (PHASE_REPAIR_RUNNING, {"repair_rounds": 0}),
            (PHASE_REPAIR_SETTLED, {"repair_rounds": 0}),
            (PHASE_REPAIR_RUNNING, {"repair_rounds": 2}),  # budget is 1
        ]
        for phase, mutate_kwargs in cases:
            with self.subTest(phase=phase, **mutate_kwargs):
                payload = _fresh_state(phase).to_payload()
                payload.update(mutate_kwargs)
                self.assertIsNone(RunOperationState.from_payload(payload))

    def test_pre_repair_phases_cannot_carry_repair_facts(self) -> None:
        for phase in (PHASE_ACCEPTED, PHASE_WRITER_RUNNING, PHASE_WRITER_SETTLED):
            for mutate_kwargs in ({"repair_rounds": 1}, {"repair_context_ref": CONTEXT_REF}):
                with self.subTest(phase=phase, **mutate_kwargs):
                    payload = _fresh_state(phase).to_payload()
                    payload.update(mutate_kwargs)
                    self.assertIsNone(RunOperationState.from_payload(payload))

    def test_proof_phase_requires_the_recorded_proof_facts(self) -> None:
        for mutate_kwargs in (
            {"completion_proof_ref": ""},
            {"completion_proof_status": ""},
            {"completion_proof_satisfied": None},
        ):
            with self.subTest(**mutate_kwargs):
                payload = _fresh_state(PHASE_COMPLETION_PROOF_RECORDED).to_payload()
                payload.update(mutate_kwargs)
                if mutate_kwargs.get("completion_proof_satisfied") is None:
                    payload.pop("completion_proof_satisfied", None)
                self.assertIsNone(RunOperationState.from_payload(payload))

    def test_pre_repair_phases_cannot_carry_proof_facts(self) -> None:
        # The proof is recorded after writer_settled; no earlier phase may
        # carry any part of it -- not even a stray satisfied flag alone.
        for phase in (PHASE_ACCEPTED, PHASE_WRITER_RUNNING, PHASE_WRITER_SETTLED):
            for mutate_kwargs in (
                {"completion_proof_ref": "completion_proof:x"},
                {"completion_proof_status": "complete"},
                {"completion_proof_satisfied": True},
                {"completion_proof_satisfied": False},
            ):
                with self.subTest(phase=phase, **mutate_kwargs):
                    payload = _fresh_state(phase).to_payload()
                    payload.update(mutate_kwargs)
                    self.assertIsNone(RunOperationState.from_payload(payload))

    def test_post_proof_phases_require_the_complete_proof_facts(self) -> None:
        # The recorded proof is the only road into the repair arm, so every
        # post-proof phase -- not just completion_proof_recorded itself --
        # must still carry that proof's complete facts.
        contexts = {
            PHASE_COMPLETION_PROOF_RECORDED: {},
            PHASE_REPAIR_CONTEXT_ADMITTED: {"repair_context_ref": CONTEXT_REF},
            PHASE_REPAIR_RUNNING: {"repair_context_ref": CONTEXT_REF, "repair_rounds": 1},
            PHASE_REPAIR_SETTLED: {"repair_context_ref": CONTEXT_REF, "repair_rounds": 1},
        }
        for phase, context in contexts.items():
            state = _fresh_state(
                phase,
                completion_proof_ref=PROOF_REF_FAIL,
                completion_proof_status="failed",
                completion_proof_satisfied=False,
                **context,
            )
            for dropped in (
                "completion_proof_ref",
                "completion_proof_status",
                "completion_proof_satisfied",
            ):
                with self.subTest(phase=phase, missing=dropped):
                    payload = state.to_payload()
                    payload.pop(dropped)
                    self.assertIsNone(RunOperationState.from_payload(payload))

    def test_malformed_repair_context_ref_fails_closed(self) -> None:
        # The context is named by its sha256 digest; anything else -- a bare
        # "ctx", a wrong hash form, a padded or extended ref -- is not a
        # committed admission.
        for ref in (
            "ctx",
            "sha256:abc",
            "sha256:" + "g" * 64,
            "SHA256:" + "a" * 64,
            "sha256:" + "a" * 63,
            "sha256:" + "a" * 65,
            "md5:" + "a" * 32,
            "sha256:" + "a" * 64 + "/etc",
        ):
            with self.subTest(ref=ref):
                payload = _fresh_state(
                    PHASE_REPAIR_CONTEXT_ADMITTED, repair_context_ref=CONTEXT_REF
                ).to_payload()
                payload["repair_context_ref"] = ref
                self.assertIsNone(RunOperationState.from_payload(payload))

        # A terminal reached through the repair arm carries the ref too, and
        # the same format rule holds there.
        settled = mark_repair_settled(
            mark_repair_running(
                mark_repair_context_admitted(
                    mark_completion_proof_recorded(
                        mark_writer_settled(
                            mark_writer_running(
                                _fresh_state(PHASE_ACCEPTED), provider_id="deepseek"
                            ),
                            provider_id="deepseek",
                            turns_used=2,
                            stop_reason="done",
                        ),
                        proof_ref=PROOF_REF_FAIL,
                        proof_status="failed",
                        proof_satisfied=False,
                    ),
                    context_ref=CONTEXT_REF,
                ),
                provider_id="deepseek",
            ),
            provider_id="deepseek",
            stop_reason="done",
        )
        payload = mark_terminal(
            settled,
            stop_reason="stopped",
            summary_chars=0,
            turns=0,
            max_turns=8,
            provider="deepseek",
        ).to_payload()
        payload["repair_context_ref"] = "ctx"
        self.assertIsNone(RunOperationState.from_payload(payload))

    def test_padded_top_level_text_fields_fail_closed(self) -> None:
        # The reader canonicalizes nothing: a padded text field is not
        # schema v1, so a register can never load as a trimmed version of
        # what is on disk.
        for key, value in (
            ("session_id", " " + SESSION + " "),
            ("run_id", RUN + " "),
            ("phase", " " + PHASE_ACCEPTED),
            ("provider_id", " deepseek "),
            ("started_at", " 2026-01-01T00:00:00Z"),
            ("blocked_reason", " unobserved "),
            ("project_ref", " project:" + "a" * 24),
        ):
            with self.subTest(field=key):
                payload = _fresh_state(PHASE_ACCEPTED).to_payload()
                payload[key] = value
                self.assertIsNone(RunOperationState.from_payload(payload))

    def test_padded_proof_and_context_fields_fail_closed(self) -> None:
        recorded = mark_completion_proof_recorded(
            _fresh_state(PHASE_WRITER_SETTLED),
            proof_ref=PROOF_REF_FAIL,
            proof_status="failed",
            proof_satisfied=False,
        )
        for key, value in (
            ("completion_proof_ref", " " + PROOF_REF_FAIL),
            ("completion_proof_status", " failed "),
        ):
            with self.subTest(field=key):
                payload = recorded.to_payload()
                payload[key] = value
                self.assertIsNone(RunOperationState.from_payload(payload))

        admitted = mark_repair_context_admitted(recorded, context_ref=CONTEXT_REF)
        payload = admitted.to_payload()
        payload["repair_context_ref"] = " " + CONTEXT_REF
        self.assertIsNone(RunOperationState.from_payload(payload))

    def test_padded_terminal_text_fields_fail_closed(self) -> None:
        state = mark_terminal(
            mark_writer_running(_fresh_state(PHASE_ACCEPTED), provider_id="deepseek"),
            stop_reason="done",
            summary_chars=3,
            turns=2,
            max_turns=8,
            provider="deepseek",
        )
        for key in ("stop_reason", "provider", "blocked_reason"):
            payload = json.loads(json.dumps(state.to_payload()))
            payload["terminal"][key] = " " + str(payload["terminal"][key]) + " "
            with self.subTest(field=key):
                self.assertIsNone(RunOperationState.from_payload(payload))

    def test_terminal_partial_proof_facts_fail_closed(self) -> None:
        # Terminal keeps its source phase's facts; a partially claimed proof
        # is a state no source phase could have committed.
        base = mark_terminal(
            mark_writer_running(_fresh_state(PHASE_ACCEPTED), provider_id="deepseek"),
            stop_reason="stopped",
            summary_chars=0,
            turns=0,
            max_turns=8,
            provider="deepseek",
        ).to_payload()
        for mutate_kwargs in (
            {"completion_proof_ref": PROOF_REF_OK},
            {"completion_proof_status": "failed"},
            {"completion_proof_satisfied": True},
            {"completion_proof_satisfied": False},
        ):
            with self.subTest(**mutate_kwargs):
                payload = dict(base)
                payload.update(mutate_kwargs)
                self.assertIsNone(RunOperationState.from_payload(payload))

    def test_terminal_repair_facts_require_the_recorded_proof(self) -> None:
        base = mark_terminal(
            mark_writer_running(_fresh_state(PHASE_ACCEPTED), provider_id="deepseek"),
            stop_reason="stopped",
            summary_chars=0,
            turns=0,
            max_turns=8,
            provider="deepseek",
        ).to_payload()
        for mutate_kwargs in (
            {"repair_rounds": 1},  # a round without the admitted context
            {"repair_context_ref": CONTEXT_REF},  # a context without its proof
            {
                "repair_rounds": 1,
                "repair_context_ref": CONTEXT_REF,
                "completion_proof_status": "failed",  # still no proof triple
            },
        ):
            with self.subTest(**mutate_kwargs):
                payload = dict(base)
                payload.update(mutate_kwargs)
                self.assertIsNone(RunOperationState.from_payload(payload))

    def test_reachable_terminal_fact_combinations_round_trip(self) -> None:
        # Every fact combination the table can produce on the way into
        # terminal must keep loading: no facts (stopped mid-writing), the
        # recorded proof, and the repair arm's context/round facts.
        failed_proof = mark_completion_proof_recorded(
            mark_writer_settled(
                mark_writer_running(_fresh_state(PHASE_ACCEPTED), provider_id="deepseek"),
                provider_id="deepseek",
                turns_used=2,
                stop_reason="done",
            ),
            proof_ref=PROOF_REF_FAIL,
            proof_status="failed",
            proof_satisfied=False,
        )
        admitted = mark_repair_context_admitted(failed_proof, context_ref=CONTEXT_REF)
        running = mark_repair_running(admitted, provider_id="deepseek")
        settled = mark_repair_settled(running, provider_id="deepseek", stop_reason="done")
        terminals = [
            mark_terminal(
                mark_writer_running(_fresh_state(PHASE_ACCEPTED), provider_id="deepseek"),
                stop_reason="stopped",
                summary_chars=0,
                turns=0,
                max_turns=8,
                provider="deepseek",
            ),
            mark_terminal(
                mark_completion_proof_recorded(
                    _fresh_state(PHASE_WRITER_SETTLED),
                    proof_ref=PROOF_REF_OK,
                    proof_status="complete",
                    proof_satisfied=True,
                ),
                stop_reason="done",
                summary_chars=4,
                turns=2,
                max_turns=8,
                provider="deepseek",
            ),
        ]
        for source in (admitted, running, settled):
            terminals.append(
                mark_terminal(
                    source,
                    stop_reason="stopped",
                    summary_chars=0,
                    turns=0,
                    max_turns=8,
                    provider="deepseek",
                )
            )
        for state in terminals:
            with self.subTest(phase=state.phase, rounds=state.repair_rounds):
                self.assertEqual(RunOperationState.from_payload(state.to_payload()), state)

    def test_unrecorded_proof_refs_fail_closed(self) -> None:
        # The proof is named by its completion_proof:<16 hex> id, like the
        # run trace's own proof vocabulary -- never by a raw path or a
        # free-form ref.
        recorded = mark_completion_proof_recorded(
            _fresh_state(PHASE_WRITER_SETTLED),
            proof_ref=PROOF_REF_FAIL,
            proof_status="failed",
            proof_satisfied=False,
        )
        for ref in (
            "E:/secret/project",
            "/tmp/proof",
            "proof:0123456789abcdef",
            "completion_proof:xyz",
            "completion_proof:" + "a" * 15,
            "completion_proof:" + "a" * 17,
            "completion_proof:" + "g" * 16,
        ):
            with self.subTest(ref=ref):
                payload = recorded.to_payload()
                payload["completion_proof_ref"] = ref
                self.assertIsNone(RunOperationState.from_payload(payload))

    def test_unrecorded_proof_statuses_fail_closed(self) -> None:
        recorded = mark_completion_proof_recorded(
            _fresh_state(PHASE_WRITER_SETTLED),
            proof_ref=PROOF_REF_FAIL,
            proof_status="failed",
            proof_satisfied=False,
        )
        for status in ("nonsense", "pending", "running", "Complete", ""):
            with self.subTest(status=status):
                payload = recorded.to_payload()
                payload["completion_proof_status"] = status
                self.assertIsNone(RunOperationState.from_payload(payload))

    def test_proof_satisfied_must_match_the_recorded_status(self) -> None:
        # The proof builder derives satisfied from the status; the register
        # may not claim a verdict the status contradicts.
        recorded = mark_completion_proof_recorded(
            _fresh_state(PHASE_WRITER_SETTLED),
            proof_ref=PROOF_REF_FAIL,
            proof_status="failed",
            proof_satisfied=False,
        )
        payload = recorded.to_payload()
        payload["completion_proof_satisfied"] = True
        self.assertIsNone(RunOperationState.from_payload(payload))

        complete = mark_completion_proof_recorded(
            _fresh_state(PHASE_WRITER_SETTLED),
            proof_ref=PROOF_REF_OK,
            proof_status="complete",
            proof_satisfied=True,
        )
        payload = complete.to_payload()
        payload["completion_proof_satisfied"] = False
        self.assertIsNone(RunOperationState.from_payload(payload))

    def test_complete_with_limitations_is_honestly_unsatisfied(self) -> None:
        # A limited pass records satisfied=False -- the same derivation the
        # proof builder uses -- and still round-trips.
        limited = mark_completion_proof_recorded(
            _fresh_state(PHASE_WRITER_SETTLED),
            proof_ref=PROOF_REF_OK,
            proof_status="complete_with_limitations",
            proof_satisfied=False,
        )
        self.assertFalse(limited.completion_proof_satisfied)
        self.assertEqual(RunOperationState.from_payload(limited.to_payload()), limited)

    def test_blocked_reason_requires_an_unsatisfied_failed_proof(self) -> None:
        # The verdict sits on the proof that failed the run: complete,
        # limited, and unproven states cannot carry one.
        for phase, overrides in (
            (PHASE_ACCEPTED, {}),
            (PHASE_WRITER_SETTLED, {}),
            (
                PHASE_COMPLETION_PROOF_RECORDED,
                {
                    "completion_proof_ref": PROOF_REF_OK,
                    "completion_proof_status": "complete",
                    "completion_proof_satisfied": True,
                },
            ),
            (
                PHASE_COMPLETION_PROOF_RECORDED,
                {
                    "completion_proof_ref": PROOF_REF_OK,
                    "completion_proof_status": "complete_with_limitations",
                    "completion_proof_satisfied": False,
                },
            ),
        ):
            with self.subTest(phase=phase, **overrides):
                payload = _fresh_state(phase, **overrides).to_payload()
                payload["blocked_reason"] = "unobserved"
                self.assertIsNone(RunOperationState.from_payload(payload))

        backed = mark_completion_blocked(
            mark_completion_proof_recorded(
                _fresh_state(PHASE_WRITER_SETTLED),
                proof_ref=PROOF_REF_FAIL,
                proof_status="failed",
                proof_satisfied=False,
            ),
            reason="unobserved",
        )
        self.assertEqual(RunOperationState.from_payload(backed.to_payload()), backed)

    def test_active_repair_phases_cannot_carry_a_blocked_verdict(self) -> None:
        # Only the recorded-proof phase, the settled repair, and terminal
        # may hold the verdict: an admitted or running repair is still in
        # motion and never carries one.
        for phase, overrides in (
            (PHASE_REPAIR_CONTEXT_ADMITTED, {"repair_context_ref": CONTEXT_REF}),
            (
                PHASE_REPAIR_RUNNING,
                {"repair_context_ref": CONTEXT_REF, "repair_rounds": 1},
            ),
        ):
            with self.subTest(phase=phase):
                payload = _fresh_state(
                    phase,
                    completion_proof_ref=PROOF_REF_FAIL,
                    completion_proof_status="failed",
                    completion_proof_satisfied=False,
                    **overrides,
                ).to_payload()
                payload["blocked_reason"] = "provider_failure"
                self.assertIsNone(RunOperationState.from_payload(payload))

        # The carrier phases keep round-tripping with their verdicts.
        failed_settled = mark_repair_settled(
            mark_repair_running(
                mark_repair_context_admitted(
                    mark_completion_proof_recorded(
                        _fresh_state(PHASE_WRITER_SETTLED),
                        proof_ref=PROOF_REF_FAIL,
                        proof_status="failed",
                        proof_satisfied=False,
                    ),
                    context_ref=CONTEXT_REF,
                ),
                provider_id="deepseek",
            ),
            provider_id="deepseek",
            stop_reason="",
            blocked_reason="provider_failure",
        )
        self.assertEqual(
            RunOperationState.from_payload(failed_settled.to_payload()), failed_settled
        )

    def test_repair_phases_carry_only_the_failed_proof(self) -> None:
        # The arm is reachable only from an unsatisfied failed proof, and it
        # carries that proof's facts through every repair phase.
        for phase, overrides in (
            (PHASE_REPAIR_CONTEXT_ADMITTED, {"repair_context_ref": CONTEXT_REF}),
            (
                PHASE_REPAIR_RUNNING,
                {"repair_context_ref": CONTEXT_REF, "repair_rounds": 1},
            ),
            (
                PHASE_REPAIR_SETTLED,
                {"repair_context_ref": CONTEXT_REF, "repair_rounds": 1},
            ),
        ):
            for status, satisfied in (
                ("complete", True),
                ("complete_with_limitations", False),
                ("blocked", False),
            ):
                with self.subTest(phase=phase, status=status):
                    payload = _fresh_state(
                        phase,
                        completion_proof_ref=PROOF_REF_OK,
                        completion_proof_status=status,
                        completion_proof_satisfied=satisfied,
                        **overrides,
                    ).to_payload()
                    self.assertIsNone(RunOperationState.from_payload(payload))

        # The reachable repair arm keeps round-tripping...
        settled_arm = mark_repair_settled(
            mark_repair_running(
                mark_repair_context_admitted(
                    mark_completion_proof_recorded(
                        _fresh_state(PHASE_WRITER_SETTLED),
                        proof_ref=PROOF_REF_FAIL,
                        proof_status="failed",
                        proof_satisfied=False,
                    ),
                    context_ref=CONTEXT_REF,
                ),
                provider_id="deepseek",
            ),
            provider_id="deepseek",
            stop_reason="done",
        )
        self.assertEqual(
            RunOperationState.from_payload(settled_arm.to_payload()), settled_arm
        )

        # ...and the post-repair re-proof to complete stays legal: that is
        # completion_proof_recorded + repair facts, not an active phase.
        reproof = mark_completion_proof_recorded(
            settled_arm,
            proof_ref=PROOF_REF_OK,
            proof_status="complete",
            proof_satisfied=True,
        )
        self.assertEqual(RunOperationState.from_payload(reproof.to_payload()), reproof)

    def test_terminal_admitted_context_requires_its_failed_proof(self) -> None:
        # repair_context_admitted -> terminal is the stop before the repair
        # ran, and admission belongs to an unsatisfied failed proof: a
        # context with no committed round cannot ride a non-failed proof.
        for status, satisfied in (
            ("complete", True),
            ("complete_with_limitations", False),
            ("blocked", False),
        ):
            with self.subTest(status=status):
                payload = mark_terminal(
                    mark_writer_running(_fresh_state(PHASE_ACCEPTED), provider_id="deepseek"),
                    stop_reason="stopped",
                    summary_chars=0,
                    turns=0,
                    max_turns=8,
                    provider="deepseek",
                ).to_payload()
                payload["repair_context_ref"] = CONTEXT_REF
                payload["completion_proof_ref"] = PROOF_REF_OK
                payload["completion_proof_status"] = status
                payload["completion_proof_satisfied"] = satisfied
                self.assertIsNone(RunOperationState.from_payload(payload))

        # The reachable shapes keep loading: the admitted-but-not-run stop
        # carries its failed proof...
        admitted = mark_repair_context_admitted(
            mark_completion_proof_recorded(
                _fresh_state(PHASE_WRITER_SETTLED),
                proof_ref=PROOF_REF_FAIL,
                proof_status="failed",
                proof_satisfied=False,
            ),
            context_ref=CONTEXT_REF,
        )
        stopped_early = mark_terminal(
            admitted,
            stop_reason="stopped",
            summary_chars=0,
            turns=0,
            max_turns=8,
            provider="deepseek",
        )
        self.assertEqual(
            RunOperationState.from_payload(stopped_early.to_payload()), stopped_early
        )

        # ...and a committed round means the repair ran, so any recorded
        # re-proof status is reachable on the way to terminal.
        running = mark_repair_running(admitted, provider_id="deepseek")
        for status, satisfied in (
            ("failed", False),
            ("complete", True),
            ("blocked", False),
        ):
            reproof = mark_completion_proof_recorded(
                mark_repair_settled(running, provider_id="deepseek", stop_reason="done"),
                proof_ref=PROOF_REF_OK,
                proof_status=status,
                proof_satisfied=satisfied,
            )
            ended = mark_terminal(
                reproof,
                stop_reason="stopped",
                summary_chars=0,
                turns=0,
                max_turns=8,
                provider="deepseek",
            )
            with self.subTest(reproof_status=status):
                self.assertEqual(RunOperationState.from_payload(ended.to_payload()), ended)

    def test_terminal_blocked_reason_must_match_the_snapshot(self) -> None:
        terminal = mark_terminal(
            mark_completion_blocked(
                mark_completion_proof_recorded(
                    _fresh_state(PHASE_WRITER_SETTLED),
                    proof_ref=PROOF_REF_FAIL,
                    proof_status="failed",
                    proof_satisfied=False,
                ),
                reason="unobserved",
            ),
            stop_reason="blocked",
            summary_chars=1,
            turns=1,
            max_turns=8,
            provider="deepseek",
        )
        payload = terminal.to_payload()
        payload["blocked_reason"] = ""
        self.assertIsNone(RunOperationState.from_payload(payload))

        payload = terminal.to_payload()
        payload["terminal"]["blocked_reason"] = "something_else"
        self.assertIsNone(RunOperationState.from_payload(payload))

    def test_reproof_phase_cannot_carry_a_partial_repair_record(self) -> None:
        # The table produces only two kinds of completion_proof_recorded:
        # the first proof, with no repair facts, and the post-repair
        # re-proof, with the context and at least one committed round.
        for mutate_kwargs in (
            {"repair_rounds": 1},  # a round without its admitted context
            {"repair_context_ref": CONTEXT_REF},  # a context with no round
        ):
            with self.subTest(**mutate_kwargs):
                payload = mark_completion_proof_recorded(
                    _fresh_state(PHASE_WRITER_SETTLED),
                    proof_ref=PROOF_REF_FAIL,
                    proof_status="failed",
                    proof_satisfied=False,
                ).to_payload()
                payload.update(mutate_kwargs)
                self.assertIsNone(RunOperationState.from_payload(payload))

        # The reachable re-proof keeps loading: context and rounds ride
        # along from the settled repair.
        reproof = mark_completion_proof_recorded(
            mark_repair_settled(
                mark_repair_running(
                    mark_repair_context_admitted(
                        mark_completion_proof_recorded(
                            _fresh_state(PHASE_WRITER_SETTLED),
                            proof_ref=PROOF_REF_FAIL,
                            proof_status="failed",
                            proof_satisfied=False,
                        ),
                        context_ref=CONTEXT_REF,
                    ),
                    provider_id="deepseek",
                ),
                provider_id="deepseek",
                stop_reason="done",
            ),
            proof_ref=PROOF_REF_OK,
            proof_status="complete",
            proof_satisfied=True,
        )
        self.assertEqual(RunOperationState.from_payload(reproof.to_payload()), reproof)

    def test_empty_provider_id_fails_closed(self) -> None:
        # start() refuses an empty provider id; the reader holds the same
        # bar for whatever claims to be on disk.
        payload = _fresh_state(PHASE_ACCEPTED).to_payload()
        payload["provider_id"] = ""
        self.assertIsNone(RunOperationState.from_payload(payload))

    def test_writer_facts_are_phase_reachable(self) -> None:
        # A fresh register carries nothing and a running writer has not
        # settled: facts only the later phases commit cannot ride along.
        for phase, mutate_kwargs in (
            (PHASE_ACCEPTED, {"turns_used": 3}),
            (PHASE_ACCEPTED, {"stop_reason": "done"}),
            (PHASE_ACCEPTED, {"writer_attempt": 2}),
            (PHASE_WRITER_RUNNING, {"turns_used": 3}),
            (PHASE_WRITER_RUNNING, {"stop_reason": "done"}),
        ):
            with self.subTest(phase=phase, **mutate_kwargs):
                payload = _fresh_state(phase).to_payload()
                payload.update(mutate_kwargs)
                self.assertIsNone(RunOperationState.from_payload(payload))

        # writer_settled and later keep their honest zero/empty forms: a
        # settled run that used no turns is still table-reachable, and the
        # facts ride along into the repair arm unchanged.
        settled = mark_writer_settled(
            mark_writer_running(_fresh_state(PHASE_ACCEPTED), provider_id="deepseek"),
            provider_id="deepseek",
            turns_used=0,
            stop_reason="",
        )
        self.assertEqual(RunOperationState.from_payload(settled.to_payload()), settled)

        repair_running = mark_repair_running(
            mark_repair_context_admitted(
                mark_completion_proof_recorded(
                    settled,
                    proof_ref=PROOF_REF_FAIL,
                    proof_status="failed",
                    proof_satisfied=False,
                ),
                context_ref=CONTEXT_REF,
            ),
            provider_id="deepseek",
        )
        self.assertEqual(
            RunOperationState.from_payload(repair_running.to_payload()), repair_running
        )

    def test_explicit_null_satisfied_fails_closed(self) -> None:
        # The writer omits the key when there is no proof; an explicit null
        # is not schema v1, before or after the proof boundary.
        for phase in (PHASE_ACCEPTED, PHASE_WRITER_RUNNING, PHASE_WRITER_SETTLED):
            with self.subTest(phase=phase):
                payload = _fresh_state(phase).to_payload()
                payload["completion_proof_satisfied"] = None
                self.assertIsNone(RunOperationState.from_payload(payload))

        payload = mark_completion_proof_recorded(
            _fresh_state(PHASE_WRITER_SETTLED),
            proof_ref=PROOF_REF_FAIL,
            proof_status="failed",
            proof_satisfied=False,
        ).to_payload()
        payload["completion_proof_satisfied"] = None
        self.assertIsNone(RunOperationState.from_payload(payload))

    def test_unknown_top_level_keys_fail_closed(self) -> None:
        # A payload with "extension" fields -- a raw prompt, a diff, any
        # future key -- is not schema v1 and must not load.
        for extra in ("raw_prompt", "diff", "task", "notes", "schema_version_v2"):
            with self.subTest(key=extra):
                payload = self._canonical()
                payload[extra] = "SHOULD_NEVER_BE_ACCEPTED"
                self.assertIsNone(RunOperationState.from_payload(payload))


class StrictWriterTests(unittest.TestCase):
    """The writer is held to the reader's bar: canonical facts or refused.

    Nothing is clipped or coerced -- a fact the reader could not load back
    raises ``RunOperationTransitionError`` at the transition itself, and
    ``commit()`` re-derives the canonical schema before anything touches
    the disk.
    """

    def test_writer_helpers_refuse_non_canonical_facts(self) -> None:
        accepted = _fresh_state(PHASE_ACCEPTED)
        running = mark_writer_running(accepted, provider_id="deepseek")
        settled = mark_writer_settled(
            running, provider_id="deepseek", turns_used=2, stop_reason="done"
        )
        proof = mark_completion_proof_recorded(
            settled,
            proof_ref=PROOF_REF_FAIL,
            proof_status="failed",
            proof_satisfied=False,
        )
        repair_running = mark_repair_running(
            mark_repair_context_admitted(proof, context_ref=CONTEXT_REF),
            provider_id="deepseek",
        )

        cases = [
            (mark_writer_running, accepted, {"provider_id": ""}),
            (mark_writer_running, accepted, {"provider_id": " deepseek "}),
            (mark_writer_running, accepted, {"provider_id": 7}),
            (mark_writer_running, accepted, {"provider_id": "x" * 81}),
            (mark_writer_running, accepted, {"provider_id": "deepseek", "writer_attempt": 0}),
            (mark_writer_running, accepted, {"provider_id": "deepseek", "writer_attempt": True}),
            (mark_writer_running, accepted, {"provider_id": "deepseek", "writer_attempt": "2"}),
            (
                mark_writer_settled,
                running,
                {"provider_id": "deepseek", "turns_used": "3", "stop_reason": "done"},
            ),
            (
                mark_writer_settled,
                running,
                {"provider_id": "deepseek", "turns_used": True, "stop_reason": "done"},
            ),
            (
                mark_writer_settled,
                running,
                {"provider_id": "deepseek", "turns_used": -1, "stop_reason": "done"},
            ),
            (
                mark_writer_settled,
                running,
                {"provider_id": "deepseek", "turns_used": 1.5, "stop_reason": "done"},
            ),
            (
                mark_writer_settled,
                running,
                {"provider_id": "deepseek", "turns_used": 3, "stop_reason": None},
            ),
            (
                mark_completion_proof_recorded,
                settled,
                {"proof_ref": "", "proof_status": "failed", "proof_satisfied": False},
            ),
            (
                mark_completion_proof_recorded,
                settled,
                {"proof_ref": PROOF_REF_FAIL, "proof_status": "", "proof_satisfied": False},
            ),
            (
                mark_completion_proof_recorded,
                settled,
                {"proof_ref": PROOF_REF_FAIL, "proof_status": "failed", "proof_satisfied": None},
            ),
            (
                mark_completion_proof_recorded,
                settled,
                {"proof_ref": PROOF_REF_FAIL, "proof_status": "failed", "proof_satisfied": "yes"},
            ),
            (
                mark_completion_proof_recorded,
                settled,
                {"proof_ref": "E:/secret/project", "proof_status": "failed", "proof_satisfied": False},
            ),
            (
                mark_completion_proof_recorded,
                settled,
                {
                    "proof_ref": "proof:0123456789abcdef",
                    "proof_status": "failed",
                    "proof_satisfied": False,
                },
            ),
            (
                mark_completion_proof_recorded,
                settled,
                {
                    "proof_ref": "completion_proof:" + "g" * 16,
                    "proof_status": "failed",
                    "proof_satisfied": False,
                },
            ),
            (
                mark_completion_proof_recorded,
                settled,
                {
                    "proof_ref": "completion_proof:" + "a" * 15,
                    "proof_status": "failed",
                    "proof_satisfied": False,
                },
            ),
            (
                mark_completion_proof_recorded,
                settled,
                {"proof_ref": PROOF_REF_FAIL, "proof_status": "nonsense", "proof_satisfied": False},
            ),
            (
                mark_completion_proof_recorded,
                settled,
                {"proof_ref": PROOF_REF_FAIL, "proof_status": "pending", "proof_satisfied": False},
            ),
            (
                mark_completion_proof_recorded,
                settled,
                {"proof_ref": PROOF_REF_FAIL, "proof_status": "failed", "proof_satisfied": True},
            ),
            (
                mark_completion_proof_recorded,
                settled,
                {"proof_ref": PROOF_REF_OK, "proof_status": "complete", "proof_satisfied": False},
            ),
            (
                mark_completion_proof_recorded,
                settled,
                {
                    "proof_ref": PROOF_REF_OK,
                    "proof_status": "complete_with_limitations",
                    "proof_satisfied": True,
                },
            ),
            (
                mark_completion_proof_recorded,
                settled,
                {"proof_ref": PROOF_REF_FAIL, "proof_status": "failed", "proof_satisfied": 1},
            ),
            (
                mark_completion_proof_recorded,
                settled,
                {"proof_ref": PROOF_REF_OK, "proof_status": "complete", "proof_satisfied": 0},
            ),
            (
                mark_completion_proof_recorded,
                settled,
                {"proof_ref": "completion_proof:" + "a" * 121, "proof_status": "failed", "proof_satisfied": False},
            ),
            (mark_repair_context_admitted, proof, {"context_ref": ""}),
            (mark_repair_context_admitted, proof, {"context_ref": "ctx"}),
            (mark_repair_context_admitted, proof, {"context_ref": "sha256:" + "g" * 64}),
            (mark_repair_context_admitted, proof, {"context_ref": "sha256:" + "a" * 65}),
            (
                mark_repair_settled,
                repair_running,
                {"provider_id": "deepseek", "stop_reason": "done", "turns_used": "6"},
            ),
            (
                mark_terminal,
                running,
                {
                    "stop_reason": "done",
                    "summary_chars": "3",
                    "turns": 2,
                    "max_turns": 8,
                    "provider": "deepseek",
                },
            ),
            (
                mark_terminal,
                running,
                {
                    "stop_reason": "done",
                    "summary_chars": 3,
                    "turns": True,
                    "max_turns": 8,
                    "provider": "deepseek",
                },
            ),
            (
                mark_terminal,
                running,
                {
                    "stop_reason": "done",
                    "summary_chars": 3,
                    "turns": 2,
                    "max_turns": -1,
                    "provider": "deepseek",
                },
            ),
            (mark_completion_blocked, proof, {"reason": ""}),
        ]
        for fn, state, kwargs in cases:
            with self.subTest(transition=fn.__name__, **kwargs):
                with self.assertRaises(RunOperationTransitionError):
                    fn(state, **kwargs)

    def test_writer_helpers_refuse_counts_above_turn_budget(self) -> None:
        accepted = _fresh_state(PHASE_ACCEPTED, turn_budget=2)
        running = mark_writer_running(accepted, provider_id="deepseek")
        with self.assertRaises(RunOperationTransitionError):
            mark_writer_settled(
                running,
                provider_id="deepseek",
                turns_used=3,
                stop_reason="done",
            )

        with self.assertRaises(RunOperationTransitionError):
            mark_terminal(
                running,
                stop_reason="done",
                summary_chars=3,
                turns=3,
                max_turns=2,
                provider="deepseek",
            )

        with self.assertRaises(RunOperationTransitionError):
            mark_terminal(
                running,
                stop_reason="done",
                summary_chars=3,
                turns=2,
                max_turns=3,
                provider="deepseek",
            )

    def test_over_length_facts_are_refused_never_clipped(self) -> None:
        accepted = _fresh_state(PHASE_ACCEPTED)
        with self.assertRaises(RunOperationTransitionError):
            mark_writer_running(accepted, provider_id="x" * 500)

        settled = mark_writer_settled(
            mark_writer_running(accepted, provider_id="deepseek"),
            provider_id="deepseek",
            turns_used=1,
            stop_reason="done",
        )
        with self.assertRaises(RunOperationTransitionError):
            mark_completion_proof_recorded(
                settled,
                proof_ref="completion_proof:" + "a" * 500,
                proof_status="failed",
                proof_satisfied=False,
            )

        proof = mark_completion_proof_recorded(
            settled,
            proof_ref=PROOF_REF_FAIL,
            proof_status="failed",
            proof_satisfied=False,
        )
        with self.assertRaises(RunOperationTransitionError):
            mark_completion_blocked(proof, reason="r" * 500)

    def test_every_strict_transition_still_round_trips(self) -> None:
        # Strictness must not bend the happy path: every helper's output is
        # exactly what the reader loads back, no clipping, no rewriting.
        settled = mark_writer_settled(
            mark_writer_running(_fresh_state(PHASE_ACCEPTED), provider_id="deepseek"),
            provider_id="deepseek",
            turns_used=4,
            stop_reason="done",
        )
        proof = mark_completion_proof_recorded(
            settled,
            proof_ref=PROOF_REF_OK,
            proof_status="complete",
            proof_satisfied=True,
        )
        terminal = mark_terminal(
            proof,
            stop_reason="done",
            summary_chars=42,
            turns=4,
            max_turns=8,
            provider="deepseek",
        )
        for state in (settled, proof, terminal):
            with self.subTest(phase=state.phase):
                self.assertEqual(RunOperationState.from_payload(state.to_payload()), state)

    def test_commit_refuses_to_write_a_state_the_reader_would_reject(self) -> None:
        # Even a transition that bypasses the helpers cannot poison the
        # register: commit re-derives the canonical schema first, and the
        # file keeps its last valid state.
        with tempfile.TemporaryDirectory() as td:
            store = _store(Path(td))
            started = _start(store)
            path = store.path_for(SESSION, RUN)
            before = path.read_text(encoding="utf-8")

            with self.assertRaises(RunOperationTransitionError):
                store.commit(
                    SESSION,
                    RUN,
                    lambda state: replace(state, provider_id="x" * 500),
                )

            self.assertEqual(path.read_text(encoding="utf-8"), before)
            self.assertEqual(store.load(SESSION, RUN), started)

    def test_commit_refuses_a_transition_that_moves_identity(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = _store(Path(td))
            started = _start(store)

            with self.assertRaises(RunOperationTransitionError):
                store.commit(
                    SESSION,
                    RUN,
                    lambda state: replace(state, run_id="run-op-2"),
                )

            self.assertEqual(store.load(SESSION, RUN), started)

    def test_commit_propagates_transition_refusals_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = _store(Path(td))
            started = _start(store)

            with self.assertRaises(RunOperationTransitionError):
                store.commit(
                    SESSION,
                    RUN,
                    lambda state: mark_repair_running(state, provider_id="deepseek"),
                )

            self.assertEqual(store.load(SESSION, RUN), started)


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
                    proof_ref=PROOF_REF_FAIL,
                    proof_status="failed",
                    proof_satisfied=False,
                ),
                context_ref="sha256:" + "b" * 64,
            )
            state = mark_repair_running(state, provider_id="deepseek")
            state = mark_repair_settled(state, provider_id="deepseek", stop_reason="done")
            state = mark_completion_proof_recorded(
                state,
                proof_ref=PROOF_REF_FAIL_2,
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
        # The verdict lands on a recorded proof that failed the run -- the
        # fixture is a real recorded failed proof, not a bare phase.
        state = mark_completion_proof_recorded(
            _fresh_state(PHASE_WRITER_SETTLED),
            proof_ref=PROOF_REF_FAIL,
            proof_status="failed",
            proof_satisfied=False,
        )
        marked = mark_completion_blocked(state, reason="unobserved")
        self.assertEqual(marked.blocked_reason, "unobserved")
        self.assertEqual(marked.phase, PHASE_COMPLETION_PROOF_RECORDED)

    def test_blocked_verdict_requires_an_unsatisfied_failed_proof(self) -> None:
        # A complete, limited, or missing proof can never carry the blocked
        # verdict -- the decision blocks on the proof that failed the run.
        cases = [
            {},
            {
                "completion_proof_ref": PROOF_REF_OK,
                "completion_proof_status": "complete",
                "completion_proof_satisfied": True,
            },
            {
                "completion_proof_ref": PROOF_REF_OK,
                "completion_proof_status": "complete_with_limitations",
                "completion_proof_satisfied": False,
            },
            {
                "completion_proof_ref": PROOF_REF_FAIL,
                "completion_proof_status": "failed",
                "completion_proof_satisfied": True,
            },
        ]
        for overrides in cases:
            state = _fresh_state(PHASE_COMPLETION_PROOF_RECORDED, **overrides)
            with self.subTest(**overrides):
                with self.assertRaises(RunOperationTransitionError):
                    mark_completion_blocked(state, reason="unobserved")

        for status in ("failed", "blocked"):
            with self.subTest(status=status):
                proof = mark_completion_proof_recorded(
                    _fresh_state(PHASE_WRITER_SETTLED),
                    proof_ref=PROOF_REF_FAIL,
                    proof_status=status,
                    proof_satisfied=False,
                )
                marked = mark_completion_blocked(proof, reason="unobserved")
                self.assertEqual(marked.blocked_reason, "unobserved")

    def test_terminal_and_repair_settled_refuse_unbacked_blocked_verdicts(self) -> None:
        # The same rule the reader enforces binds the writer's verdict
        # carriers: terminal and a settled repair may only end blocked on
        # an unsatisfied failed/blocked proof.
        with self.assertRaises(RunOperationTransitionError):
            mark_terminal(
                _fresh_state(PHASE_ACCEPTED),
                stop_reason="stopped",
                summary_chars=0,
                turns=0,
                max_turns=8,
                provider="deepseek",
                blocked_reason="unobserved",
            )

        # A complete-proof repair position cannot claim a provider failure.
        # Such a position is not table-reachable -- admission refuses it --
        # so it is built directly to exercise the verdict guard.
        running = _fresh_state(
            PHASE_REPAIR_RUNNING,
            completion_proof_ref=PROOF_REF_OK,
            completion_proof_status="complete",
            completion_proof_satisfied=True,
            repair_context_ref=CONTEXT_REF,
            repair_rounds=1,
        )
        with self.assertRaises(RunOperationTransitionError):
            mark_repair_settled(
                running,
                provider_id="deepseek",
                stop_reason="done",
                blocked_reason="provider_failure",
            )

    def test_only_a_failed_proof_admits_the_repair_arm(self) -> None:
        # The repair arm exists for product failures: a complete, limited,
        # or blocked proof never sends the run into repair -- the completion
        # projection admits a context for unsatisfied failed proofs only.
        settled = mark_writer_settled(
            mark_writer_running(_fresh_state(PHASE_ACCEPTED), provider_id="deepseek"),
            provider_id="deepseek",
            turns_used=2,
            stop_reason="done",
        )
        for status, satisfied in (
            ("complete", True),
            ("complete_with_limitations", False),
            ("blocked", False),
        ):
            proof = mark_completion_proof_recorded(
                settled,
                proof_ref=PROOF_REF_OK,
                proof_status=status,
                proof_satisfied=satisfied,
            )
            with self.subTest(status=status):
                with self.assertRaises(RunOperationTransitionError):
                    mark_repair_context_admitted(proof, context_ref=CONTEXT_REF)

        failed = mark_completion_proof_recorded(
            settled,
            proof_ref=PROOF_REF_FAIL,
            proof_status="failed",
            proof_satisfied=False,
        )
        admitted = mark_repair_context_admitted(failed, context_ref=CONTEXT_REF)
        self.assertEqual(RunOperationState.from_payload(admitted.to_payload()), admitted)

    def test_blocked_verdict_is_final_until_terminal(self) -> None:
        # Once the verdict is on the counter, the run may only end: the
        # blocked proof cannot admit a repair, and the provider-failure
        # settle cannot re-proof -- not even into a failed proof that would
        # keep the stale verdict looking fresh.
        settled = mark_writer_settled(
            mark_writer_running(_fresh_state(PHASE_ACCEPTED), provider_id="deepseek"),
            provider_id="deepseek",
            turns_used=2,
            stop_reason="done",
        )
        blocked_proof = mark_completion_blocked(
            mark_completion_proof_recorded(
                settled,
                proof_ref=PROOF_REF_FAIL,
                proof_status="failed",
                proof_satisfied=False,
            ),
            reason="unobserved",
        )
        with self.assertRaises(RunOperationTransitionError):
            mark_repair_context_admitted(blocked_proof, context_ref=CONTEXT_REF)
        ended = mark_terminal(
            blocked_proof,
            stop_reason="blocked",
            summary_chars=1,
            turns=1,
            max_turns=8,
            provider="deepseek",
        )
        self.assertEqual(RunOperationState.from_payload(ended.to_payload()), ended)

        failed_settled = mark_repair_settled(
            mark_repair_running(
                mark_repair_context_admitted(
                    mark_completion_proof_recorded(
                        settled,
                        proof_ref=PROOF_REF_FAIL,
                        proof_status="failed",
                        proof_satisfied=False,
                    ),
                    context_ref=CONTEXT_REF,
                ),
                provider_id="deepseek",
            ),
            provider_id="deepseek",
            stop_reason="",
            blocked_reason="provider_failure",
        )
        for status, satisfied in (("complete", True), ("failed", False)):
            with self.subTest(reproof_status=status):
                with self.assertRaises(RunOperationTransitionError):
                    mark_completion_proof_recorded(
                        failed_settled,
                        proof_ref=PROOF_REF_OK,
                        proof_status=status,
                        proof_satisfied=satisfied,
                    )
        verdict_terminal = mark_terminal(
            failed_settled,
            stop_reason="stopped",
            summary_chars=0,
            turns=0,
            max_turns=8,
            provider="deepseek",
        )
        self.assertEqual(
            RunOperationState.from_payload(verdict_terminal.to_payload()), verdict_terminal
        )


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
                    proof_ref=PROOF_REF_FAIL,
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

    fields = {
        "session_id": SESSION,
        "run_id": RUN,
        "project_ref": "",
        "provider_id": "deepseek",
        "turn_budget": 8,
        "max_repair_rounds": 1,
        "phase": phase,
        "started_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        **overrides,
    }
    return RunOperationState(**fields)  # type: ignore[arg-type]


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
            proof_ref=PROOF_REF_FAIL,
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
    def test_long_run_ids_use_digest_stems_without_colliding(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = _store(Path(td))
            prefix = "run-" + "a" * 160
            first = prefix + "x"
            second = prefix + "y"

            self.assertNotEqual(store.path_for(SESSION, first), store.path_for(SESSION, second))
            self.assertIsNotNone(store.start(
                session_id=SESSION,
                run_id=first,
                project="",
                provider_id="deepseek",
                turn_budget=8,
                max_repair_rounds=1,
            ))
            self.assertIsNotNone(store.start(
                session_id=SESSION,
                run_id=second,
                project="",
                provider_id="deepseek",
                turn_budget=8,
                max_repair_rounds=1,
            ))

            self.assertIsNotNone(store.load(SESSION, first))
            self.assertIsNotNone(store.load(SESSION, second))

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

    def test_delete_session_does_not_follow_symlink_bucket(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = _store(root)
            target = root / "outside-target"
            target.mkdir()
            sentinel = target / "keep.json"
            sentinel.write_text("keep", encoding="utf-8")
            bucket = store.session_dir(SESSION)
            bucket.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.symlink(target, bucket, target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                raise unittest.SkipTest(f"symlink creation unavailable: {exc}") from exc

            store.delete_session(SESSION)

            self.assertTrue(sentinel.exists())
            self.assertTrue(bucket.is_symlink())

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
