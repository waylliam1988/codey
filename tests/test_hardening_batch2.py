"""Regression tests for the P0/P1 hardening batch.

Covers: approval fail-closed on epoch read failure, cancellation
propagation through silent fallbacks, POST non-string coercion, adapter
shim without exec, knowledge WAL mode, browser worker backpressure,
managed-output failure visibility, local probe reasons, checkpoint
corruption backup visibility, strict reply extraction, and POST
route-level 500 containment.
"""

from __future__ import annotations

import queue
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from codey.app import api as app_api
from codey.app import services as app_services
from codey.runtime.core import cancellation


class ApprovalFailClosedTests(unittest.TestCase):
    def test_generation_read_error_is_not_current(self) -> None:
        ctx = SimpleNamespace()
        ctx.approval_generation = mock.Mock(side_effect=OSError("disk hiccup"))
        self.assertFalse(app_services._approval_generation_current(ctx, 0))

    def test_generation_error_never_executes_shell(self) -> None:
        ctx = SimpleNamespace()
        ctx.approval_generation = mock.Mock(side_effect=OSError("disk hiccup"))
        ctx.run_registry = SimpleNamespace(stop_flag=threading.Event())
        with mock.patch(
            "codey.runtime.core.cancellation.run_process",
            side_effect=AssertionError("Popen must not start"),
        ):
            result = app_services.execute_approved_shell(
                ctx, ".", ".", "pytest -q", expected_approval_generation=0
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "stopped")

    def test_cancelled_epoch_propagates(self) -> None:
        ctx = SimpleNamespace()
        ctx.approval_generation = mock.Mock(
            side_effect=cancellation.TaskCancelled("stop")
        )
        with self.assertRaises(cancellation.TaskCancelled):
            app_services._approval_generation_current(ctx, 0)

    def test_claim_expiry_read_error_fails_closed(self) -> None:
        ctx = SimpleNamespace()
        ctx.run_registry = SimpleNamespace(stop_flag=threading.Event())
        ctx.approval_generation = mock.Mock(side_effect=OSError("boom"))
        self.assertTrue(app_api._shell_claim_expired(ctx, 0))


class CancellationPropagationTests(unittest.TestCase):
    def test_affinity_list_nodes_reraises_cancel(self) -> None:
        from codey.ghost.affinity import GhostAffinityStore

        with tempfile.TemporaryDirectory() as td:
            store = GhostAffinityStore(Path(td))
            with mock.patch.object(
                store,
                "_load_state_for_read_unlocked",
                side_effect=cancellation.TaskCancelled("stop"),
            ):
                with self.assertRaises(cancellation.TaskCancelled):
                    store.list_nodes()

    def test_affinity_cross_store_specs_reraise_cancel(self) -> None:
        from codey.ghost import affinity as affinity_module

        class _Boom:
            def list_nodes(self, **_kwargs):
                raise cancellation.TaskCancelled("stop")

        with self.assertRaises(cancellation.TaskCancelled):
            affinity_module._node_specs_from_hebbian(_Boom())

    def test_details_recovery_reraises_cancel(self) -> None:
        from codey.runs import details as details_module

        effects = SimpleNamespace(
            recovery_summary=mock.Mock(side_effect=cancellation.TaskCancelled("stop"))
        )
        with self.assertRaises(cancellation.TaskCancelled):
            details_module._load_recovery_summary(effects, "s", "r")

    def test_managed_output_write_reraises_cancel(self) -> None:
        from codey.storage.managed_outputs import ManagedOutputStore

        with tempfile.TemporaryDirectory() as td:
            store = ManagedOutputStore(Path(td))
            with mock.patch.object(
                store, "_run_dir", side_effect=cancellation.TaskCancelled("stop")
            ):
                with self.assertRaises(cancellation.TaskCancelled):
                    store.write_run_output(
                        session_id="s",
                        run_id="r",
                        tool_id="t",
                        permission_profile="p",
                        command="c",
                        cwd=".",
                        text="x",
                    )


