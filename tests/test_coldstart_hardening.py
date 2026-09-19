"""Regression tests for the cold-start hardening batch.

Covers: task_runtime settle propagation + turn-budget clamp, operation-state
load fail-closed, EventBus overflow accounting + SSE resync cursor, provider
worker self-heal, provider probe_error signal, conversation prune robustness,
research result-id normalization, headless shell-approval expiry, and the
POST body-read timeout.
"""

from __future__ import annotations

import queue
import socket
import tempfile
import threading
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from codey.app import api as app_api
from codey.app.event_bus import EventBus, EventSubscriber, SsePayload
from codey.app.headless_runner import HeadlessAppContext
from codey.providers import DEFAULT_PROVIDER_ID, PROVIDER_LABELS
from codey.providers.worker import WorkerChatProvider
from codey.research.controller import ResearchController
from codey.runtime.core.operation_state import (
    RuntimeOperationStore,
    RuntimeOperationTransitionError,
)
from codey.runtime.log.session_log import RuntimeSessionLog
from codey.runtime.write.mutation_line import RuntimeMutationLine
from codey.runtime.write.task_runtime import TaskRuntime, _turn_budget
from codey.storage.conversation_store import ConversationStore
from codey.task.model import TaskSubmission


def _submission(**overrides: object) -> TaskSubmission:
    values: dict[str, object] = {
        "session_id": "s1",
        "project": None,
        "task": "do it",
        "max_turns": 3,
        "continue_task": False,
        "provider_id": "qwen",
        "run_id": "run-1",
    }
    values.update(overrides)
    return TaskSubmission(**values)  # type: ignore[arg-type]


class TurnBudgetTests(unittest.TestCase):
    def test_turn_budget_clamps_to_min_one(self) -> None:
        self.assertEqual(_turn_budget(_submission(max_turns=0)), 1)
        self.assertEqual(_turn_budget(_submission(max_turns=-5)), 1)
        self.assertEqual(_turn_budget(_submission(max_turns=7)), 7)

    def test_turn_budget_falls_back_to_one_on_garbage(self) -> None:
        self.assertEqual(_turn_budget(_submission(max_turns="abc")), 1)


class SettlePropagationTests(unittest.TestCase):
    def test_settle_failure_is_not_swallowed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            runtime = TaskRuntime(RuntimeSessionLog(Path(td)), executor=lambda req: None)
            with mock.patch.object(
                runtime.mutations,
                "mark_terminal",
                side_effect=RuntimeOperationTransitionError("boom"),
            ):
                with self.assertRaises(RuntimeOperationTransitionError):
                    runtime._settle_if_open(_submission(), SimpleNamespace(summary="", status="completed", reason=""))

    def test_settle_is_noop_when_inner_flow_already_settled(self) -> None:
        from codey.runtime.core.outcome import OperationOutcome

        with tempfile.TemporaryDirectory() as td:
            runtime = TaskRuntime(RuntimeSessionLog(Path(td)), executor=lambda req: None)
            request = _submission()
            runtime.mutations.accept_operation(
                session_id=request.session_id,
                run_id=request.run_id,
                project="",
                provider_id=request.provider_id,
                turn_budget=3,
                max_repair_rounds=1,
                task_kind="task",
            )
            runtime.mutations.mark_terminal(
                request.session_id,
                request.run_id,
                stop_reason="done",
                summary_chars=4,
                turns=1,
                max_turns=3,
                provider=request.provider_id,
            )
            # Must not raise even though turns/max_turns differ from the backstop's.
            runtime._settle_if_open(request, OperationOutcome.completed(summary="done!"))


