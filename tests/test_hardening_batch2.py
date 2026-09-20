"""Regression tests for the P0/P1 hardening batch.

Covers: approval fail-closed on epoch read failure, cancellation
propagation through silent fallbacks, POST non-string coercion, adapter
shim without exec, knowledge WAL mode, browser worker backpressure,
managed-output failure visibility, local probe reasons, checkpoint
corruption backup visibility, strict reply extraction, and POST
route-level 500 containment.
"""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from codey.app import api as app_api
from codey.app import shell_service
from codey.runtime.core import cancellation


class ApprovalFailClosedTests(unittest.TestCase):
    def test_generation_read_error_is_not_current(self) -> None:
        ctx = SimpleNamespace()
        ctx.approval_generation = mock.Mock(side_effect=OSError("disk hiccup"))
        self.assertFalse(shell_service._approval_generation_current(ctx, 0))

    def test_generation_error_never_executes_shell(self) -> None:
        ctx = SimpleNamespace()
        ctx.approval_generation = mock.Mock(side_effect=OSError("disk hiccup"))
        ctx.run_registry = SimpleNamespace(stop_flag=threading.Event())
        ctx.lock = threading.Lock()
        with mock.patch(
            "codey.runtime.core.cancellation.start_process",
            side_effect=AssertionError("Popen must not start"),
        ):
            result = shell_service.execute_approved_shell(
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
            shell_service._approval_generation_current(ctx, 0)

    def test_claim_expiry_read_error_fails_closed(self) -> None:
        ctx = SimpleNamespace()
        ctx.run_registry = SimpleNamespace(stop_flag=threading.Event())
        ctx.approval_generation = mock.Mock(side_effect=OSError("boom"))
        self.assertFalse(shell_service._approval_generation_current(ctx, 0))


class CancellationPropagationTests(unittest.TestCase):
    def test_affinity_list_nodes_reraises_cancel(self) -> None:
        from codey.ghost.affinity import GhostAffinityStore

        with tempfile.TemporaryDirectory() as td:
            store = GhostAffinityStore(Path(td))
            with mock.patch.object(
                store,
                "_load_state_for_read_unlocked",
                side_effect=cancellation.TaskCancelled("stop"),
            ), self.assertRaises(cancellation.TaskCancelled):
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
            ), self.assertRaises(cancellation.TaskCancelled):
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
        from codey.automation.browser_worker import BrowserWorker, BrowserWorkerBusy

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
        with self.assertRaises(BrowserWorkerBusy):
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
        for _ in range(100):
            if cleaned.is_set():
                break
            threading.Event().wait(0.05)
        self.assertTrue(cleaned.is_set())
        health = worker.health_snapshot().to_payload()
        self.assertGreaterEqual(health["cancelled_jobs"], 1)


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

    def test_non_json_models_is_not_an_endpoint(self) -> None:
        from codey.providers import local_openai as local_module

        response = mock.Mock()
        response.read.return_value = b"not json"
        response.__enter__ = mock.Mock(return_value=response)
        response.__exit__ = mock.Mock(return_value=False)
        with mock.patch.object(
            local_module.urllib.request, "urlopen", return_value=response
        ):
            endpoint, reason = local_module.probe_local_endpoint_detail("http://x")
            thin = local_module.probe_local_endpoint("http://x")
        self.assertIsNone(endpoint)
        self.assertEqual(reason, "invalid_json")
        self.assertIsNone(thin)

    def test_non_openai_payload_is_not_an_endpoint(self) -> None:
        import json as json_module

        from codey.providers import local_openai as local_module

        response = mock.Mock()
        response.read.return_value = json_module.dumps({}).encode("utf-8")
        response.__enter__ = mock.Mock(return_value=response)
        response.__exit__ = mock.Mock(return_value=False)
        with mock.patch.object(
            local_module.urllib.request, "urlopen", return_value=response
        ):
            endpoint, reason = local_module.probe_local_endpoint_detail("http://x")
            thin = local_module.probe_local_endpoint("http://x")
        self.assertIsNone(endpoint)
        self.assertEqual(reason, "invalid_json")
        self.assertIsNone(thin)

    def test_save_rejects_invalid_models_payload(self) -> None:
        with (
            mock.patch.object(
                app_api,
                "probe_local_endpoint_detail",
                return_value=(None, "invalid_json"),
            ),
            mock.patch.object(
                app_api, "save_local_config", side_effect=AssertionError("must not save")
            ),
        ):
            status, payload = app_api.save_local_provider_response({
                "base_url": "http://x/v1",
                "model": "m",
                "api_key": "k",
            })
        self.assertEqual(status, 400)
        self.assertEqual(payload.get("reason"), "invalid_json")

    def test_save_probes_models_exactly_once(self) -> None:
        import json as json_module

        from codey.providers import local_openai as local_module

        response = mock.Mock()
        response.read.return_value = json_module.dumps(
            {"data": [{"id": "m"}]}
        ).encode("utf-8")
        response.__enter__ = mock.Mock(return_value=response)
        response.__exit__ = mock.Mock(return_value=False)
        with (
            mock.patch.object(
                local_module.urllib.request, "urlopen", return_value=response
            ) as opened,
            mock.patch.object(app_api, "load_local_config", return_value={}),
            mock.patch.object(app_api, "save_local_config") as saved,
            mock.patch.object(
                app_api, "local_config_payload", return_value={"connected": True}
            ),
        ):
            status, payload = app_api.save_local_provider_response({
                "base_url": "http://x/v1",
                "model": "m",
                "api_key": "k",
            })
        self.assertEqual(status, 200)
        self.assertTrue(payload.get("ok"))
        self.assertEqual(opened.call_count, 1)
        saved.assert_called_once()