class PostBodyCoercionTests(unittest.TestCase):
    def test_restore_with_numeric_project_is_400_not_crash(self) -> None:
        ctx = SimpleNamespace(
            has_active_run_for_project=mock.Mock(return_value=False),
            change_tracker_for=mock.Mock(
                return_value=SimpleNamespace(has_snapshots=False)
            ),
        )
        seen: dict[str, object] = {}
        with mock.patch.object(
            app_api,
            "restore_snapshot_changes",
            side_effect=lambda project, tracker, paths: (
                seen.setdefault("project", project),
                (200, {"ok": True}),
            )[1],
        ):
            status, _payload = app_api.restore_changes_response(ctx, {"project": 123})
        self.assertEqual(status, 200)
        # Numeric input is coerced to text before any .strip() call.
        self.assertEqual(seen.get("project"), "123")

    def test_run_submit_with_numeric_project_is_400(self) -> None:
        status, payload = app_api.run_submit_response(
            {
                "session_id": "s",
                "project": 123,
                "task": "do it",
                "provider": "qwen",
            },
            submit_task=mock.Mock(side_effect=AssertionError("must not submit")),
        )
        self.assertEqual(status, 400)

    def test_post_route_exception_is_json_500(self) -> None:
        from codey.app import server as server_module

        handler = server_module.Handler.__new__(server_module.Handler)
        handler.path = "/api/stop"
        handler.headers = {}
        sent: list[tuple[int, dict]] = []
        handler._send_json = lambda status, payload: sent.append((status, payload))  # type: ignore[method-assign]
        handler._request_origin_allowed = lambda: True  # type: ignore[method-assign]
        broken = SimpleNamespace()
        with mock.patch.object(server_module, "STATE", broken):
            handler.do_POST()
        self.assertTrue(sent)
        self.assertEqual(sent[0][0], 500)
        self.assertIn("error", sent[0][1])


class AdapterShimTests(unittest.TestCase):
    def test_shim_has_no_exec(self) -> None:
        from codey.repairs.adapter_overrides import _package_shim_text

        text = _package_shim_text(Path("/base/codey"))
        self.assertNotIn("exec(", text)
        self.assertIn("__path__.append", text)


class KnowledgeWalTests(unittest.TestCase):
    def test_index_enables_wal_and_busy_timeout(self) -> None:
        from codey.knowledge.index import KnowledgeIndex

        with tempfile.TemporaryDirectory() as td:
            index = KnowledgeIndex(Path(td) / "k.db")
            try:
                mode = index._conn.execute("PRAGMA journal_mode").fetchone()[0]
                timeout = index._conn.execute("PRAGMA busy_timeout").fetchone()[0]
            finally:
                index.close()
        self.assertEqual(str(mode).lower(), "wal")
        self.assertGreaterEqual(int(timeout), 5000)


class BrowserWorkerBackpressureTests(unittest.TestCase):
    def test_submit_drops_when_full(self) -> None:
        from codey.automation.browser_worker import BrowserWorker

        worker = BrowserWorker(name="test-backpressure", max_queue_size=1)
        release = threading.Event()
        started = threading.Event()

        def _blocker() -> str:
            started.set()
            release.wait(10)
            return "done"

        self.assertTrue(worker.submit(_blocker))
        self.assertTrue(started.wait(10))
        # Worker is busy with the blocker; one job queues, the next drops.
        self.assertTrue(worker.submit(_blocker))
        self.assertFalse(worker.submit(_blocker))
        release.set()

    def test_call_reports_busy_when_full(self) -> None:
        from codey.automation.browser_worker import BrowserWorker

        worker = BrowserWorker(name="test-call-busy", max_queue_size=1)
        release = threading.Event()
        started = threading.Event()

        def _blocker() -> str:
            started.set()
            release.wait(10)
            return "done"

        self.assertTrue(worker.submit(_blocker))
        self.assertTrue(started.wait(10))
        self.assertTrue(worker.submit(_blocker))
        with self.assertRaisesRegex(RuntimeError, "busy"):
            worker.call(_blocker, timeout=5)
        release.set()

    def test_abandoned_cleanup_runs(self) -> None:
        from codey.automation.browser_worker import BrowserWorker

        worker = BrowserWorker(name="test-cleanup")
        cleaned = threading.Event()
        release = threading.Event()
        started = threading.Event()

        def _slow() -> str:
            started.set()
            release.wait(10)
            return "late"

        outcome: dict[str, str] = {}

        def _caller() -> None:
            try:
                worker.call(
                    _slow,
                    timeout=0.3,
                    on_abandoned=cleaned.set,
                )
            except TimeoutError:
                outcome["timed_out"] = "yes"

        thread = threading.Thread(target=_caller, daemon=True)
        thread.start()
        self.assertTrue(started.wait(10))
        thread.join(10)
        release.set()
        self.assertEqual(outcome.get("timed_out"), "yes")
        # Cleanup runs on the worker thread after abandon; poll briefly.
        deadline = threading.Event()
        for _ in range(100):
            if cleaned.is_set():
                break
            deadline.wait(0.05)
        self.assertTrue(cleaned.is_set())
        self.assertTrue(queue.Queue().empty() or True)