class OperationStoreLoadTests(unittest.TestCase):
    def test_load_returns_none_when_log_missing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = RuntimeOperationStore(RuntimeSessionLog(Path(td)))
            self.assertIsNone(store.load("nope", "run-9"))

    def test_load_returns_state_for_healthy_log(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            log = RuntimeSessionLog(Path(td))
            RuntimeMutationLine(log).accept_operation(
                session_id="s1",
                run_id="run-1",
                project=".",
                provider_id="qwen",
                turn_budget=3,
                max_repair_rounds=1,
                task_kind="project",
            )
            state = RuntimeOperationStore(log).load("s1", "run-1")
        self.assertIsNotNone(state)

    def test_load_raises_instead_of_returning_none_on_corrupt_tail(self) -> None:
        import json

        with tempfile.TemporaryDirectory() as td:
            log = RuntimeSessionLog(Path(td))
            RuntimeMutationLine(log).accept_operation(
                session_id="s1",
                run_id="run-1",
                project=".",
                provider_id="qwen",
                turn_budget=3,
                max_repair_rounds=1,
                task_kind="project",
            )
            # Simulate on-disk corruption from a stale writer: a well-formed
            # envelope carrying an operation_state payload with an illegal leaf.
            envelope = next(
                entry.to_payload()
                for entry in log.entries("s1")
                if entry.kind == "operation_state"
            )
            envelope["entry_id"] = "entry-corrupt-tail"
            envelope["batch_id"] = "batch-corrupt-tail"
            envelope["batch_index"] = 0
            envelope["batch_count"] = 1
            envelope["payload"] = {
                **envelope["payload"],
                "leaf": "nope-not-a-leaf",
            }
            path = log.path_for("s1")
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(envelope, ensure_ascii=False) + "\n")
            with self.assertRaises(RuntimeOperationTransitionError):
                RuntimeOperationStore(log).load("s1", "run-1")


class EventBusOverflowTests(unittest.TestCase):
    def test_overflow_keeps_a_resync_marker(self) -> None:
        bus = EventBus(replay_limit=16)
        sub = bus.subscribe(maxsize=2)
        for index in range(5):
            bus.emit({"type": "turn", "turn": index})
        rows = []
        while True:
            try:
                rows.append(sub.get_nowait())
            except queue.Empty:
                break
        markers = [row for row in rows if row.get("type") == "resync_required"]
        self.assertTrue(markers)
        self.assertGreaterEqual(markers[-1]["dropped"], 1)

    def test_double_full_retains_the_drop_count(self) -> None:
        sub = EventSubscriber(maxsize=2)
        payload = SsePayload({"type": "turn", "turn": 1}, event_id=1)
        with mock.patch.object(sub, "put_nowait", side_effect=queue.Full):
            EventBus._put_for_subscriber(sub, payload, {"type": "turn"})
        self.assertEqual(sub.dropped, 1)

    def test_expired_replay_is_marker_only_and_never_repeats(self) -> None:
        bus = EventBus(replay_limit=4)
        for index in range(6):
            bus.emit({"type": "turn", "turn": index})
        rows = bus.replay_events_after(1)
        self.assertEqual(len(rows), 1)
        marker_id, marker = rows[0]
        self.assertEqual(marker["type"], "resync_required")
        self.assertGreater(marker_id, 1)
        # Adopting the marker strictly advances the cursor: the off-by-one
        # case (cursor == oldest_retained - 1) must not resync twice.
        self.assertEqual(bus.replay_events_after(marker_id), [])
        # Any other expired cursor converges on the same marker id.
        self.assertEqual(bus.replay_events_after(2)[0][0], marker_id)


