from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess
from collections import deque
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from codey import adapter_overrides
from codey.adapter_repair import AdapterRepairResult, _render_repair_prompt, run_adapter_repair, run_worker_canary
from codey.agent import RunResult
from codey.provider_worker import WorkerChatProvider, _failure_from_response
from codey import provider_worker_child
from codey.provider_diagnostics import (
    FAILURE_AUTHENTICATION_REQUIRED,
    FAILURE_CONTROL_MISSING,
    FAILURE_READINESS_STALE,
    FAILURE_RESPONSE_MISSING,
    ProviderActionError,
    ProviderFailure,
)
from codey.provider_supervisor import ProviderHealth, STATE_DEGRADED, STATE_OPEN
from codey.repair_policy import validate_candidate
from codey.repair_sandbox import create_repair_sandbox
from codey.self_repair import SelfRepairJob, SelfRepairSupervisor
from codey.self_repair_worker import _run_worker_job, run_self_repair_worker


def _source_tree(root: Path) -> None:
    (root / "codey" / "providers").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "codey" / "__init__.py").write_text("", encoding="utf-8")
    (root / "codey" / "qwen.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "codey" / "providers" / "__init__.py").write_text("", encoding="utf-8")
    (root / "codey" / "providers" / "qwen_web.py").write_text(
        "class QwenWebProvider:\n    pass\n",
        encoding="utf-8",
    )
    (root / "tests" / "test_qwen.py").write_text("def test_qwen():\n    pass\n", encoding="utf-8")


def _failure(kind: str, *, facts: dict[str, object] | None = None) -> ProviderFailure:
    return ProviderFailure("Qwen", "send", "", "", "broken", "now", kind, facts=facts or {})


class AdapterOverridesTests(unittest.TestCase):
    def test_candidate_promotes_to_active_and_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "src"
            _source_tree(root)
            state = Path(td) / "state"

            first = adapter_overrides.install_candidate("qwen", root, state_home=state)
            adapter_overrides.mark_provisional("qwen", first.generation, state_home=state)
            adapter_overrides.record_success(
                "qwen",
                first.generation,
                state_home=state,
                current_root=root,
            )
            active = adapter_overrides.load_enabled_override("qwen", state_home=state, current_root=root)
            self.assertIsNotNone(active)
            self.assertEqual(active.status, adapter_overrides.STATUS_ACTIVE)

            second_root = Path(td) / "src2"
            _source_tree(second_root)
            (second_root / "codey" / "qwen.py").write_text("VALUE = 2\n", encoding="utf-8")
            second = adapter_overrides.install_candidate(
                "qwen",
                second_root,
                state_home=state,
                base_hash=adapter_overrides.adapter_base_hash("qwen", root),
            )
            adapter_overrides.mark_provisional("qwen", second.generation, state_home=state)
            enabled = adapter_overrides.load_enabled_override("qwen", state_home=state, current_root=root)
            self.assertEqual(enabled.generation, second.generation)

            adapter_overrides.record_failure(
                "qwen",
                second.generation,
                FAILURE_RESPONSE_MISSING,
                state_home=state,
            )
            enabled = adapter_overrides.load_enabled_override("qwen", state_home=state, current_root=root)
            self.assertEqual(enabled.generation, second.generation)
            adapter_overrides.record_failure(
                "qwen",
                second.generation,
                FAILURE_CONTROL_MISSING,
                state_home=state,
            )
            restored = adapter_overrides.load_enabled_override("qwen", state_home=state, current_root=root)
            self.assertEqual(restored.generation, first.generation)

    def test_non_structural_failure_does_not_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "src"
            _source_tree(root)
            state = Path(td) / "state"
            override = adapter_overrides.install_candidate("qwen", root, state_home=state)
            adapter_overrides.mark_provisional("qwen", override.generation, state_home=state)

            adapter_overrides.record_failure(
                "qwen",
                override.generation,
                FAILURE_AUTHENTICATION_REQUIRED,
                state_home=state,
            )

            enabled = adapter_overrides.load_enabled_override("qwen", state_home=state, current_root=root)
            self.assertEqual(enabled.generation, override.generation)

    def test_readiness_stale_failure_rolls_back_provisional_override(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "src"
            _source_tree(root)
            state = Path(td) / "state"
            override = adapter_overrides.install_candidate("qwen", root, state_home=state)
            adapter_overrides.mark_provisional("qwen", override.generation, state_home=state)

            adapter_overrides.record_failure(
                "qwen",
                override.generation,
                FAILURE_READINESS_STALE,
                state_home=state,
            )
            enabled = adapter_overrides.load_enabled_override("qwen", state_home=state, current_root=root)
            self.assertEqual(enabled.generation, override.generation)

            adapter_overrides.record_failure(
                "qwen",
                override.generation,
                FAILURE_READINESS_STALE,
                state_home=state,
            )

            self.assertIsNone(adapter_overrides.load_enabled_override("qwen", state_home=state))

    def test_rollback_does_not_restore_missing_previous_generation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "src"
            _source_tree(root)
            state = Path(td) / "state"

            first = adapter_overrides.install_candidate("qwen", root, state_home=state)
            adapter_overrides.mark_provisional("qwen", first.generation, state_home=state)
            adapter_overrides.record_success(
                "qwen",
                first.generation,
                state_home=state,
                current_root=root,
            )
            self.assertTrue(first.root.exists())
            shutil.rmtree(first.root)
            (root / "codey" / "qwen.py").write_text("VALUE = 2\n", encoding="utf-8")
            second = adapter_overrides.install_candidate("qwen", root, state_home=state)
            adapter_overrides.mark_provisional("qwen", second.generation, state_home=state)

            adapter_overrides.record_failure(
                "qwen",
                second.generation,
                FAILURE_RESPONSE_MISSING,
                state_home=state,
            )
            adapter_overrides.record_failure(
                "qwen",
                second.generation,
                FAILURE_CONTROL_MISSING,
                state_home=state,
            )

            self.assertIsNone(adapter_overrides.load_enabled_override("qwen", state_home=state))

    def test_base_hash_mismatch_disables_override(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "src"
            _source_tree(root)
            state = Path(td) / "state"
            override = adapter_overrides.install_candidate("qwen", root, state_home=state)
            adapter_overrides.mark_provisional("qwen", override.generation, state_home=state)
            self.assertIsNotNone(
                adapter_overrides.load_enabled_override("qwen", state_home=state, current_root=root)
            )

            (root / "codey" / "qwen.py").write_text("VALUE = 999\n", encoding="utf-8")

            self.assertIsNone(
                adapter_overrides.load_enabled_override("qwen", state_home=state, current_root=root)
            )

    def test_candidate_does_not_disable_existing_active_until_provisional(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "src"
            _source_tree(root)
            state = Path(td) / "state"
            first = adapter_overrides.install_candidate("qwen", root, state_home=state)
            adapter_overrides.mark_provisional("qwen", first.generation, state_home=state)
            adapter_overrides.record_success(
                "qwen",
                first.generation,
                state_home=state,
                current_root=root,
            )

            second_root = Path(td) / "src2"
            _source_tree(second_root)
            (second_root / "codey" / "qwen.py").write_text("VALUE = 2\n", encoding="utf-8")
            second = adapter_overrides.install_candidate(
                "qwen",
                second_root,
                state_home=state,
                base_hash=adapter_overrides.adapter_base_hash("qwen", root),
            )

            enabled = adapter_overrides.load_enabled_override("qwen", state_home=state, current_root=root)
            self.assertEqual(enabled.generation, first.generation)

            adapter_overrides.mark_provisional("qwen", second.generation, state_home=state)
            enabled = adapter_overrides.load_enabled_override("qwen", state_home=state, current_root=root)
            self.assertEqual(enabled.generation, second.generation)


class RepairPolicyTests(unittest.TestCase):
    def test_policy_allows_only_adapter_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / "base"
            candidate = Path(td) / "candidate"
            _source_tree(base)
            _source_tree(candidate)
            (candidate / "codey" / "qwen.py").write_text("VALUE = 3\n", encoding="utf-8")

            result = validate_candidate("qwen", base, candidate)

            self.assertTrue(result.ok)
            self.assertIn("codey/qwen.py", result.changed_files)

    def test_policy_rejects_generated_tests(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / "base"
            candidate = Path(td) / "candidate"
            _source_tree(base)
            _source_tree(candidate)
            (candidate / "tests" / "generated").mkdir()
            (candidate / "tests" / "generated" / "test_adapter_repair_qwen.py").write_text(
                "def test_generated():\n    pass\n",
                encoding="utf-8",
            )

            result = validate_candidate("qwen", base, candidate)

            self.assertFalse(result.ok)
            self.assertTrue(any("file is not allowed" in item for item in result.errors))

    def test_policy_rejects_test_changes_and_core_changes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / "base"
            candidate = Path(td) / "candidate"
            _source_tree(base)
            _source_tree(candidate)
            (candidate / "tests" / "test_qwen.py").write_text("def test_qwen():\n    assert True\n", encoding="utf-8")
            (candidate / "codey" / "agent.py").write_text("VALUE = 1\n", encoding="utf-8")

            result = validate_candidate("qwen", base, candidate)

            self.assertFalse(result.ok)
            self.assertTrue(any("test file is read-only" in item for item in result.errors))
            self.assertTrue(any("file is not allowed" in item for item in result.errors))

    def test_policy_rejects_dangerous_adapter_code(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / "base"
            candidate = Path(td) / "candidate"
            _source_tree(base)
            _source_tree(candidate)
            (candidate / "codey" / "qwen.py").write_text(
                "def bad():\n    eval('1')\n",
                encoding="utf-8",
            )

            result = validate_candidate("qwen", base, candidate)

            self.assertFalse(result.ok)
            self.assertTrue(any("forbidden snippet" in item for item in result.errors))

    def test_policy_allows_existing_forbidden_snippets_but_rejects_new_ones(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / "base"
            candidate = Path(td) / "candidate"
            _source_tree(base)
            _source_tree(candidate)
            (base / "codey" / "glm.py").write_text(
                "def ok(source):\n    return compile(source, '<x>', 'exec')\n",
                encoding="utf-8",
            )
            (candidate / "codey" / "glm.py").write_text(
                "def ok(source):\n    return compile(source, '<x>', 'exec')\n# repaired\n",
                encoding="utf-8",
            )

            result = validate_candidate("glm", base, candidate)

            self.assertTrue(result.ok)

            (candidate / "codey" / "glm.py").write_text(
                "def ok(source):\n    return compile(source, '<x>', 'exec')\n"
                "def bad(source):\n    return compile(source, '<y>', 'exec')\n",
                encoding="utf-8",
            )

            result = validate_candidate("glm", base, candidate)

            self.assertFalse(result.ok)
            self.assertTrue(any("compile(" in item for item in result.errors))


class RepairSandboxTests(unittest.TestCase):
    def test_sandbox_copies_source_without_runtime_junk(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "src"
            _source_tree(root)
            (root / ".git").mkdir()
            (root / ".git" / "HEAD").write_text("ref", encoding="utf-8")
            (root / "codey" / "__pycache__").mkdir()
            (root / "codey" / "__pycache__" / "x.pyc").write_bytes(b"x")

            sandbox = create_repair_sandbox(root)
            try:
                self.assertTrue((sandbox.baseline_root / "codey" / "qwen.py").is_file())
                self.assertFalse((sandbox.baseline_root / ".git").exists())
                self.assertFalse((sandbox.candidate_root / "codey" / "__pycache__").exists())
            finally:
                sandbox.cleanup()
                self.assertFalse(sandbox.temp_root.exists())


class SelfRepairSupervisorTests(unittest.TestCase):
    def test_only_open_structural_failures_enqueue(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            supervisor = SelfRepairSupervisor(td, clock=lambda: 100.0)
            open_health = ProviderHealth(state=STATE_OPEN, last_failure_kind=FAILURE_RESPONSE_MISSING)

            self.assertTrue(supervisor.maybe_enqueue("qwen", _failure(FAILURE_RESPONSE_MISSING), open_health))
            self.assertFalse(supervisor.maybe_enqueue("qwen", _failure(FAILURE_RESPONSE_MISSING), open_health))
            self.assertEqual(len(supervisor.pending()), 1)

            degraded = ProviderHealth(state=STATE_DEGRADED, last_failure_kind=FAILURE_RESPONSE_MISSING)
            self.assertFalse(supervisor.maybe_enqueue("stepfun", _failure(FAILURE_RESPONSE_MISSING), degraded))
            self.assertFalse(supervisor.maybe_enqueue("glm", _failure(FAILURE_AUTHENTICATION_REQUIRED), open_health))

    def test_readiness_stale_enqueue_carries_sanitized_facts_without_keying_on_facts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            supervisor = SelfRepairSupervisor(td, clock=lambda: 100.0)
            open_health = ProviderHealth(state=STATE_OPEN, last_failure_kind=FAILURE_READINESS_STALE)

            first = _failure(
                FAILURE_READINESS_STALE,
                facts={
                    "composer_visible": True,
                    "waited_for": "/api/v2/models/",
                    "prompt": "secret",
                },
            )
            second = _failure(
                FAILURE_READINESS_STALE,
                facts={"composer_visible": False, "waited_for": "/other"},
            )

            self.assertTrue(supervisor.maybe_enqueue("qwen", first, open_health))
            self.assertFalse(supervisor.maybe_enqueue("qwen", second, open_health))
            job = supervisor.pending()[0]

            self.assertEqual(job.failure_kind, FAILURE_READINESS_STALE)
            self.assertEqual(job.failure_facts, {
                "composer_visible": True,
                "waited_for": "/api/v2/models/",
            })

    def test_run_pending_uses_runner(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            results = []

            def runner(job):
                results.append(job.provider_id)
                return mock.Mock(ok=True, provider_id=job.provider_id, generation=3, error="")

            supervisor = SelfRepairSupervisor(td, runner=runner, clock=lambda: 100.0)
            supervisor.maybe_enqueue(
                "qwen",
                _failure(FAILURE_CONTROL_MISSING),
                ProviderHealth(state=STATE_OPEN, last_failure_kind=FAILURE_CONTROL_MISSING),
            )

            output = supervisor.run_pending_once()

            self.assertEqual(results, ["qwen"])
            self.assertEqual(output[0].generation, 3)
            self.assertEqual(supervisor.pending(), ())

    def test_failed_repair_remains_queued_until_retry_time(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            now = 100.0
            attempts = []

            def clock():
                return now

            def runner(job):
                attempts.append(job.provider_id)
                return mock.Mock(ok=False, provider_id=job.provider_id, generation=0, error="bad patch")

            supervisor = SelfRepairSupervisor(td, runner=runner, clock=clock)
            supervisor.maybe_enqueue(
                "qwen",
                _failure(FAILURE_CONTROL_MISSING),
                ProviderHealth(state=STATE_OPEN, last_failure_kind=FAILURE_CONTROL_MISSING),
            )

            first = supervisor.run_pending_once()
            self.assertFalse(supervisor.has_due_work())
            second = supervisor.run_pending_once()
            now += 15 * 60 + 1
            self.assertTrue(supervisor.has_due_work())
            third = supervisor.run_pending_once()

            self.assertEqual(len(first), 1)
            self.assertEqual(second, ())
            self.assertEqual(len(third), 1)
            self.assertEqual(attempts, ["qwen", "qwen"])
            self.assertEqual(len(supervisor.pending()), 1)

    def test_runner_exception_remains_queued_until_retry_time(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            now = 100.0
            attempts = []

            def clock():
                return now

            def runner(job):
                attempts.append(job.provider_id)
                raise RuntimeError("helper unavailable")

            supervisor = SelfRepairSupervisor(td, runner=runner, clock=clock)
            supervisor.maybe_enqueue(
                "qwen",
                _failure(FAILURE_RESPONSE_MISSING),
                ProviderHealth(state=STATE_OPEN, last_failure_kind=FAILURE_RESPONSE_MISSING),
            )

            self.assertEqual(supervisor.run_pending_once(), ())
            self.assertEqual(supervisor.run_pending_once(), ())
            now += 15 * 60 + 1
            self.assertEqual(supervisor.run_pending_once(), ())

            self.assertEqual(attempts, ["qwen", "qwen"])
            self.assertEqual(len(supervisor.pending()), 1)


class ProviderRegistryOverrideTests(unittest.TestCase):
    def test_registry_returns_worker_for_enabled_override(self) -> None:
        from codey.providers import registry

        fake_override = mock.Mock()
        with (
            mock.patch.object(registry, "load_enabled_override", return_value=fake_override),
            mock.patch.object(registry, "WorkerChatProvider", return_value="worker") as worker,
        ):
            provider = registry.connect_provider("qwen", port=9100)

        self.assertEqual(provider, "worker")
        self.assertEqual(worker.call_args.args[0], "qwen")
        self.assertEqual(worker.call_args.args[1], fake_override)

    def test_registry_worker_child_env_uses_builtin_provider(self) -> None:
        from codey.providers import registry

        provider_type = mock.Mock()
        provider_type.connect.return_value = "builtin"
        with (
            mock.patch.dict("os.environ", {"CODEY_PROVIDER_WORKER_CHILD": "1"}),
            mock.patch.object(registry, "PROVIDER_TYPES", {"qwen": provider_type}),
            mock.patch.object(registry, "load_enabled_override") as load_override,
        ):
            provider = registry.connect_provider("qwen")

        self.assertEqual(provider, "builtin")
        load_override.assert_not_called()

    def test_registry_skips_override_when_connecting_existing_provider(self) -> None:
        from codey.providers import registry

        provider_type = mock.Mock()
        provider_type.connect.return_value = "builtin"
        with (
            mock.patch.object(registry, "PROVIDER_TYPES", {"qwen": provider_type}),
            mock.patch.object(registry, "load_enabled_override") as load_override,
            mock.patch.object(registry, "WorkerChatProvider") as worker,
        ):
            provider = registry.connect_existing_provider("qwen")

        self.assertEqual(provider, "builtin")
        load_override.assert_not_called()
        worker.assert_not_called()


class TaskRunnerSelfRepairIntegrationTests(unittest.TestCase):
    def test_structural_writer_failure_is_offered_to_self_repair_without_blocking_failover(self) -> None:
        from codey import server

        with tempfile.TemporaryDirectory() as td:
            state = server.State(Path(td) / "state")
            state.provider_failover_order = lambda: ("deepseek", "stepfun")
            state.provider_supervisor.record_failure(
                "deepseek",
                ProviderFailure(
                    "DeepSeek",
                    "send",
                    "",
                    "",
                    "missing input",
                    "now",
                    FAILURE_CONTROL_MISSING,
                ),
            )
            state.self_repair = mock.Mock()
            writer = mock.Mock()
            writer.name = "DeepSeek Web"
            writer.location = "https://chat.deepseek.com/"
            writer.send.side_effect = lambda prompt, timeout=None: str(prompt).rsplit(" ", 1)[-1]
            sibling = mock.Mock()
            sibling.name = "StepFun Chat"
            sibling.location = "https://chat.stepfun.com/chats/"
            failure = ProviderActionError(ProviderFailure(
                "DeepSeek",
                "send",
                "",
                "",
                "response missing",
                "now",
                FAILURE_RESPONSE_MISSING,
            ))
            changes = {
                "ok": True,
                "mode": "snapshot",
                "changed_count": 0,
                "files": [],
                "diff": "",
            }

            with (
                mock.patch.object(server, "STATE", state),
                mock.patch.object(state, "get_provider", side_effect=[writer, sibling]),
                mock.patch.object(
                    server,
                    "agent_run",
                    side_effect=[failure, RunResult("done", "done", 1, False, False)],
                ),
                mock.patch.object(server, "collect_changes", return_value=changes),
                mock.patch.object(server, "_run_project_audit", return_value=()),
            ):
                server._run_task("session-self-repair", td, "task", 8, False, "deepseek")

            state.self_repair.maybe_enqueue.assert_called_once()
            args = state.self_repair.maybe_enqueue.call_args.args
            self.assertEqual(args[0], "deepseek")
            self.assertEqual(args[1].kind, FAILURE_RESPONSE_MISSING)
            self.assertEqual(args[2].state, STATE_DEGRADED)
            self.assertEqual(state.last_terminal_event["provider"], "stepfun")

    def test_state_kicks_self_repair_queue_only_when_idle(self) -> None:
        from codey import server

        with tempfile.TemporaryDirectory() as td:
            ran = threading.Event()

            def runner(job):
                ran.set()
                return mock.Mock(ok=True, provider_id=job.provider_id, generation=1, error="")

            state = server.State(Path(td) / "state")
            state.self_repair = SelfRepairSupervisor(td, runner=runner, clock=lambda: 100.0)
            state.self_repair.maybe_enqueue(
                "qwen",
                _failure(FAILURE_RESPONSE_MISSING),
                ProviderHealth(state=STATE_OPEN, last_failure_kind=FAILURE_RESPONSE_MISSING),
            )

            self.assertTrue(state.kick_self_repair())
            self.assertTrue(ran.wait(2.0))
            deadline = time.time() + 2.0
            while state._self_repair_running and time.time() < deadline:
                time.sleep(0.01)
            self.assertFalse(state._self_repair_running)
            self.assertEqual(state.self_repair.pending(), ())

    def test_state_runs_self_repair_without_browser_worker(self) -> None:
        from codey import server

        with tempfile.TemporaryDirectory() as td:
            ran = threading.Event()

            def runner(job):
                ran.set()
                return mock.Mock(ok=True, provider_id=job.provider_id, generation=1, error="")

            state = server.State(Path(td) / "state")
            state.self_repair = SelfRepairSupervisor(td, runner=runner, clock=lambda: 100.0)
            state.self_repair.maybe_enqueue(
                "qwen",
                _failure(FAILURE_RESPONSE_MISSING),
                ProviderHealth(state=STATE_OPEN, last_failure_kind=FAILURE_RESPONSE_MISSING),
            )
            with mock.patch.object(server, "submit_browser_task") as submit:
                self.assertTrue(state.kick_self_repair())
                self.assertTrue(ran.wait(2.0))
                deadline = time.time() + 2.0
                while state._self_repair_running and time.time() < deadline:
                    time.sleep(0.01)
                self.assertFalse(state._self_repair_running)

        submit.assert_not_called()

    def test_state_self_repair_job_spawns_process_worker_with_candidates(self) -> None:
        from codey import server

        with tempfile.TemporaryDirectory() as td:
            state = server.State(Path(td) / "state")
            state.provider_failover_order = lambda: ("qwen", "stepfun", "deepseek")
            with mock.patch.object(
                server,
                "run_self_repair_worker",
                return_value=AdapterRepairResult(True, "deepseek", generation=7),
            ) as worker:
                result = state._run_self_repair_job(SelfRepairJob("deepseek", FAILURE_RESPONSE_MISSING))

        self.assertTrue(result.ok)
        worker.assert_called_once()
        self.assertEqual(worker.call_args.kwargs["helper_ids"], ("qwen", "stepfun"))

    def test_state_does_not_start_self_repair_while_busy(self) -> None:
        from codey import server

        with tempfile.TemporaryDirectory() as td:
            ran = threading.Event()

            def runner(job):
                ran.set()
                return mock.Mock(ok=True, provider_id=job.provider_id, generation=1, error="")

            state = server.State(Path(td) / "state")
            state.busy = True
            state.self_repair = SelfRepairSupervisor(td, runner=runner, clock=lambda: 100.0)
            state.self_repair.maybe_enqueue(
                "qwen",
                _failure(FAILURE_RESPONSE_MISSING),
                ProviderHealth(state=STATE_OPEN, last_failure_kind=FAILURE_RESPONSE_MISSING),
            )

            self.assertFalse(state.kick_self_repair())
            self.assertFalse(ran.wait(0.1))
            self.assertEqual(len(state.self_repair.pending()), 1)

    def test_state_accepts_new_user_run_while_self_repair_is_running(self) -> None:
        from codey import server

        with tempfile.TemporaryDirectory() as td:
            state = server.State(Path(td) / "state")
            state._self_repair_running = True

            reserved = state.reserve_run(
                session_id="s",
                project=td,
                task="task",
                provider_id="qwen",
            )

            self.assertIsNotNone(reserved)
            self.assertEqual(reserved.provider_id, "qwen")


class SelfRepairWorkerTests(unittest.TestCase):
    def test_parent_runner_uses_subprocess_and_parses_result(self) -> None:
        completed = mock.Mock(
            returncode=0,
            stdout='noise\n{"ok":true,"provider_id":"qwen","generation":4,"changed_files":["codey/qwen.py"]}\n',
            stderr="",
        )
        with mock.patch("codey.self_repair_worker.cancellation.run_process", return_value=completed) as run:
            result = run_self_repair_worker(
                SelfRepairJob(
                    "qwen",
                    FAILURE_READINESS_STALE,
                    "new_chat",
                    failure_facts={"composer_visible": True},
                ),
                helper_ids=("deepseek", "stepfun"),
                state_home=Path("state"),
                source_root=Path("src"),
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.generation, 4)
        command = run.call_args.args[0]
        self.assertIn("codey.self_repair_worker", command)
        self.assertEqual(command.count("--helper"), 2)
        self.assertIn("--failure-facts-json", command)
        self.assertIn('{"composer_visible":true}', command)
        self.assertEqual(run.call_args.kwargs["timeout"], 900.0)

    def test_parent_runner_uses_process_tree_cleanup_on_timeout(self) -> None:
        with mock.patch(
            "codey.self_repair_worker.cancellation.run_process",
            side_effect=subprocess.TimeoutExpired(["worker"], 900.0),
        ):
            result = run_self_repair_worker(
                SelfRepairJob("qwen", FAILURE_RESPONSE_MISSING),
                helper_ids=("deepseek",),
                state_home=Path("state"),
                source_root=Path("src"),
            )

        self.assertFalse(result.ok)
        self.assertIn("timed out", result.error)

    def test_worker_job_uses_repair_helper_and_suppresses_hidden_assistance(self) -> None:
        helper = mock.Mock()
        helper.send.return_value = '{"files":[]}'
        with (
            mock.patch("codey.self_repair_worker.connect_repair_helper", return_value=helper) as connect_helper,
            mock.patch("codey.self_repair_worker.run_adapter_repair", return_value=AdapterRepairResult(True, "qwen")) as repair,
            mock.patch("codey.self_repair_worker.provider_controls.suppress_assistance", return_value=contextlib.nullcontext()) as controls_suppress,
            mock.patch("codey.self_repair_worker.provider_flow.suppress_assistance", return_value=contextlib.nullcontext()) as flow_suppress,
        ):
            result = _run_worker_job(
                provider_id="qwen",
                failure_kind=FAILURE_READINESS_STALE,
                failure_stage="new_chat",
                failure_facts={"composer_visible": True},
                helper_ids=("deepseek",),
                state_home=Path("state"),
                source_root=Path("src"),
                model_timeout=12.0,
            )

        self.assertTrue(result.ok)
        connect_helper.assert_called_once_with("deepseek")
        controls_suppress.assert_called_once()
        flow_suppress.assert_called_once()
        helper.new_chat.assert_called_once_with(timeout=12.0)
        repair.assert_called_once()
        self.assertEqual(repair.call_args.kwargs["failure_kind"], FAILURE_READINESS_STALE)
        self.assertEqual(repair.call_args.kwargs["failure_stage"], "new_chat")
        self.assertEqual(repair.call_args.kwargs["failure_facts"], {"composer_visible": True})

    def test_worker_job_tries_next_helper_after_invalid_repair_result(self) -> None:
        first = mock.Mock()
        second = mock.Mock()
        failed = AdapterRepairResult(False, "qwen", error="bad json")
        passed = AdapterRepairResult(True, "qwen", generation=4)
        with (
            mock.patch(
                "codey.self_repair_worker.connect_repair_helper",
                side_effect=[first, second],
            ) as connect_helper,
            mock.patch(
                "codey.self_repair_worker.run_adapter_repair",
                side_effect=[failed, passed],
            ) as repair,
            mock.patch("codey.self_repair_worker.provider_controls.suppress_assistance", return_value=contextlib.nullcontext()),
            mock.patch("codey.self_repair_worker.provider_flow.suppress_assistance", return_value=contextlib.nullcontext()),
        ):
            result = _run_worker_job(
                provider_id="qwen",
                helper_ids=("deepseek", "stepfun"),
                state_home=Path("state"),
                source_root=Path("src"),
                model_timeout=12.0,
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.generation, 4)
        self.assertEqual(connect_helper.call_args_list[0].args, ("deepseek",))
        self.assertEqual(connect_helper.call_args_list[1].args, ("stepfun",))
        self.assertEqual(repair.call_count, 2)
        first.close.assert_called_once()
        second.close.assert_called_once()

    def test_repair_helper_uses_shared_profile_fresh_background_tab(self) -> None:
        from codey import self_repair_worker

        provider_type = mock.Mock()
        with (
            mock.patch.dict(self_repair_worker.PROVIDER_TYPES, {"qwen": provider_type}),
            mock.patch.dict(self_repair_worker.PROVIDER_WORKER_PORT_OFFSETS, {"qwen": 3}),
        ):
            helper = self_repair_worker.connect_repair_helper("qwen")

        self.assertIs(helper, provider_type.connect.return_value)
        provider_type.connect.assert_called_once_with(
            port=self_repair_worker.DEFAULT_PORT + 200 + 3,
            profile=self_repair_worker.DEFAULT_PROFILE,
            open_if_missing=True,
            bring_to_front=False,
            isolated=False,
            fresh_tab=True,
        )

    def test_provider_worker_child_uses_dedicated_isolated_profile(self) -> None:
        provider_type = mock.Mock()
        provider = provider_type.connect.return_value
        provider.session.page = mock.Mock()
        provider.session.cdp_port = 9444
        cdp = provider.session.page.context.new_cdp_session.return_value
        cdp.send.return_value = {"targetInfo": {"targetId": "target-1"}}
        with (
            mock.patch.dict(provider_worker_child.PROVIDER_TYPES, {"qwen": provider_type}),
            mock.patch("sys.stdin", io.StringIO("")),
            mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            code = provider_worker_child.main([
                "--provider",
                "qwen",
                "--port",
                "9555",
                "--profile",
                "state/provider-workers/qwen/generation-3",
            ])

        self.assertEqual(code, 0)
        # The override worker never attaches to the user's default profile.
        self.assertNotEqual(
            provider_type.connect.call_args.kwargs["profile"],
            provider_worker_child.DEFAULT_PROFILE,
        )
        self.assertEqual(
            provider_type.connect.call_args.kwargs["profile"],
            Path("state/provider-workers/qwen/generation-3"),
        )
        event = json.loads(stdout.getvalue().splitlines()[0])
        self.assertEqual(event["event"], "page")
        self.assertEqual(event["port"], 9444)
        self.assertEqual(event["target_id"], "target-1")
        provider.close.assert_called_once()

    def test_provider_worker_launches_with_per_generation_profile_and_stderr_drain(self) -> None:
        override = mock.Mock()
        override.root = Path("override")
        override.generation = 7
        process = mock.Mock()
        process.stdout = iter(())
        process.stderr = iter(())
        process.stdin = mock.Mock()
        job = mock.Mock()

        with (
            mock.patch("codey.provider_worker.subprocess.Popen", return_value=process) as popen,
            mock.patch("codey.provider_worker.cancellation.attach_process_tree", return_value=job),
        ):
            WorkerChatProvider("qwen", override, state_home=Path("state"))

        cmd = popen.call_args.args[0]
        self.assertIn("--profile", cmd)
        profile_arg = Path(cmd[cmd.index("--profile") + 1])
        expected_tail = Path("state") / "provider-workers" / "qwen" / "7"
        self.assertEqual(profile_arg, expected_tail)
        self.assertIsNotNone(popen.call_args.kwargs.get("stderr"))
        self.assertNotEqual(popen.call_args.kwargs.get("stderr"), subprocess.DEVNULL)

    def test_provider_worker_stderr_tail_is_kept_bounded_and_attached(self) -> None:
        provider = WorkerChatProvider.__new__(WorkerChatProvider)
        provider._stderr_tail = deque(maxlen=24)
        provider.provider_id = "qwen"
        provider.name = "qwen worker"
        for index in range(40):
            provider._stderr_tail.append(f"line {index}")

        suffix = provider._worker_error_suffix()
        self.assertIn("line 39", suffix)
        self.assertNotIn("line 0 ", suffix)
        self.assertLessEqual(len(suffix), 420)

    def test_provider_worker_terminates_process_tree(self) -> None:
        override = mock.Mock()
        override.root = Path("override")
        override.generation = 3
        process = mock.Mock()
        process.stdout = iter(())
        process.stdin = mock.Mock()
        job = mock.Mock()

        with (
            mock.patch("codey.provider_worker.subprocess.Popen", return_value=process),
            mock.patch("codey.provider_worker.cancellation.attach_process_tree", return_value=job) as attach,
            mock.patch("codey.provider_worker.cancellation.terminate_process_tree") as terminate,
        ):
            provider = WorkerChatProvider("qwen", override, state_home=Path("state"))
            provider._terminate()

        attach.assert_called_once_with(process)
        terminate.assert_called_once_with(process, job)
        job.close.assert_called_once()

    def test_provider_worker_closes_fresh_tab_when_terminating(self) -> None:
        override = mock.Mock()
        override.root = Path("override")
        override.generation = 3
        process = mock.Mock()
        process.stdout = iter(())
        process.stdin = mock.Mock()
        job = mock.Mock()

        with (
            mock.patch("codey.provider_worker.subprocess.Popen", return_value=process),
            mock.patch("codey.provider_worker.cancellation.attach_process_tree", return_value=job),
            mock.patch("codey.provider_worker.cancellation.terminate_process_tree"),
            mock.patch("codey.provider_worker.urlopen") as urlopen,
        ):
            provider = WorkerChatProvider("qwen", override, state_home=Path("state"))
            provider._cdp_port = 9444
            provider._target_id = "target/with space"
            provider._terminate()

        urlopen.assert_called_once()
        self.assertIn(
            "http://127.0.0.1:9444/json/close/target%2Fwith%20space",
            urlopen.call_args.args[0],
        )

    def test_provider_worker_timeout_closes_fresh_tab_before_killing_child(self) -> None:
        override = mock.Mock()
        override.root = Path("override")
        override.generation = 3
        process = mock.Mock()
        process.stdout = iter(())
        process.stdin = mock.Mock()
        process.poll.return_value = None
        job = mock.Mock()

        with (
            mock.patch("codey.provider_worker.subprocess.Popen", return_value=process),
            mock.patch("codey.provider_worker.cancellation.attach_process_tree", return_value=job),
            mock.patch("codey.provider_worker.cancellation.terminate_process_tree") as terminate,
            mock.patch("codey.provider_worker.urlopen") as urlopen,
            mock.patch("codey.provider_worker.WORKER_TIMEOUT_GRACE", 0.0),
            mock.patch("codey.provider_worker.time.monotonic", side_effect=[100.0, 100.1]),
        ):
            provider = WorkerChatProvider("qwen", override, state_home=Path("state"))
            provider._cdp_port = 9444
            provider._target_id = "target-1"
            with self.assertRaises(ProviderActionError):
                provider._request("send", {}, 0.0)

        urlopen.assert_called_once()
        self.assertIn("http://127.0.0.1:9444/json/close/target-1", urlopen.call_args.args[0])
        terminate.assert_called_once_with(process, job)

    def test_provider_worker_failure_from_response_preserves_sanitized_facts(self) -> None:
        failure = _failure_from_response(
            "qwen",
            "new_chat",
            {
                "error": "failed",
                "failure": {
                    "model": "Qwen",
                    "action": "new_chat",
                    "message": "stale bootstrap",
                    "kind": FAILURE_READINESS_STALE,
                    "stage": "new_chat",
                    "facts": {
                        "composer_visible": True,
                        "waited_for": "/api/v2/models/",
                        "cookie": "secret",
                    },
                },
            },
        )

        self.assertEqual(failure.kind, FAILURE_READINESS_STALE)
        self.assertEqual(failure.facts, {
            "composer_visible": True,
            "waited_for": "/api/v2/models/",
        })

    def test_provider_worker_new_chat_failure_counts_against_override(self) -> None:
        provider = WorkerChatProvider.__new__(WorkerChatProvider)
        provider.provider_id = "qwen"
        provider.override = mock.Mock(generation=7)
        provider.state_home = Path("state")
        failure = ProviderFailure(
            "Qwen",
            "new_chat",
            "",
            "",
            "stale readiness",
            "now",
            FAILURE_READINESS_STALE,
            "new_chat",
        )

        with (
            mock.patch.object(
                WorkerChatProvider,
                "_request",
                side_effect=ProviderActionError(failure),
            ),
            mock.patch("codey.provider_worker.record_failure") as record,
        ):
            with self.assertRaises(ProviderActionError):
                WorkerChatProvider.new_chat(provider, timeout=1.0)

        record.assert_called_once_with(
            "qwen",
            7,
            FAILURE_READINESS_STALE,
            state_home=Path("state"),
        )


class AdapterRepairRunnerTests(unittest.TestCase):
    def test_repair_prompt_example_uses_target_provider_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "src"
            _source_tree(root)
            (root / "codey" / "deepseek.py").write_text("VALUE = 1\n", encoding="utf-8")
            (root / "codey" / "providers" / "deepseek_web.py").write_text("VALUE = 1\n", encoding="utf-8")
            (root / "tests" / "test_deepseek.py").write_text("def test_deepseek():\n    pass\n", encoding="utf-8")

            prompt = _render_repair_prompt("deepseek", root)

            self.assertIn('"path":"codey/deepseek.py"', prompt)
            self.assertNotIn('"path":"codey/qwen.py"', prompt)

    def test_repair_prompt_includes_bounded_readiness_failure_context(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "src"
            _source_tree(root)

            prompt = _render_repair_prompt(
                "qwen",
                root,
                failure_kind=FAILURE_READINESS_STALE,
                failure_stage="new_chat",
                failure_facts={
                    "composer_visible": True,
                    "model_selector_text_present": True,
                    "waited_for": "/api/v2/models/",
                    "reply": "secret answer",
                },
            )

            self.assertIn("Observed failure:", prompt)
            self.assertIn("kind: readiness_stale", prompt)
            self.assertIn("stage: new_chat", prompt)
            self.assertIn("- composer_visible=true", prompt)
            self.assertIn("- model_selector_text_present=true", prompt)
            self.assertIn('- waited_for="/api/v2/models/"', prompt)
            self.assertIn("prefer DOM readiness over brittle internal bootstrap resources", prompt)
            self.assertNotIn("secret answer", prompt)

    def test_adapter_repair_installs_candidate_after_policy_and_checks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "src"
            _source_tree(root)
            state = Path(td) / "state"
            replacement = "VALUE = 42\n"
            reply = {
                "files": [
                    {
                        "path": "codey/qwen.py",
                        "content": replacement,
                    }
                ]
            }

            with mock.patch(
                "codey.adapter_repair._run_static_checks",
                return_value=("passed:py_compile", "passed:ruff", "passed:provider_unittest"),
            ):
                result = run_adapter_repair(
                    "qwen",
                    send_prompt=lambda _prompt: __import__("json").dumps(reply),
                    state_home=state,
                    source_root=root,
                    run_canary=lambda _override: True,
                )

            self.assertTrue(result.ok)
            enabled = adapter_overrides.load_enabled_override("qwen", state_home=state, current_root=root)
            self.assertIsNotNone(enabled)
            self.assertEqual(enabled.status, adapter_overrides.STATUS_PROVISIONAL)
            self.assertEqual(
                (enabled.root / "codey" / "qwen.py").read_text(encoding="utf-8"),
                replacement,
            )

    def test_adapter_repair_keeps_candidate_disabled_when_canary_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "src"
            _source_tree(root)
            state = Path(td) / "state"
            reply = {
                "files": [
                    {
                        "path": "codey/qwen.py",
                        "content": "VALUE = 99\n",
                    }
                ]
            }

            with mock.patch(
                "codey.adapter_repair._run_static_checks",
                return_value=("passed:py_compile", "passed:ruff", "passed:provider_unittest"),
            ):
                result = run_adapter_repair(
                    "qwen",
                    send_prompt=lambda _prompt: __import__("json").dumps(reply),
                    state_home=state,
                    source_root=root,
                    run_canary=lambda _override: False,
                )

            self.assertFalse(result.ok)
            self.assertIn("canary", result.error)
            self.assertIsNone(adapter_overrides.load_enabled_override("qwen", state_home=state))

    def test_adapter_repair_rejects_modified_readonly_tests(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "src"
            _source_tree(root)
            state = Path(td) / "state"
            reply = {
                "files": [
                    {
                        "path": "tests/test_qwen.py",
                        "content": "def test_qwen():\n    assert True\n",
                    }
                ]
            }

            result = run_adapter_repair(
                "qwen",
                send_prompt=lambda _prompt: __import__("json").dumps(reply),
                state_home=state,
                source_root=root,
            )

            self.assertFalse(result.ok)
            self.assertIn("disallowed file", result.error)
            self.assertIsNone(adapter_overrides.load_enabled_override("qwen", state_home=state))

    def test_worker_canary_requires_exact_marker_reply(self) -> None:
        override = mock.Mock()
        provider = mock.Mock()
        provider.send.side_effect = lambda prompt, timeout=None: str(prompt).rsplit(" ", 1)[-1]
        with mock.patch("codey.adapter_repair.WorkerChatProvider", return_value=provider) as worker:
            ok = run_worker_canary("qwen", override, state_home=Path("state"), attempts=2)

        self.assertTrue(ok)
        self.assertEqual(provider.new_chat.call_count, 2)
        self.assertEqual(provider.send.call_count, 2)
        provider.close.assert_called_once()
        worker.assert_called_once()

    def test_worker_canary_rejects_wrong_reply(self) -> None:
        override = mock.Mock()
        provider = mock.Mock()
        provider.send.return_value = "wrong"
        with mock.patch("codey.adapter_repair.WorkerChatProvider", return_value=provider):
            ok = run_worker_canary("qwen", override, state_home=Path("state"), attempts=1)

        self.assertFalse(ok)
        provider.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