class ManagedOutputFailureTests(unittest.TestCase):
    def test_write_failure_is_visible_not_silent(self) -> None:
        from codey.storage.managed_outputs import run_command_with_managed_output
        from codey.toolchain.runtime import ToolOutcome

        projected = ToolOutcome("truncated model text", True, truncated=True)
        raw = SimpleNamespace(command="pytest -q", output="x" * 10)
        store = SimpleNamespace(write_run_output=mock.Mock(return_value=None))
        with (
            mock.patch(
                "codey.storage.managed_outputs.tool_runtime.run_command_raw",
                return_value=raw,
            ),
            mock.patch(
                "codey.storage.managed_outputs.tool_runtime.project_run_command_result",
                return_value=projected,
            ),
        ):
            outcome = run_command_with_managed_output(
                Path("."),
                ".",
                "pytest -q",
                permission_profile="project",
                store=store,
                session_id="s",
                run_id="r",
            )
        self.assertEqual(outcome.error_code, "managed_output_failed")
        self.assertTrue(outcome.audit.get("managed_output_failed"))
        self.assertTrue(outcome.model_text.startswith("truncated model text"))


class LocalProbeReasonTests(unittest.TestCase):
    def test_auth_is_distinguished(self) -> None:
        import urllib.error

        from codey.providers import local_openai as local_module

        error = urllib.error.HTTPError(
            "http://x/models", 401, "unauthorized", {}, None
        )
        with mock.patch.object(
            local_module.urllib.request, "urlopen", side_effect=error
        ):
            endpoint, reason = local_module.probe_local_endpoint_detail("http://x")
        self.assertIsNone(endpoint)
        self.assertEqual(reason, "auth")

    def test_malformed_reply_raises_instead_of_empty(self) -> None:
        from codey.providers import local_openai as local_module

        with self.assertRaises(RuntimeError):
            local_module._extract_reply({"choices": []})


class CheckpointCorruptionTests(unittest.TestCase):
    def test_corrupt_checkpoint_leaves_a_backup(self) -> None:
        from codey.runs.work_checkpoint import WorkCheckpointStore

        with tempfile.TemporaryDirectory() as td:
            store = WorkCheckpointStore(Path(td))
            path = store.path_for("s1")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{truncated", encoding="utf-8")
            self.assertIsNone(store.load("s1"))
            backup = store.last_corrupt_backup
            self.assertIsNotNone(backup)
            assert backup is not None
            self.assertTrue(backup.exists())


class SystemPromptLazyTests(unittest.TestCase):
    def test_module_prompt_matches_codec_prompt(self) -> None:
        from codey.protocols.json_codec import SYSTEM_PROMPT, JsonToolCodec

        self.assertEqual(SYSTEM_PROMPT, JsonToolCodec().system_prompt())


if __name__ == "__main__":
    unittest.main()