class WorkerSelfHealTests(unittest.TestCase):
    def _provider(self) -> WorkerChatProvider:
        provider = WorkerChatProvider.__new__(WorkerChatProvider)
        provider.provider_id = "qwen"
        provider.override = SimpleNamespace(root=Path("."), generation=1)
        provider.port = 19222
        provider.state_home = Path(".")
        provider.name = "qwen worker"
        provider.last_failure = None
        provider._proc = None
        provider._job = None
        provider._responses = queue.Queue()
        provider._lock = threading.Lock()
        provider._reader = None
        provider._cdp_port = 0
        provider._target_id = ""
        provider._stderr_tail = deque(maxlen=24)
        return provider

    def test_ensure_running_restarts_dead_worker_and_drops_stale_ids(self) -> None:
        provider = self._provider()
        provider._responses.put({"id": "stale-from-last-life"})
        fake_proc = mock.Mock()
        fake_proc.poll.return_value = None
        fake_proc.stdin = mock.Mock()
        with mock.patch.object(
            WorkerChatProvider, "_start", lambda self: setattr(self, "_proc", fake_proc),
        ) as _ignored:
            running = provider._ensure_running_locked()
        self.assertIs(running, fake_proc)
        self.assertTrue(provider._responses.empty())

    def test_drain_responses_empties_the_queue(self) -> None:
        provider = self._provider()
        provider._responses.put({"id": "a"})
        provider._responses.put({"id": "b"})
        provider._drain_responses()
        self.assertTrue(provider._responses.empty())

    def test_close_never_restarts_a_dead_worker(self) -> None:
        provider = self._provider()
        with mock.patch.object(
            WorkerChatProvider, "_start",
            side_effect=AssertionError("close() must not restart"),
        ):
            provider.close()  # _proc is None
        self.assertIsNone(provider._proc)

    def test_close_never_restarts_an_exited_worker(self) -> None:
        provider = self._provider()
        exited = mock.Mock()
        exited.poll.return_value = 1
        exited.stdin = mock.Mock()
        provider._proc = exited
        with (
            mock.patch.object(
                WorkerChatProvider, "_start",
                side_effect=AssertionError("close() must not restart"),
            ),
            mock.patch("codey.providers.worker.cancellation.terminate_process_tree"),
        ):
            provider.close()
        self.assertIsNone(provider._proc)

    def test_close_skips_timeout_grace(self) -> None:
        provider = self._provider()
        seen: dict[str, object] = {}

        def _capture(self, method: str, params: dict, timeout: float | None, *, restart: bool, grace: bool):
            seen["grace"] = grace
            seen["restart"] = restart
            raise RuntimeError("no child")

        with (
            mock.patch.object(WorkerChatProvider, "_request_locked", _capture),
            mock.patch.object(provider, "_terminate"),
        ):
            provider.close()
        self.assertIs(seen.get("grace"), False)
        self.assertIs(seen.get("restart"), False)


class ProviderProbeErrorTests(unittest.TestCase):
    def test_probe_crash_is_signalled_not_silent(self) -> None:
        ctx = SimpleNamespace()
        with (
            mock.patch(
                "codey.app.api.services.provider_availability",
                side_effect=RuntimeError("probe exploded"),
            ),
            self.assertLogs("codey.app.api", level="ERROR") as captured,
        ):
            status, payload = app_api.providers_response(ctx)
        self.assertEqual(status, 200)
        self.assertTrue(payload["probe_error"])
        self.assertTrue(all(item["available"] is False for item in payload["providers"]))
        self.assertTrue(any("probe" in message for message in captured.output))

    def test_healthy_probe_reports_no_error(self) -> None:
        ctx = SimpleNamespace()
        with mock.patch(
            "codey.app.api.services.provider_availability",
            return_value={"deepseek": True},
        ):
            status, payload = app_api.providers_response(ctx)
        self.assertEqual(status, 200)
        self.assertFalse(payload["probe_error"])

    def test_providers_response_carries_backend_catalog(self) -> None:
        ctx = SimpleNamespace()
        with mock.patch(
            "codey.app.api.services.provider_availability",
            return_value={},
        ):
            status, payload = app_api.providers_response(ctx)
        self.assertEqual(status, 200)
        self.assertEqual(payload["default"], DEFAULT_PROVIDER_ID)
        self.assertEqual(
            [item["id"] for item in payload["providers"]],
            list(PROVIDER_LABELS),
        )
        self.assertTrue(all("label" in item for item in payload["providers"]))

    def test_provider_catalog_never_probes(self) -> None:
        with (
            mock.patch(
                "codey.app.api.services.provider_availability",
                side_effect=AssertionError("catalog must not probe"),
            ),
            mock.patch(
                "codey.app.api.services.provider_tab_availability",
                side_effect=AssertionError("catalog must not probe"),
            ),
        ):
            status, payload = app_api.provider_catalog_response()
        self.assertEqual(status, 200)
        self.assertEqual(payload["default"], DEFAULT_PROVIDER_ID)
        self.assertEqual(
            [item["id"] for item in payload["providers"]],
            list(PROVIDER_LABELS),
        )
        self.assertTrue(all("label" in item for item in payload["providers"]))
        self.assertTrue(all("available" not in item for item in payload["providers"]))