class CheckpointCorruptionTests(unittest.TestCase):
    def test_corrupt_checkpoint_leaves_a_backup(self) -> None:
        from codey.runs.work_checkpoint import WorkCheckpointStore

        with tempfile.TemporaryDirectory() as td:
            store = WorkCheckpointStore(Path(td))
            path = store.path_for("s1")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{truncated", encoding="utf-8")
            result = store.load_result("s1")
            self.assertIsNone(result.checkpoint)
            self.assertIsNotNone(result.corrupt_backup_path)
            assert result.corrupt_backup_path is not None
            self.assertTrue(result.corrupt_backup_path.exists())
            # Plain load keeps its historical shape.
            self.assertIsNone(store.load("s1"))

    def test_builder_surfaces_corrupt_backup_in_prompt(self) -> None:
        from codey.operations.task_context import ProjectTaskContextBuilder
        from codey.runs.work_checkpoint import WorkCheckpointStore

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            project = root / "project"
            project.mkdir()
            store = WorkCheckpointStore(root / "state")
            path = store.path_for("s1")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{truncated", encoding="utf-8")
            context = ProjectTaskContextBuilder(
                work_checkpoints=store,
            ).build(
                project=project,
                task="do it",
                session_id="s1",
                run_id="run-1",
                continue_task=False,
                provider_session_changed=False,
            )
        self.assertTrue(context.checkpoint.corrupt_backup_path)
        self.assertIn("corrupt", context.checkpoint.prompt)
        self.assertIn("backed up", context.checkpoint.prompt)

    def test_notice_survives_fresh_start_failure(self) -> None:
        from codey.operations.task_context import ProjectTaskContextBuilder
        from codey.runs.work_checkpoint import WorkCheckpointLoadResult

        class _CorruptThenUnwritable:
            def load_result(self, _session_id):
                return WorkCheckpointLoadResult(
                    checkpoint=None,
                    corrupt_backup_path=Path("/tmp/state/abc.corrupt"),
                )

            def start(self, **_kwargs):
                raise OSError("cannot write")

        with tempfile.TemporaryDirectory() as td:
            context = ProjectTaskContextBuilder(
                work_checkpoints=_CorruptThenUnwritable(),
            ).build(
                project=Path(td),
                task="do it",
                session_id="s1",
                run_id="run-1",
                continue_task=False,
                provider_session_changed=False,
            )
        self.assertIsNone(context.checkpoint.item)
        self.assertTrue(context.checkpoint.corrupt_backup_path)
        self.assertIn("corrupt", context.checkpoint.prompt)


class WorkerBusyMappingTests(unittest.TestCase):
    def test_run_submit_maps_busy_to_503(self) -> None:
        from codey.automation.browser_worker import BrowserWorkerBusy

        def _busy(*_args, **_kwargs):
            raise BrowserWorkerBusy("browser worker busy: queue full")

        status, payload = app_api.run_submit_response(
            {
                "session_id": "s",
                "project": str(Path(".").resolve()),
                "task": "do it",
                "provider": "qwen",
            },
            submit_task=_busy,
        )
        self.assertEqual(status, 503)
        self.assertIn("busy", str(payload.get("error")))
        self.assertTrue(payload.get("hint"))

    def test_run_submit_keeps_500_for_server_faults(self) -> None:
        def _broken(*_args, **_kwargs):
            raise RuntimeError("disk exploded")

        status, payload = app_api.run_submit_response(
            {
                "session_id": "s",
                "project": str(Path(".").resolve()),
                "task": "do it",
                "provider": "qwen",
            },
            submit_task=_broken,
        )
        self.assertEqual(status, 500)

    def test_submit_task_raises_typed_busy_on_full_queue(self) -> None:
        from codey.app import server as server_module
        from codey.app import task_submit as task_submit_module
        from codey.automation.browser_worker import BrowserWorkerBusy

        reserved = SimpleNamespace(run_id="run-1")
        state = SimpleNamespace(
            reserve_run=mock.Mock(return_value=reserved),
            release_run=mock.Mock(),
            expire_stale_shell_approvals=mock.Mock(),
        )
        with (
            mock.patch.object(server_module, "STATE", state),
            mock.patch.object(
                task_submit_module, "submit_browser_task", return_value=False
            ),self.assertRaises(BrowserWorkerBusy)
        ):
            server_module._submit_task("s", None, "do it", 3, False, "qwen", "auto")
        state.release_run.assert_called_once_with("run-1")


class BrowserFetchAbandonTests(unittest.TestCase):
    def test_abandoned_fetch_closes_page_via_production_wrapper(self) -> None:
        from codey.automation.browser_worker import BrowserWorker
        from codey.research import browser_search as browser_search_module
        from codey.research.browser_search import BrowserSearchProvider

        provider = BrowserSearchProvider()
        worker = BrowserWorker(name="test-fetch-abandon")
        fake_page = mock.Mock()
        release = threading.Event()

        def _slow_fetch(url):
            provider._fetch_page = fake_page
            self.assertTrue(release.wait(10))
            return {"url": url, "title": "", "text": "late", "truncated": False}

        with (
            mock.patch.object(
                provider, "_fetch_on_browser_thread", side_effect=_slow_fetch
            ),
            mock.patch.object(
                browser_search_module,
                "_search_browser_worker",
                return_value=worker,
            ),
            mock.patch.object(
                browser_search_module, "_FETCH_TOTAL_TIMEOUT_SECONDS", 0.2
            ),
        ):
            result = provider.fetch("https://example.com/report")
        self.assertIn("ERROR", str(result.get("text")))
        release.set()
        for _ in range(100):
            if fake_page.close.called:
                break
            threading.Event().wait(0.05)
        fake_page.close.assert_called()


class SystemPromptLazyTests(unittest.TestCase):
    def test_module_prompt_matches_codec_prompt(self) -> None:
        from codey.protocols.json_codec import SYSTEM_PROMPT, JsonToolCodec

        self.assertEqual(SYSTEM_PROMPT, JsonToolCodec().system_prompt())


if __name__ == "__main__":
    unittest.main()