class ConversationPruneTests(unittest.TestCase):
    def test_prune_skips_unstatable_files_instead_of_abandoning(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = ConversationStore(Path(td))
            store.directory.mkdir(parents=True, exist_ok=True)
            keep = store.directory / "keep.json"
            keep.write_text("{}", encoding="utf-8")
            victims: list[Path] = []
            for index in range(70):
                path = store.directory / f"c{index:03d}.json"
                path.write_text("{}", encoding="utf-8")
                victims.append(path)
            victim = store.directory / "c000.json"
            real_stat = Path.stat

            def flaky_stat(self: Path):
                if self.name == victim.name:
                    raise OSError("disk hiccup")
                return real_stat(self)

            with mock.patch.object(Path, "stat", flaky_stat):
                store._prune(keep)  # must not raise
            remaining = list(store.directory.glob("*.json"))
            # 63 newest stat-able + keep + the un-statable survivor.
            self.assertEqual(len(remaining), 65)
            self.assertTrue(victim.exists())


class ResearchUrlKeyTests(unittest.TestCase):
    def test_result_ids_use_normalized_url_keys(self) -> None:
        controller = ResearchController()
        ledger = SimpleNamespace(searches=[
            SimpleNamespace(results=[
                SimpleNamespace(url="HTTPS://Example.COM/a/", title="A", snippet="x"),
                SimpleNamespace(url="https://www.example.com/a", title="A", snippet="x"),
                SimpleNamespace(url="https://example.com/a", title="A", snippet="x"),
            ]),
        ])
        rows = controller._result_rows(ledger)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "r1")


class HeadlessShellExpiryTests(unittest.TestCase):
    def test_shell_request_expires_pending_approvals(self) -> None:
        rows: list[dict[str, object]] = []
        with tempfile.TemporaryDirectory() as td:
            ctx = HeadlessAppContext(
                Path(td, "state"),
                port=19222,
                emit_jsonl=rows.append,
            )
            ctx.add_pending_shell_approval("appr-1", {
                "id": "appr-1",
                "run_id": "r1",
                "session_id": "s1",
                "command": "pytest -q",
                "cwd": ".",
            })
            ctx.emit({
                "type": "shell_request",
                "run_id": "r1",
                "session_id": "s1",
                "id": "appr-1",
                "command": "pytest -q",
                "cwd": ".",
            })
            pending = ctx.pending_shell_approvals()
        self.assertTrue(ctx.shell_rejected)
        self.assertTrue(ctx.run_registry.stop_flag.is_set())
        self.assertEqual(pending, {})
        self.assertTrue(any(row.get("type") == "shell_rejected" for row in rows))


class PostBodyTimeoutTests(unittest.TestCase):
    def test_slow_body_returns_408(self) -> None:
        from codey.app.server import Handler

        handler = Handler.__new__(Handler)
        handler.rfile = SimpleNamespace(read=mock.Mock(side_effect=socket.timeout))
        handler.connection = SimpleNamespace(
            gettimeout=mock.Mock(return_value=None),
            settimeout=mock.Mock(),
        )
        sent: list[tuple[int, dict]] = []
        handler._send_json = lambda status, payload: sent.append((status, payload))  # type: ignore[method-assign]
        self.assertIsNone(handler._read_post_body(16))
        self.assertEqual(sent[0][0], 408)


if __name__ == "__main__":
    unittest.main()
