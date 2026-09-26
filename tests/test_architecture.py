from __future__ import annotations

import ast
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVENT_MATRIX_PATH = ROOT / "docs" / "codey_event_matrix.md"
TASK_FLOW_PATH = ROOT / "codey" / "operations" / "task_flow.py"
TASK_ENTRY_PATH = ROOT / "codey" / "operations" / "task_entry.py"
TASK_RUN_PATH = ROOT / "codey" / "operations" / "task_run.py"
TASK_PHASES_DIR = ROOT / "codey" / "operations" / "task_phases"
APP_CONTEXT_PATH = ROOT / "codey" / "app" / "context.py"


def task_phases_sources() -> str:
    return "".join(
        path.read_text(encoding="utf-8")
        for path in sorted(TASK_PHASES_DIR.glob("*.py"))
    )


def task_phases_imports() -> set[str]:
    imports: set[str] = set()
    for path in sorted(TASK_PHASES_DIR.glob("*.py")):
        imports |= imported_modules(path)
    return imports


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def codey_python_files(*parts: str) -> tuple[Path, ...]:
    return tuple(sorted((ROOT / "codey" / Path(*parts)).glob("*.py")))


def imports_with_forbidden_prefixes(path: Path, prefixes: set[str]) -> list[str]:
    return sorted(
        name
        for name in imported_modules(path)
        if name in prefixes or any(name.startswith(f"{item}.") for item in prefixes)
    )


def event_matrix_capability_ids() -> set[str]:
    lines = EVENT_MATRIX_PATH.read_text(encoding="utf-8").splitlines()
    try:
        start = lines.index("## Capability Vocabulary") + 1
    except ValueError as exc:
        raise AssertionError("missing capability vocabulary in event matrix") from exc
    ids: set[str] = set()
    for line in lines[start:]:
        stripped = line.strip()
        if ids and (stripped.startswith("## ") or stripped.endswith(":")):
            break
        if line.startswith("- `"):
            ids.add(line.split("`", 2)[1])
    if not ids:
        raise AssertionError("empty capability vocabulary in event matrix")
    return ids


class ArchitectureBoundaryTests(unittest.TestCase):
    def test_agent_runtime_has_no_browser_or_deepseek_dependency(self) -> None:
        imports = imported_modules(ROOT / "codey" / "agents" / "loop.py")

        self.assertNotIn("playwright.sync_api", imports)
        self.assertNotIn("codey.automation.browser", imports)
        self.assertNotIn("codey.providers.web_drivers.deepseek", imports)
        self.assertNotIn("codey.providers.web_drivers.qwen", imports)
        self.assertNotIn("codey.providers.web_drivers.stepfun", imports)
        self.assertNotIn("codey.providers.web_drivers.glm", imports)
        self.assertNotIn("codey.providers", imports)
        self.assertIn("codey.protocols", imports)

    def test_agent_loop_keeps_prompt_verification_and_tool_owners_at_the_boundary(self) -> None:
        imports = imported_modules(ROOT / "codey" / "agents" / "loop.py")
        forbidden = {
            "codey.completion",
            "codey.operations",
            "codey.toolchain",
            "codey.workspace.coding_context",
            "codey.workspace.context_epoch",
            "codey.workspace.context_source",
        }

        self.assertEqual(
            imports_with_forbidden_prefixes(ROOT / "codey" / "agents" / "loop.py", forbidden),
            [],
        )
        self.assertIn("codey.agents.prompt_context", imports)
        self.assertIn("codey.agents.tool_execution", imports)
        self.assertIn("codey.agents.verification_driver", imports)

    def test_runtime_package_does_not_import_business_layers(self) -> None:
        forbidden = {"codey.agents", "codey.ghost", "codey.operations"}
        offenders: dict[str, list[str]] = {}
        for path in sorted((ROOT / "codey" / "runtime").rglob("*.py")):
            blocked = imports_with_forbidden_prefixes(path, forbidden)
            if blocked:
                offenders[path.relative_to(ROOT).as_posix()] = blocked
        self.assertEqual(offenders, {})

    def test_agents_package_does_not_import_operations(self) -> None:
        offenders: dict[str, list[str]] = {}
        for path in codey_python_files("agents"):
            blocked = imports_with_forbidden_prefixes(path, {"codey.operations"})
            if blocked:
                offenders[path.relative_to(ROOT).as_posix()] = blocked
        self.assertEqual(offenders, {})

    def test_completion_package_does_not_import_app_provider_or_operations(self) -> None:
        forbidden = {"codey.app", "codey.operations", "codey.providers"}
        offenders: dict[str, list[str]] = {}
        for path in codey_python_files("completion"):
            blocked = imports_with_forbidden_prefixes(path, forbidden)
            if blocked:
                offenders[path.relative_to(ROOT).as_posix()] = blocked
        self.assertEqual(offenders, {})

    def test_orchestrators_create_providers_instead_of_browser_sessions(self) -> None:
        paths = (
            ("cli.py", ROOT / "codey" / "app" / "cli.py"),
            ("server.py", ROOT / "codey" / "app" / "server.py"),
            ("operations/task_entry.py", TASK_ENTRY_PATH),
            ("operations/task_run.py", TASK_RUN_PATH),
        )
        for name, path in paths:
            with self.subTest(name=name):
                imports = imported_modules(path)
                self.assertNotIn("codey.automation.browser", imports)
                self.assertNotIn("codey.providers.web_drivers.deepseek", imports)
                self.assertNotIn("codey.providers.web_drivers.qwen", imports)
                self.assertNotIn("codey.providers.web_drivers.stepfun", imports)
                self.assertNotIn("codey.providers.web_drivers.glm", imports)

    def test_deepseek_and_stepfun_share_stability_loop(self) -> None:
        base_source = (ROOT / "codey" / "providers" / "web_drivers" / "base.py").read_text(encoding="utf-8")
        self.assertIn("def wait_for_stable_completion", base_source)
        for name in ("deepseek.py", "stepfun.py"):
            path = ROOT / "codey" / "providers" / "web_drivers" / name
            source = path.read_text(encoding="utf-8")
            with self.subTest(driver=name):
                self.assertIn("from codey.providers.web_drivers import base", source)
                self.assertIn("wait_for_stable_completion(", source)
                self.assertNotIn("ctx.last = current", source)

    def test_http_server_delegates_task_orchestration(self) -> None:
        imports = imported_modules(ROOT / "codey" / "app" / "server.py")
        source = (ROOT / "codey" / "app" / "server.py").read_text(encoding="utf-8")
        submit_imports = imported_modules(ROOT / "codey" / "app" / "task_submit.py")

        # server.py keeps HTTP/SSE routing + STATE/boot; task wiring lives in
        # task_submit.py, execution in operations/.
        self.assertIn("from codey.app import task_submit", source)
        self.assertNotIn("codey.operations.task_entry", imports)
        self.assertIn("codey.operations.task_entry", submit_imports)
        self.assertNotIn("on_shell_request(cwd_rel", source)
        self.assertNotIn("conversation.prepare_model_handoff", source)

    def test_sse_event_bus_owns_replay_state(self) -> None:
        server_source = APP_CONTEXT_PATH.read_text(encoding="utf-8")
        bus_imports = imported_modules(ROOT / "codey" / "app" / "event_bus.py")

        self.assertIn("codey.app.event_bus", imported_modules(APP_CONTEXT_PATH))
        self.assertNotIn("self.subscribers", server_source)
        self.assertNotIn("self.event_replay", server_source)
        self.assertNotIn("self.event_sequence", server_source)
        self.assertIn("queue", bus_imports)

    def test_run_registry_owns_run_lifecycle_state(self) -> None:
        server_source = APP_CONTEXT_PATH.read_text(encoding="utf-8")
        registry_source = (ROOT / "codey" / "app" / "run_registry.py").read_text(encoding="utf-8")

        self.assertIn("codey.app.run_registry", imported_modules(APP_CONTEXT_PATH))
        self.assertIn("class RunRegistry", registry_source)
        self.assertNotIn("class RunSnapshot", server_source)
        self.assertIn("self._active_run: RunSnapshot | None", registry_source)
        self.assertIn("self._last_terminal_event: dict | None", registry_source)
        for token in (
            "self.active_run: RunSnapshot",
            "self.project: str | None",
            "self.task: str | None",
            "self.provider_id = DEFAULT_PROVIDER_ID",
            'self.status = "idle"',
            "self.last_terminal_event: dict",
            "self.last_shell_result: dict",
            "self.run_registry.active_run =",
            "self.run_registry.last_terminal_event =",
            "self.active_run = run",
            "self.busy = True",
            "self.last_terminal_event = payload",
            "self.last_shell_result = payload",
        ):
            with self.subTest(token=token):
                self.assertNotIn(token, server_source)

    def test_approval_registry_owns_pending_approval_maps(self) -> None:
        server_source = APP_CONTEXT_PATH.read_text(encoding="utf-8")
        approval_source = (ROOT / "codey" / "app" / "approval_registry.py").read_text(encoding="utf-8")

        self.assertIn("codey.app.approval_registry", imported_modules(APP_CONTEXT_PATH))
        self.assertIn("class ApprovalRegistry", approval_source)
        self.assertNotIn("self.pending_shell: dict", server_source)
        self.assertNotIn("self.pending_teach: dict", server_source)
        self.assertNotIn("def pending_shell(self", server_source)
        self.assertNotIn("def pending_teach(self", server_source)
        self.assertNotIn("self.pending_shell", approval_source)
        self.assertNotIn("self.pending_teach", approval_source)
        self.assertIn("self._pending_shell: dict", approval_source)
        self.assertIn("self._pending_teach: dict", approval_source)
        self.assertIn("def add_pending_shell_approval", server_source)
        self.assertIn("def pop_pending_shell_approval", server_source)
        self.assertIn("def resume_pending_teach", server_source)

    def test_provider_registry_owns_provider_sessions_and_health(self) -> None:
        server_source = APP_CONTEXT_PATH.read_text(encoding="utf-8")
        registry_source = (ROOT / "codey" / "app" / "provider_registry.py").read_text(encoding="utf-8")

        self.assertIn("codey.app.provider_registry", imported_modules(APP_CONTEXT_PATH))
        self.assertIn("class ProviderRegistry", registry_source)
        self.assertNotIn("self.provider_sessions: dict", server_source)
        self.assertNotIn("self.provider_supervisor = ProviderSupervisor", server_source)
        self.assertNotIn("self.sessions: dict", registry_source)
        self.assertIn("self._sessions: dict", registry_source)
        self.assertNotIn("def provider_sessions", server_source)
        self.assertIn("def sessions_snapshot", registry_source)
        self.assertNotIn("def provider_supervisor", server_source)
        self.assertIn("self.supervisor =", registry_source)

    def test_conversation_registry_owns_conversation_cache_and_store(self) -> None:
        server_source = APP_CONTEXT_PATH.read_text(encoding="utf-8")
        registry_source = (ROOT / "codey" / "app" / "conversation_registry.py").read_text(encoding="utf-8")

        self.assertIn("codey.app.conversation_registry", imported_modules(APP_CONTEXT_PATH))
        self.assertIn("class ConversationRegistry", registry_source)
        for token in (
            "self.conversations: dict",
            "self.conversation_tokens: dict",
            "self.conversation_store =",
            "def _save_conversation",
        ):
            with self.subTest(token=token):
                self.assertNotIn(token, server_source)

    def test_background_workers_own_single_flight_state(self) -> None:
        server_source = APP_CONTEXT_PATH.read_text(encoding="utf-8")
        ghost_source = (ROOT / "codey" / "app" / "ghost_daemon.py").read_text(encoding="utf-8")
        indexer_source = (ROOT / "codey" / "app" / "knowledge_indexer.py").read_text(encoding="utf-8")

        imports = imported_modules(APP_CONTEXT_PATH)
        self.assertIn("codey.app.ghost_daemon", imports)
        self.assertIn("codey.app.knowledge_indexer", imports)
        self.assertIn("class GhostSleepDaemon", ghost_source)
        self.assertIn("class KnowledgeIndexer", indexer_source)
        for token in (
            "_knowledge_rebuild_running",
            "_knowledge_rebuild_pending",
            "_ghost_sleep_running",
            "_ghost_sleep_pending",
            "_ghost_sleep_thread",
        ):
            with self.subTest(token=token):
                self.assertNotIn(token, server_source)

    def test_task_entry_and_task_run_have_no_http_dependency(self) -> None:
        for path in (TASK_ENTRY_PATH, TASK_RUN_PATH):
            with self.subTest(path=path.name):
                imports = imported_modules(path)
                self.assertNotIn("http.server", imports)
                self.assertNotIn("codey.app.server", imports)

    def test_legacy_task_flow_facade_is_removed(self) -> None:
        self.assertFalse((ROOT / "codey" / "task" / "service.py").exists())
        self.assertFalse(TASK_FLOW_PATH.exists())

    def test_task_submission_model_is_not_defined_by_execution_service(self) -> None:
        model_source = (ROOT / "codey" / "task" / "model.py").read_text(encoding="utf-8")
        entry_source = TASK_ENTRY_PATH.read_text(encoding="utf-8")
        task_run_source = TASK_RUN_PATH.read_text(encoding="utf-8")
        import codey.task as task_package

        self.assertIn("class TaskSubmission", model_source)
        self.assertNotIn("class TaskSubmission", entry_source)
        self.assertNotIn("class TaskSubmission", task_run_source)
        self.assertFalse(hasattr(task_package, "TaskFlow"))

    def test_operation_context_values_are_not_defined_by_task_flow(self) -> None:
        task_run_source = TASK_RUN_PATH.read_text(encoding="utf-8")
        phases_source = task_phases_sources()
        context_source = (ROOT / "codey" / "operations" / "context.py").read_text(encoding="utf-8")
        result_source = (ROOT / "codey" / "operations" / "result.py").read_text(encoding="utf-8")
        imports = imported_modules(TASK_RUN_PATH) | task_phases_imports()

        self.assertIn("codey.operations.context", imports)
        self.assertIn("codey.operations.result", imports)
        for token in ("_RunFrame", "_RunWork", "_RunHooks", "_ModeOutcome", "_TaskSubmission"):
            with self.subTest(token=token):
                self.assertNotIn(token, task_run_source)
                self.assertNotIn(token, phases_source)
        self.assertIn("class RunFrame", context_source)
        self.assertIn("class RunWork", context_source)
        self.assertIn("class RunHooks", context_source)
        self.assertIn("class ModeOutcome", result_source)

    def test_chat_mode_logic_lives_in_chat_operation(self) -> None:
        task_run_source = TASK_RUN_PATH.read_text(encoding="utf-8")
        phases_source = task_phases_sources()
        chat_source = (ROOT / "codey" / "operations" / "chat.py").read_text(encoding="utf-8")

        self.assertIn(
            "codey.operations.chat",
            imported_modules(TASK_RUN_PATH) | task_phases_imports(),
        )
        self.assertIn("def run_chat_mode", chat_source)
        self.assertIn("run_chat_mode(", task_run_source + phases_source)
        self.assertNotIn("chat_outbound_prompt", task_run_source)
        self.assertNotIn("chat_outbound_prompt", phases_source)
        self.assertIn("chat_outbound_prompt", chat_source)

    def test_project_completion_logic_lives_in_project_completion_operation(self) -> None:
        task_run_source = TASK_RUN_PATH.read_text(encoding="utf-8")
        phases_source = task_phases_sources()
        completion_source = (ROOT / "codey" / "operations" / "project_completion_flow.py").read_text(encoding="utf-8")

        self.assertIn("def run_project_mode", completion_source)
        self.assertIn("WriterFailoverRunner", completion_source)
        self.assertIn("project_repair_context", completion_source)
        self.assertIn("build_task_receipt", completion_source)
        self.assertIn("run_project_mode(", task_run_source + phases_source)
        self.assertNotIn("def _run_project_mode", task_run_source)
        self.assertNotIn("def _run_project_mode", phases_source)
        self.assertNotIn("ReviewCoordinator", task_run_source)
        self.assertNotIn("ReviewCoordinator", phases_source)
        self.assertNotIn("project_repair_context", task_run_source)
        self.assertNotIn("project_repair_context", phases_source)
        self.assertNotIn("build_task_receipt(", task_run_source)
        self.assertNotIn("build_task_receipt(", phases_source)

    def test_legacy_task_runner_module_is_gone(self) -> None:
        self.assertFalse((ROOT / "codey" / "app" / "task_runner.py").exists())
        offenders: list[str] = []
        for path in sorted((ROOT / "codey").rglob("*.py")):
            imports = imported_modules(path)
            if "codey.app.task_runner" in imports:
                offenders.append(path.relative_to(ROOT).as_posix())
        self.assertEqual(offenders, [])

    def test_runtime_kernel_stays_below_app_provider_and_domain_layers(self) -> None:
        forbidden = {
            "codey.agents",
            "codey.app",
            "codey.completion",
            "codey.ghost",
            "codey.providers",
            "codey.research",
            "codey.reviews",
            "codey.toolchain",
            "playwright.sync_api",
        }
        allowed = {
            "codey.runtime.core.cancellation",
            "codey.runtime.observe.events",
            "codey.runtime.observe.execution_evidence",
            "codey.runtime.effects.effect_records",
            "codey.runtime.write.mutation_line",
            "codey.runtime.core.models",
            "codey.runtime.core.operation",
            "codey.runtime.core.operation_reducer",
            "codey.runtime.core.operation_state",
            "codey.runtime.core.outcome",
            "codey.runtime.core.output_capture",
            "codey.runtime.observe.prompt_envelope",
            "codey.runtime.effects.replay_args",
            "codey.runtime.effects.replay_policy",
            "codey.runtime.effects.keep_policies",
            "codey.runtime.log.compaction",
            "codey.runtime.log.entries",
            "codey.runtime.log.session_projection",
            "codey.runtime.log.session_log",
            "codey.runtime.log.session_view",
            "codey.runtime.observe.terminalizer",
            "codey.runtime.write.delivery_recovery",
            "codey.runtime.write.drive",
            "codey.runtime.write.provider_effects",
            "codey.runtime.write.tool_batches",
            "codey.runtime.effects.tool_result_delivery",
            "codey.storage.atomic_io",
            "codey.storage.file_lock",
            "codey.storage.local_store",
        }
        offenders: dict[str, list[str]] = {}
        kernel_files = (
            "core/operation.py",
            "core/operation_state.py",
            "core/operation_reducer.py",
            "core/outcome.py",
            "core/models.py",
            "core/cancellation.py",
            "write/mutation_line.py",
            "write/drive.py",
            "write/provider_effects.py",
            "write/tool_batches.py",
            "write/delivery_recovery.py",
            "log/session_log.py",
            "log/session_projection.py",
            "log/session_view.py",
            "log/compaction.py",
            "observe/terminalizer.py",
        )
        self.assertFalse((ROOT / "codey" / "runtime" / "effects.py").exists())
        self.assertFalse((ROOT / "codey" / "runtime" / "reducer.py").exists())
        self.assertFalse((ROOT / "codey" / "runtime" / "scheduler.py").exists())
        self.assertFalse((ROOT / "codey" / "runtime" / "lane.py").exists())
        self.assertFalse((ROOT / "codey" / "runtime" / "suspension.py").exists())
        for flat in (
            "operation.py",
            "operation_state.py",
            "operation_reducer.py",
            "mutation_line.py",
            "drive.py",
            "outcome.py",
            "session_projection.py",
            "session_log.py",
            "session_view.py",
            "effect_records.py",
            "tool_result_delivery.py",
            "terminalizer.py",
        ):
            with self.subTest(flat=flat):
                self.assertFalse((ROOT / "codey" / "runtime" / flat).exists())
        session_log_source = (ROOT / "codey" / "runtime" / "log" / "session_log.py").read_text(encoding="utf-8")
        delivery_source = (ROOT / "codey" / "runtime" / "effects" / "tool_result_delivery.py").read_text(
            encoding="utf-8"
        )
        operation_state_source = (ROOT / "codey" / "runtime" / "core" / "operation_state.py").read_text(
            encoding="utf-8"
        )
        mutation_line_source = (ROOT / "codey" / "runtime" / "write" / "mutation_line.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("def append(", session_log_source)
        self.assertNotIn("def append_many(", session_log_source)
        self.assertNotIn("def transition_operation(", mutation_line_source)
        for token in (
            "def start(",
            "def commit(",
            "def delete_session(",
        ):
            with self.subTest(token=token):
                self.assertNotIn(token, operation_state_source)
        for token in (
            "def record_batch_intent(",
            "def record_send_attempt(",
            "def record_delivered(",
            "def record_recovered(",
        ):
            with self.subTest(token=token):
                self.assertNotIn(token, delivery_source)
        for name in kernel_files:
            path = ROOT / "codey" / "runtime" / name
            imports = imported_modules(path)
            blocked = sorted(
                name for name in imports if name in forbidden or any(name.startswith(f"{item}.") for item in forbidden)
            )
            unexpected_codey = sorted(
                name for name in imports if (name == "codey" or name.startswith("codey.")) and name not in allowed
            )
            if blocked or unexpected_codey:
                offenders[path.relative_to(ROOT).as_posix()] = blocked + unexpected_codey
        self.assertEqual(offenders, {})

        mutate_callers = []
        allowed_mutate_callers = {
            "codey/runtime/write/mutation_line.py",
            "codey/runtime/log/session_log.py",
        }
        for path in (ROOT / "codey").rglob("*.py"):
            rel = path.relative_to(ROOT).as_posix()
            if rel in allowed_mutate_callers:
                continue
            source = path.read_text(encoding="utf-8")
            if ".mutate(" in source:
                mutate_callers.append(rel)
        self.assertEqual(mutate_callers, [])

        test_mutate_callers = []
        allowed_test_mutate_callers = {
            "tests/test_architecture.py",
            "tests/test_runtime_session_log.py",
        }
        for path in (ROOT / "tests").rglob("*.py"):
            rel = path.relative_to(ROOT).as_posix()
            if rel in allowed_test_mutate_callers:
                continue
            source = path.read_text(encoding="utf-8")
            if ".mutate(" in source:
                test_mutate_callers.append(rel)
        self.assertEqual(test_mutate_callers, [])

    def test_runtime_write_and_observe_stay_separated(self) -> None:
        write_offenders: dict[str, list[str]] = {}
        for path in sorted((ROOT / "codey" / "runtime" / "write").rglob("*.py")):
            blocked = imports_with_forbidden_prefixes(path, {"codey.runtime.observe"})
            if blocked:
                write_offenders[path.relative_to(ROOT).as_posix()] = blocked
        self.assertEqual(write_offenders, {})
        observe_offenders: dict[str, list[str]] = {}
        for path in sorted((ROOT / "codey" / "runtime" / "observe").rglob("*.py")):
            blocked = imports_with_forbidden_prefixes(path, {"codey.runtime.write"})
            if blocked:
                observe_offenders[path.relative_to(ROOT).as_posix()] = blocked
        self.assertEqual(observe_offenders, {})

    def test_session_view_is_canonical_runtime_read_model(self) -> None:
        view_path = ROOT / "codey" / "runtime" / "log" / "session_view.py"
        self.assertTrue(view_path.exists(), "missing codey/runtime/log/session_view.py")
        tree = ast.parse(view_path.read_text(encoding="utf-8"), filename=str(view_path))
        fields: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "SessionView":
                for item in node.body:
                    if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                        fields.append(item.target.id)
        self.assertEqual(fields, ["state", "effects", "batches"])
        forbidden_view = {
            "codey.completion",
            "codey.research",
            "codey.ghost",
            "codey.providers",
            "codey.app",
            "codey.agents",
            "codey.evidence",
        }
        self.assertEqual(imports_with_forbidden_prefixes(view_path, forbidden_view), [])
        drive_imports = imported_modules(ROOT / "codey" / "runtime" / "write" / "drive.py")
        for token in (
            "codey.runtime.effects.effect_records",
            "codey.runtime.effects.tool_result_delivery",
            "codey.runtime.core.operation_state",
        ):
            self.assertNotIn(token, drive_imports)

    def test_research_pipeline_owns_iteration_boundary_without_legacy_seams(self) -> None:
        pipeline = ROOT / "codey" / "research" / "pipeline.py"
        research_flow = ROOT / "codey" / "operations" / "research_flow.py"
        research_runner = ROOT / "codey" / "research" / "runner.py"
        pipeline_source = pipeline.read_text(encoding="utf-8")
        research_flow_source = research_flow.read_text(encoding="utf-8")
        task_run_source = TASK_RUN_PATH.read_text(encoding="utf-8")
        research_runner_source = research_runner.read_text(encoding="utf-8")

        self.assertIn("ResearchIterationRun", pipeline_source)
        self.assertIn("ResearchIterationRun", research_flow_source)
        self.assertNotIn("ResearchIterationRun", task_run_source)
        self.assertNotIn("codey.operations.task_flow", imported_modules(pipeline))
        self.assertNotIn("codey.app.server", imported_modules(pipeline))
        self.assertNotIn("_run_research_task", task_run_source)
        self.assertNotIn("_run_research_iteration", task_run_source)
        self.assertNotIn("close_search", pipeline_source)
        self.assertNotIn("runtime_tools", research_runner_source)

    def test_ghost_runtime_has_no_provider_browser_tool_or_research_dependency(self) -> None:
        forbidden = {
            "torch",
            "transformers",
            "codey.automation.browser",
            "codey.providers",
            "codey.toolchain.runtime",
            "codey.research.runner",
            "codey.research.tools",
        }
        for path in (ROOT / "codey" / "ghost").glob("*.py"):
            with self.subTest(path=path.name):
                imports = imported_modules(path)
                self.assertTrue(forbidden.isdisjoint(imports), sorted(forbidden & imports))

    def test_affinity_boundaries_do_not_expand_execution_or_research_authority(self) -> None:
        affinity_imports = imported_modules(ROOT / "codey" / "ghost" / "affinity.py")
        forbidden_affinity = {
            "torch",
            "transformers",
            "codey.automation.browser",
            "codey.providers",
            "codey.providers.controls",
            "codey.toolchain.runtime",
            "codey.research.runner",
            "codey.research.tools",
        }
        self.assertTrue(
            forbidden_affinity.isdisjoint(affinity_imports),
            sorted(forbidden_affinity & affinity_imports),
        )

        research_imports = imported_modules(ROOT / "codey" / "research" / "runner.py")
        permission_imports = imported_modules(ROOT / "codey" / "policies" / "permissions.py")
        repair_source = (ROOT / "codey" / "repairs" / "adapter_repair.py").read_text(encoding="utf-8")
        tool_runtime_imports = imported_modules(ROOT / "codey" / "toolchain" / "runtime.py")

        self.assertNotIn("codey.ghost.affinity", research_imports)
        self.assertNotIn("codey.ghost.affinity", permission_imports)
        self.assertNotIn("affinity", repair_source.casefold())
        self.assertNotIn("codey.ghost", tool_runtime_imports)

    def test_ghost_package_root_has_small_public_surface(self) -> None:
        source = (ROOT / "codey" / "ghost" / "__init__.py").read_text(encoding="utf-8")

        self.assertIn('"GhostControlSurface"', source)
        self.assertNotIn("GhostHebbianStore", source)
        self.assertNotIn("GhostAffinityStore", source)
        self.assertNotIn("GhostSleepStore", source)
        self.assertNotIn("GhostRouter", source)

    def test_context_epoch_is_projection_only_leaf(self) -> None:
        # Context Epoch projects admission metadata over already-rendered
        # sources; it must stay a stdlib-only leaf with no runtime imports
        # and no I/O of its own.
        path = ROOT / "codey" / "workspace" / "context_epoch.py"
        imports = imported_modules(path)
        source = path.read_text(encoding="utf-8")

        internal_imports = sorted(name for name in imports if name == "codey" or name.startswith("codey."))
        self.assertEqual(internal_imports, [])
        forbidden_source = (
            "write_text(",
            "write_json",
            "open(",
            "eval(",
            "exec(",
            "subprocess",
            "urllib",
            "pathlib",
        )
        for token in forbidden_source:
            with self.subTest(token=token):
                self.assertNotIn(token, source)

    def test_prompt_envelope_is_not_a_provider_or_tool_runtime_seam(self) -> None:
        imports = imported_modules(ROOT / "codey" / "runtime" / "observe" / "prompt_envelope.py")
        forbidden = {
            "codey.automation.browser",
            "codey.providers.web_drivers.deepseek",
            "codey.providers.web_drivers.qwen",
            "codey.providers.web_drivers.stepfun",
            "codey.providers.web_drivers.glm",
            "codey.providers",
            "codey.providers.controls",
            "codey.toolchain.runtime",
            "codey.research.runner",
            "codey.ghost",
        }

        self.assertTrue(forbidden.isdisjoint(imports), sorted(forbidden & imports))

    def test_capability_registry_production_module_is_gone(self) -> None:
        self.assertFalse((ROOT / "codey" / "policies" / "capability_registry.py").exists())
        offenders: list[str] = []
        for path in sorted((ROOT / "codey").rglob("*.py")):
            imports = imported_modules(path)
            if "codey.policies.capability_registry" in imports:
                offenders.append(path.relative_to(ROOT).as_posix())
        self.assertEqual(offenders, [])

    def test_task_run_does_not_carry_capability_registry_for_decisions(self) -> None:
        source = TASK_RUN_PATH.read_text(encoding="utf-8")

        self.assertNotIn("CapabilityRegistry", source)
        self.assertNotIn("self.capabilities", source)
        self.assertNotIn("if capabilities", source)

    def test_builtin_profiles_module_is_gone(self) -> None:
        # The metadata-only catalog never influenced any decision and was
        # removed ahead of 0.4.x instead of shipping dead surface.
        self.assertFalse((ROOT / "codey" / "builtin_profiles.py").exists())
        source = TASK_RUN_PATH.read_text(encoding="utf-8")
        self.assertNotIn("builtin_profiles", source)
        server_source = (ROOT / "codey" / "app" / "server.py").read_text(encoding="utf-8")
        self.assertNotIn("builtin_profiles", server_source)

    def test_action_policy_is_not_runtime_or_plugin_host(self) -> None:
        path = ROOT / "codey" / "policies" / "action.py"
        imports = imported_modules(path)
        source = path.read_text(encoding="utf-8")
        forbidden_imports = {
            "codey.automation.browser",
            "codey.providers.web_drivers.deepseek",
            "codey.providers.web_drivers.qwen",
            "codey.providers.web_drivers.stepfun",
            "codey.providers.web_drivers.glm",
            "codey.providers",
            "codey.providers.controls",
            "codey.toolchain.runtime",
            "codey.app.server",
            "codey.operations.task_flow",
            "importlib",
            "pkgutil",
        }
        forbidden_source = (
            "entry_points",
            "load_plugin",
            "register_runtime",
            "def is_allowed_run_command",
            "def is_suite_run_command",
            "RUN_ALLOWED_",
            "dispatch(",
            "subprocess.",
            "eval(",
            "exec(",
        )

        self.assertTrue(
            forbidden_imports.isdisjoint(imports),
            sorted(forbidden_imports & imports),
        )
        for token in forbidden_source:
            with self.subTest(token=token):
                self.assertNotIn(token, source)

    def test_run_details_is_read_only_projection_not_runtime(self) -> None:
        path = ROOT / "codey" / "runs" / "details.py"
        imports = imported_modules(path)
        source = path.read_text(encoding="utf-8")
        forbidden_imports = {
            "codey.automation.browser",
            "codey.providers.web_drivers.deepseek",
            "codey.providers.web_drivers.qwen",
            "codey.providers.web_drivers.stepfun",
            "codey.providers.web_drivers.glm",
            "codey.providers",
            "codey.providers.controls",
            "codey.toolchain.runtime",
            "codey.app.server",
            "codey.operations.task_flow",
            "importlib",
            "pkgutil",
        }
        forbidden_source = (
            "entry_points",
            "load_plugin",
            "register_runtime",
            "dispatch(",
            "subprocess.",
            "eval(",
            "exec(",
            "write_text(",
            "write_json",
        )

        self.assertTrue(
            forbidden_imports.isdisjoint(imports),
            sorted(forbidden_imports & imports),
        )
        for token in forbidden_source:
            with self.subTest(token=token):
                self.assertNotIn(token, source)

    def test_edit_scope_is_stdlib_leaf(self) -> None:
        # The edit-scope vocabulary shares one pure path normalizer
        # (codey.utils.change_paths, itself stdlib-only) instead of
        # duplicating it: the only allowed internal import is that leaf.
        path = ROOT / "codey" / "completion" / "edit_scope.py"
        imports = imported_modules(path)
        internal = sorted(name for name in imports if name == "codey" or name.startswith("codey."))
        self.assertEqual(internal, ["codey.utils.change_paths"])

    def test_edit_integrity_is_projection_leaf(self) -> None:
        path = ROOT / "codey" / "completion" / "edit_integrity.py"
        imports = imported_modules(path)
        forbidden = {
            "codey.automation.browser",
            "codey.providers",
            "codey.toolchain",
            "codey.toolchain.runtime",
            "codey.app.server",
            "codey.operations.task_flow",
            "codey.ghost",
            "codey.research",
        }
        self.assertTrue(forbidden.isdisjoint(imports), sorted(forbidden & imports))
        allowed_internal = {"codey.completion.edit_scope", "codey.utils.refs"}
        internal = {name for name in imports if name == "codey" or name.startswith("codey.")}
        self.assertTrue(internal.issubset(allowed_internal), sorted(internal - allowed_internal))

    def test_completion_decision_is_pure_projection(self) -> None:
        path = ROOT / "codey" / "completion" / "decision.py"
        imports = imported_modules(path)
        forbidden = {
            "codey.automation.browser",
            "codey.providers",
            "codey.toolchain",
            "codey.app.server",
            "codey.operations.task_flow",
        }
        self.assertTrue(forbidden.isdisjoint(imports), sorted(forbidden & imports))

    def test_research_object_model_is_projection_not_runtime(self) -> None:
        path = ROOT / "codey" / "research" / "object_model.py"
        imports = imported_modules(path)
        source = path.read_text(encoding="utf-8")
        forbidden_imports = {
            "codey.automation.browser",
            "codey.providers.web_drivers.deepseek",
            "codey.providers.web_drivers.qwen",
            "codey.providers.web_drivers.stepfun",
            "codey.providers.web_drivers.glm",
            "codey.providers",
            "codey.providers.controls",
            "codey.toolchain.runtime",
            "codey.app.server",
            "codey.operations.task_flow",
            "codey.ghost",
            "importlib",
            "pkgutil",
        }
        forbidden_source = (
            "entry_points",
            "load_plugin",
            "register_runtime",
            "dispatch(",
            "subprocess.",
            "eval(",
            "exec(",
            "write_text(",
            "write_json",
        )

        self.assertTrue(
            forbidden_imports.isdisjoint(imports),
            sorted(forbidden_imports & imports),
        )
        for token in forbidden_source:
            with self.subTest(token=token):
                self.assertNotIn(token, source)

    def test_research_identity_ledger_and_proof_do_not_import_runtime_layers(self) -> None:
        forbidden_imports = {
            "codey.automation.browser",
            "codey.providers.web_drivers.deepseek",
            "codey.providers.web_drivers.qwen",
            "codey.providers.web_drivers.stepfun",
            "codey.providers.web_drivers.glm",
            "codey.providers",
            "codey.providers.controls",
            "codey.toolchain.runtime",
            "codey.app.server",
            "codey.operations.task_flow",
            "codey.ghost",
            "importlib",
            "pkgutil",
            "subprocess",
        }
        for path in (
            ROOT / "codey" / "research" / "identity.py",
            ROOT / "codey" / "research" / "evidence_ledger.py",
            ROOT / "codey" / "research" / "proof_quality.py",
            ROOT / "codey" / "research" / "completion_gate.py",
            ROOT / "codey" / "research" / "source_connectors.py",
            ROOT / "codey" / "research" / "connector_terms.py",
            ROOT / "codey" / "research" / "query_planner.py",
            ROOT / "codey" / "research" / "connector_search.py",
            ROOT / "codey" / "research" / "plan_executor.py",
            ROOT / "codey" / "research" / "evidence_followup.py",
            ROOT / "codey" / "research" / "record_merge.py",
        ):
            with self.subTest(path=path.relative_to(ROOT).as_posix()):
                imports = imported_modules(path)
                self.assertTrue(
                    forbidden_imports.isdisjoint(imports),
                    sorted(forbidden_imports & imports),
                )

    def test_source_domains_is_a_stdlib_data_leaf(self) -> None:
        # The shared host-domain tables have exactly one owner and no
        # behavior: both the capture-time classifier and the trust
        # projection consume them, so they must stay import-cycle-free.
        # codey.utils.refs is the one allowed import (the shared stdlib hostname
        # shape predicate); no other codey module, no I/O.
        path = ROOT / "codey" / "research" / "source_domains.py"
        imports = imported_modules(path)

        self.assertEqual(
            sorted(name for name in imports if name == "codey" or name.startswith("codey.")),
            ["codey.utils.refs"],
        )
        source = path.read_text(encoding="utf-8")
        for token in (
            "write_text(",
            "write_json",
            "open(",
            "eval(",
            "exec(",
            "subprocess",
            "urllib",
            "urlparse",
        ):
            self.assertNotIn(token, source)

    def test_research_review_and_local_context_do_not_import_tool_runtime(self) -> None:
        paths = [
            *(ROOT / "codey" / "research").glob("*.py"),
            ROOT / "codey" / "reviews" / "core.py",
            ROOT / "codey" / "reviews" / "coordinator.py",
            *(ROOT / "codey" / "ghost").glob("*.py"),
        ]
        for path in paths:
            with self.subTest(path=path.relative_to(ROOT).as_posix()):
                imports = imported_modules(path)
                self.assertNotIn("codey.toolchain.runtime", imports)
                # managed_outputs sits on the runtime side of the boundary
                # (it imports tool_runtime), so research/review/ghost modules
                # must consume normalized metadata dicts instead.
                self.assertNotIn("codey.storage.managed_outputs", imports)

    def test_analysis_run_projection_stays_pure(self) -> None:
        analysis_run_imports = imported_modules(ROOT / "codey" / "research" / "analysis_run.py")
        lineage_imports = imported_modules(ROOT / "codey" / "research" / "artifact_lineage.py")
        capsule_imports = imported_modules(ROOT / "codey" / "research" / "reproducibility.py")
        forbidden = {
            "codey.runtime.observe.events",
            "codey.toolchain.runtime",
            "codey.storage.managed_outputs",
            "codey.operations.task_flow",
            "codey.app.server",
        }
        for name, imports in (
            ("analysis_run", analysis_run_imports),
            ("artifact_lineage", lineage_imports),
            ("reproducibility", capsule_imports),
        ):
            with self.subTest(module=name):
                self.assertTrue(forbidden.isdisjoint(imports), sorted(forbidden & imports))

    def test_research_behavior_modules_do_not_read_trace_or_ui_projections(self) -> None:
        # Projection-of-projection is forbidden: behavior-side research
        # modules consume canonical facts, never the trace/UI read models.
        forbidden = {
            "codey.runs.trace",
            "codey.runs.details",
            "codey.runs.ledger_projection",
        }
        paths = [
            ROOT / "codey" / "research" / "query_planner.py",
            ROOT / "codey" / "research" / "proof_quality.py",
            ROOT / "codey" / "research" / "brief_projection.py",
            ROOT / "codey" / "research" / "source_trust.py",
        ]
        for path in paths:
            with self.subTest(path=path.relative_to(ROOT).as_posix()):
                imports = imported_modules(path)
                self.assertTrue(
                    forbidden.isdisjoint(imports),
                    sorted(forbidden & imports),
                )

    def test_profile_source_trust_combination_has_single_owner(self) -> None:
        # Composing evidence profiles with source trust must live in exactly
        # one place. Today nothing combines them; if a consumer ever needs
        # to, it must become a dedicated owner module -- not another import
        # site that quietly grows policy logic.
        offenders = []
        for path in sorted((ROOT / "codey").rglob("*.py")):
            imports = imported_modules(path)
            if "codey.research.domain_profiles" in imports and "codey.research.source_trust" in imports:
                offenders.append(path.relative_to(ROOT).as_posix())
        self.assertEqual(offenders, [])

    def test_refs_and_redaction_are_stdlib_leaves(self) -> None:
        # The bounded ref vocabulary and redaction predicates are the shared
        # dialect of every refs-only read model (coding, research, future
        # experiment domains). They stay domain-neutral stdlib leaves so no
        # projection has to reach into another domain's namespace to speak.
        paths = (
            ("refs.py", ROOT / "codey" / "utils" / "refs.py"),
            ("redaction.py", ROOT / "codey" / "policies" / "redaction.py"),
        )
        for name, path in paths:
            with self.subTest(module=name):
                imports = imported_modules(path)
                self.assertEqual(
                    [item for item in imports if item == "codey" or item.startswith("codey.")],
                    [],
                )
                source = path.read_text(encoding="utf-8")
                for token in (
                    "write_text(",
                    "write_json",
                    "open(",
                    "eval(",
                    "exec(",
                    "subprocess",
                    "urllib",
                    "pathlib",
                ):
                    self.assertNotIn(token, source)

    def test_digest_helpers_are_not_imported_under_neutral_aliases(self) -> None:
        # AST-based so whitespace/quoting can never smuggle an aliased
        # import past a string scan again.
        offenders: list[str] = []
        watched = {"content_digest", "valid_digest_ref"}
        for path in sorted((ROOT / "codey").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    offenders.extend(
                        f"{path.relative_to(ROOT).as_posix()}: import {alias.name}"
                        for alias in node.names
                        if alias.asname == "_digest_ref"
                    )
                elif isinstance(node, ast.ImportFrom):
                    if node.module and node.module.split(".")[-1] != "refs":
                        continue
                    offenders.extend(
                        f"{path.relative_to(ROOT).as_posix()}: from {node.module} import {alias.name} as _digest_ref"
                        for alias in node.names
                        if alias.name in watched and alias.asname == "_digest_ref"
                    )

        self.assertEqual(offenders, [])

    def test_completion_contract_modules_are_projection_only(self) -> None:
        # The completion contract is the Verified Completion Gate's pure core:
        # it derives proofs from facts handed to it, and must never reach
        # execution layers, providers, I/O, or model-visible surfaces. The
        # queue gate itself stays under the existing research import-boundary
        # test above.
        paths = (
            ROOT / "codey" / "completion" / "contract.py",
            ROOT / "codey" / "research" / "contract.py",
        )
        forbidden_imports = {
            "codey.automation.browser",
            "codey.providers.web_drivers.deepseek",
            "codey.providers.web_drivers.qwen",
            "codey.providers.web_drivers.stepfun",
            "codey.providers.web_drivers.glm",
            "codey.providers",
            "codey.providers.controls",
            "codey.toolchain.runtime",
            "codey.storage.managed_outputs",
            "codey.runtime.observe.events",
            "codey.app.server",
            "codey.operations.task_flow",
            "codey.ghost",
            "codey.reviews.core",
            "importlib",
            "pkgutil",
            "subprocess",
            "urllib",
        }
        forbidden_source = (
            "eval(",
            "exec(",
            "write_text(",
            "write_json",
            "open(",
            "pathlib",
        )
        for path in paths:
            with self.subTest(path=path.relative_to(ROOT).as_posix()):
                imports = imported_modules(path)
                self.assertTrue(
                    forbidden_imports.isdisjoint(imports),
                    sorted(forbidden_imports & imports),
                )
                source = path.read_text(encoding="utf-8")
                for token in forbidden_source:
                    self.assertNotIn(token, source)

    def test_evidence_runtime_and_review_finding_are_projection_only(self) -> None:
        # Evidence Runtime and ReviewFinding explain facts that already exist;
        # they must not reach execution layers, providers, the A/B journal, or
        # the code review parser they intentionally do not migrate.
        paths = (
            ROOT / "codey" / "research" / "evidence_runtime.py",
            ROOT / "codey" / "research" / "review_finding.py",
        )
        forbidden = {
            "codey.automation.browser",
            "codey.providers.web_drivers.deepseek",
            "codey.providers.web_drivers.qwen",
            "codey.providers.web_drivers.stepfun",
            "codey.providers.web_drivers.glm",
            "codey.providers",
            "codey.providers.controls",
            "codey.toolchain.runtime",
            "codey.storage.managed_outputs",
            "codey.app.server",
            "codey.operations.task_flow",
            "codey.ghost",
            "codey.reviews.core",
            "ab_journal",
            "tests.manual.ab_journal",
            "importlib",
            "pkgutil",
            "subprocess",
        }
        forbidden_source = (
            "eval(",
            "exec(",
            "write_text(",
            "write_json",
        )
        for path in paths:
            with self.subTest(path=path.relative_to(ROOT).as_posix()):
                imports = imported_modules(path)
                self.assertTrue(forbidden.isdisjoint(imports), sorted(forbidden & imports))
                source = path.read_text(encoding="utf-8")
                for token in forbidden_source:
                    self.assertNotIn(token, source)

    def test_research_benchmark_scorer_is_tooling_only(self) -> None:
        # The 0.4.11 scorer is the evaluation spine's pure core: it consumes
        # existing projection payloads and emits bounded metrics, observables,
        # and verdicts. It must never reach execution layers, providers, the
        # A/B journal, or perform any I/O of its own.
        path = ROOT / "tools" / "research_benchmark" / "scorer.py"
        imports = imported_modules(path)
        source = path.read_text(encoding="utf-8")

        forbidden_imports = {
            "codey.automation.browser",
            "codey.providers.web_drivers.deepseek",
            "codey.providers.web_drivers.qwen",
            "codey.providers.web_drivers.stepfun",
            "codey.providers.web_drivers.glm",
            "codey.providers",
            "codey.providers.controls",
            "codey.toolchain.runtime",
            "codey.storage.managed_outputs",
            "codey.runtime.observe.events",
            "codey.app.server",
            "codey.operations.task_flow",
            "codey.ghost",
            "codey.reviews.core",
            "codey.knowledge",
            "codey.runs.trace",
            "codey.runs.details",
            "ab_journal",
            "tests.manual.ab_journal",
            "importlib",
            "pkgutil",
            "subprocess",
            "urllib",
        }
        forbidden_source = (
            "eval(",
            "exec(",
            "write_text(",
            "write_json",
            "open(",
            "pathlib",
            "requests.",
        )

        self.assertTrue(
            forbidden_imports.isdisjoint(imports),
            sorted(forbidden_imports & imports),
        )
        for token in forbidden_source:
            self.assertNotIn(token, source)

    def test_prompt_surface_source_has_no_dead_epoch_helper(self) -> None:
        source = (ROOT / "codey" / "runtime" / "observe" / "prompt_surface.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        functions = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}

        self.assertNotIn("_is_epoch", functions)
        self.assertNotIn("_is_epoch(", source)

    def test_research_followup_scorers_are_pure_leaf_modules(self) -> None:
        for path in (
            ROOT / "codey" / "research" / "followup_selection.py",
            ROOT / "tests" / "manual" / "research_scorers" / "followup_quality.py",
            ROOT / "tests" / "manual" / "research_scorers" / "source_finalizer_scoring.py",
        ):
            with self.subTest(path=path.relative_to(ROOT).as_posix()):
                self._assert_stdlib_leaf(path)

    def test_experiment_scorers_stay_out_of_production_research(self) -> None:
        # A/B scorer helpers are manual-layer only; if they drift back into
        # codey/research they become unowned dead code, so ratchet their absence.
        for name in ("followup_quality.py", "source_finalizer_scoring.py"):
            with self.subTest(module=name):
                self.assertFalse(
                    (ROOT / "codey" / "research" / name).exists(),
                    f"experiment scorer belongs in tests/manual/research_scorers: {name}",
                )

    def test_research_regression_gate_production_module_is_gone(self) -> None:
        self.assertFalse((ROOT / "codey" / "research" / "regression_gate.py").exists())
        source = (ROOT / "codey" / "research" / "__init__.py").read_text(encoding="utf-8")
        self.assertNotIn("regression_gate", source)

    def test_production_never_imports_the_manual_layer(self) -> None:
        # Manual harnesses, journals, and benchmark tooling are experiment-
        # layer only; no production module may import them in either direction.
        offenders: list[str] = []
        for path in sorted((ROOT / "codey").rglob("*.py")):
            imports = imported_modules(path)
            bad = [
                name
                for name in imports
                if name.startswith("tests.")
                or name.startswith("tools.research_benchmark")
                or name == "ab_journal"
                or name == "ab_harness_common"
                or name.endswith(".ab_journal")
            ]
            if bad:
                offenders.append(f"{path.relative_to(ROOT).as_posix()}: {sorted(bad)}")
        self.assertEqual(offenders, [])

    def test_domain_profiles_is_a_stdlib_data_leaf(self) -> None:
        # Profiles are data only: no planner, no I/O, no codey imports at all.
        path = ROOT / "codey" / "research" / "domain_profiles.py"
        imports = imported_modules(path)

        self.assertEqual(
            [name for name in imports if name == "codey" or name.startswith("codey.")],
            [],
        )
        source = path.read_text(encoding="utf-8")
        for token in (
            "write_text(",
            "write_json",
            "open(",
            "eval(",
            "exec(",
            "subprocess",
            "urllib",
            "importlib",
        ):
            self.assertNotIn(token, source)

    def test_source_trust_and_brief_projection_stay_projection_only(self) -> None:
        # Source trust classifies sources; brief projection structures handoff
        # refs. Neither may fetch, delete evidence, reach execution layers, or
        # import the knowledge store (which consumes their outputs instead).
        forbidden_imports = {
            "codey.automation.browser",
            "codey.providers.web_drivers.deepseek",
            "codey.providers.web_drivers.qwen",
            "codey.providers.web_drivers.stepfun",
            "codey.providers.web_drivers.glm",
            "codey.providers",
            "codey.providers.controls",
            "codey.toolchain.runtime",
            "codey.storage.managed_outputs",
            "codey.runtime.observe.events",
            "codey.app.server",
            "codey.operations.task_flow",
            "codey.ghost",
            "codey.reviews.core",
            "codey.knowledge",
            "codey.research.runner",
            "codey.research.tools",
            "codey.research.connector_search",
            "urllib",
            "subprocess",
            "importlib",
            "pkgutil",
        }
        forbidden_source = (
            "eval(",
            "exec(",
            "write_text(",
            "write_json",
            "requests.",
        )
        for name in ("source_trust.py", "brief_projection.py"):
            path = ROOT / "codey" / "research" / name
            with self.subTest(module=name):
                imports = imported_modules(path)
                self.assertTrue(
                    forbidden_imports.isdisjoint(imports),
                    sorted(forbidden_imports & imports),
                )
                source = path.read_text(encoding="utf-8")
                for token in forbidden_source:
                    self.assertNotIn(token, source)

    def test_knowledge_never_imports_the_research_package(self) -> None:
        # Knowledge is a lower layer than Research: its Writer handoff consumes
        # the neutral section parser (codey.reviews.report_sections) instead of
        # reaching upward into codey.research, whose package __init__ eagerly
        # loads the runner/browser/pipeline stack.
        for path in sorted((ROOT / "codey" / "knowledge").glob("*.py")):
            with self.subTest(path=path.relative_to(ROOT).as_posix()):
                imports = imported_modules(path)
                research_imports = {name for name in imports if name.startswith("codey.research")}
                self.assertEqual(sorted(research_imports), [])

    def test_knowledge_brief_import_does_not_load_research_runtime(self) -> None:
        # Import-level isolation, checked in a clean interpreter: importing
        # the brief must not transitively load any research runtime module.
        script = (
            "import sys\n"
            "import codey.knowledge.brief\n"
            "loaded = [name for name in sys.modules if name.startswith('codey.research')]\n"
            "assert loaded == [], loaded\n"
            "print('isolated')\n"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            cwd=ROOT,
            timeout=120,
        )
        self.assertEqual(
            completed.returncode,
            0,
            f"stdout={completed.stdout} stderr={completed.stderr}",
        )
        self.assertIn("isolated", completed.stdout)

    def test_report_sections_is_a_stdlib_leaf(self) -> None:
        # The shared section parser is domain-neutral: no codey imports and
        # no I/O, so both research and knowledge layers can own one parser.
        self._assert_stdlib_leaf(ROOT / "codey" / "reviews" / "report_sections.py")

    def test_citation_scanner_is_a_stdlib_leaf(self) -> None:
        # Citation scanning is shared by Research's done gate and Knowledge's
        # Writer handoff, so the scanner lives below both domains.
        self._assert_stdlib_leaf(ROOT / "codey" / "utils" / "citation_scanner.py")

    def _assert_stdlib_leaf(self, path: Path) -> None:
        imports = imported_modules(path)

        self.assertEqual(
            [name for name in imports if name == "codey" or name.startswith("codey.")],
            [],
        )
        source = path.read_text(encoding="utf-8")
        for token in (
            "write_text(",
            "write_json",
            "open(",
            "eval(",
            "exec(",
            "subprocess",
            "urllib",
        ):
            self.assertNotIn(token, source)

    def test_ab_journal_is_manual_layer_only(self) -> None:
        # The A/B journal is manual-experiment tooling: production layers must
        # not consume it, and it must not depend on production orchestration.
        journal_path = ROOT / "tests" / "manual" / "ab_journal.py"
        journal_imports = imported_modules(journal_path)
        self.assertTrue(
            {
                "codey.runs.trace",
                "codey.research.evidence_ledger",
                "codey.operations.task_flow",
                "codey.app.server",
            }.isdisjoint(journal_imports),
            sorted(journal_imports),
        )

        consumers = [
            ROOT / "codey" / "runs" / "trace.py",
            *(ROOT / "codey" / "research").glob("*.py"),
            TASK_ENTRY_PATH,
            TASK_RUN_PATH,
            ROOT / "codey" / "app" / "server.py",
        ]
        for path in consumers:
            with self.subTest(path=path.relative_to(ROOT).as_posix()):
                imports = imported_modules(path)
                self.assertNotIn("ab_journal", imports)
                self.assertNotIn("tests.manual.ab_journal", imports)

    def test_transcript_archive_cannot_become_evidence(self) -> None:
        # Transcript replay material stays in the manual layer; the evidence
        # ledger and object model must not know transcripts exist.
        for name in ("evidence_ledger.py", "object_model.py"):
            imports = imported_modules(ROOT / "codey" / "research" / name)
            with self.subTest(module=name):
                self.assertNotIn("ab_journal", imports)
                self.assertNotIn("tests.manual.ab_journal", imports)

    def test_research_topic_continuity_is_pure_projection_leaf(self) -> None:
        # Topic continuity (0.4.12) is a read model over bounded local facts.
        # It must stay a stdlib-only leaf: no Ghost runtime import, no
        # providers, no I/O, no networking, and no evidence vocabulary of
        # its own (GhostHint != Evidence).
        path = ROOT / "codey" / "research" / "topic_continuity.py"
        imports = imported_modules(path)
        source = path.read_text(encoding="utf-8")

        self.assertEqual(
            sorted(name for name in imports if name == "codey" or name.startswith("codey.")),
            [],
        )
        forbidden_imports = {
            "subprocess",
            "urllib",
            "requests",
            "socket",
            "importlib",
            "pkgutil",
        }
        self.assertTrue(forbidden_imports.isdisjoint(imports), sorted(imports))
        for token in (
            "write_text(",
            "write_json",
            "open(",
            "eval(",
            "exec(",
            "evidence_refs",
            "web_search",
            "provider.send",
        ):
            with self.subTest(token=token):
                self.assertNotIn(token, source)

    def test_research_stack_never_imports_ghost_runtime(self) -> None:
        # The Research pipeline consumes only the bounded continuity
        # projection handed to it; it must never reach into Ghost stores.
        for name in ("context.py", "pipeline.py", "runner.py", "topic_continuity.py"):
            imports = imported_modules(ROOT / "codey" / "research" / name)
            with self.subTest(module=name):
                ghost_imports = [item for item in imports if item == "codey.ghost" or item.startswith("codey.ghost.")]
                self.assertEqual(sorted(ghost_imports), [])

    def test_stamped_capability_ids_are_registered_boundaries(self) -> None:
        # Every capability_id literal stamped onto a prompt section or context
        # source in production code must name a documented boundary.
        registered = event_matrix_capability_ids()
        stamped: dict[str, set[str]] = {}

        for path in sorted((ROOT / "codey").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                for keyword in node.keywords:
                    if keyword.arg != "capability_id":
                        continue
                    value = keyword.value
                    if isinstance(value, ast.Constant) and isinstance(value.value, str) and value.value:
                        stamped.setdefault(path.relative_to(ROOT).as_posix(), set()).add(value.value)

        self.assertTrue(stamped, "expected capability_id stamps in production code")
        offenders = {path: sorted(ids - registered) for path, ids in stamped.items() if ids - registered}
        self.assertEqual(offenders, {})

    def test_refactor_has_no_test_only_compatibility_residue(self) -> None:
        agent_source = (ROOT / "codey" / "agents" / "runner.py").read_text(encoding="utf-8")
        research_source = (ROOT / "codey" / "research" / "runner.py").read_text(encoding="utf-8")
        task_run_source = TASK_RUN_PATH.read_text(encoding="utf-8")
        tool_source = (ROOT / "codey" / "toolchain" / "runtime.py").read_text(encoding="utf-8")

        self.assertNotIn("class StepResult", agent_source)
        self.assertNotIn("def trace_call", agent_source)
        self.assertNotIn("def _trace(", research_source)
        self.assertNotIn("def _trace_call", task_run_source)
        self.assertNotIn("compatibility ``tool_*``", tool_source)

    def test_agent_runner_is_only_the_public_entry_surface(self) -> None:
        source = (ROOT / "codey" / "agents" / "runner.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        function_names = {
            node.name for node in tree.body if isinstance(node, ast.FunctionDef)
        }
        class_names = {
            node.name for node in tree.body if isinstance(node, ast.ClassDef)
        }

        self.assertEqual(function_names, set())
        self.assertEqual(class_names, set())
        self.assertIn("from codey.agents.loop import", source)
        self.assertNotIn("def _read_before_edit_outcome", source)

    def test_run_operation_fact_source_is_runtime_only(self) -> None:
        self.assertFalse((ROOT / "codey" / "run_operation.py").exists())
        offenders: dict[str, list[str]] = {}
        forbidden = "codey." + "run_operation"
        for path in (ROOT / "codey").rglob("*.py"):
            imports = imported_modules(path)
            if forbidden in imports:
                offenders[str(path.relative_to(ROOT))] = sorted(imports)
        self.assertEqual(offenders, {})

    def test_completion_repair_context_is_pure_projection_leaf(self) -> None:
        # The repair context (0.4.13) consumes an already-evaluated proof
        # payload; it must never import the completion contract (one
        # completion semantic owner), the verification module, or any
        # runtime layer. It is a stdlib + redaction leaf, nothing else.
        path = ROOT / "codey" / "completion" / "repair_context.py"
        imports = imported_modules(path)
        source = path.read_text(encoding="utf-8")

        allowed_internal = {"codey.policies.redaction"}
        internal = sorted(name for name in imports if name == "codey" or name.startswith("codey."))
        self.assertTrue(set(internal) <= allowed_internal, sorted(internal))
        forbidden_imports = {
            "codey.completion.contract",
            "codey.completion.verification",
            "codey.operations.task_flow",
            "codey.agents.runner",
            "codey.providers",
            "codey.toolchain.runtime",
            "codey.ghost",
            "subprocess",
            "urllib",
            "socket",
            "importlib",
        }
        self.assertTrue(forbidden_imports.isdisjoint(imports), sorted(imports & forbidden_imports))
        for token in (
            "write_text(",
            "open(",
            "eval(",
            "exec(",
            "build_completion_contract",
            "project_completion_proof",
            "provider.send",
        ):
            with self.subTest(token=token):
                self.assertNotIn(token, source)

    def test_completion_repair_payload_vocabulary_is_closed(self) -> None:
        # The trace payload of a repair projection carries counts, classes,
        # reason codes and a digest only: there is no field that could hold
        # raw failure output, prompt text, or source bodies.
        from codey.completion.repair_context import project_repair_context

        projection = project_repair_context(
            proof={
                "status": "failed",
                "proof_id": "completion_proof:" + "a" * 16,
                "contract_id": "completion_contract:" + "b" * 16,
                "reason_codes": ["relevant_verification_failed"],
                "checks": [
                    {
                        "check_id": "relevant_verification",
                        "status": "fail",
                        "reason_code": "relevant_verification_failed",
                    }
                ],
                "raw_stdout": "SHOULD_NEVER_APPEAR",
            },
            failure_class="product_failure",
            decisive_checks=[
                {
                    "command": "pytest -q",
                    "cwd": ".",
                    "exit_code": 1,
                    "result_summary": "1 failed",
                }
            ],
        )
        payload = projection.to_payload()
        allowed_keys = {
            "schema_version",
            "kind",
            "context_source",
            "admitted",
            "failure_class",
            "detail",
            "check_count",
            "changed_file_count",
            "analysis_run_ref_count",
            "finding_ref_count",
            "summary_chars",
            "truncated",
            "reason_codes",
            "warnings",
            "digest",
            "proof_id",
            "contract_id",
            "refused_reason",
        }
        self.assertTrue(set(payload) <= allowed_keys, sorted(set(payload) - allowed_keys))
        self.assertNotIn("SHOULD_NEVER_APPEAR", json.dumps(payload))
        self.assertTrue(str(payload["digest"]).startswith("sha256:"))

    def test_no_repair_or_completion_managers_exist(self) -> None:
        # 0.4.13 closes the loop with pure projections and thin wiring; a
        # manager/planner/scheduler layer would reintroduce exactly the
        # runtime-that-thinks-for-the-model boundary Codey rejects.
        forbidden_names = (
            "RepairManager",
            "CompletionManager",
            "RepairRuntime",
            "RepairPlanner",
            "RepairCoordinator",
            "RepairPolicyEngine",
            "RepairScheduler",
            "MetaPlanner",
        )
        offenders: list[str] = []
        for path in sorted((ROOT / "codey").rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            found = [name for name in forbidden_names if name in source]
            if found:
                offenders.append(f"{path.relative_to(ROOT).as_posix()}: {found}")
        self.assertEqual(offenders, [])

    def test_completion_enforcement_has_explicit_stop_conditions(self) -> None:
        # The repair loop must be bounded by named stop conditions, never a
        # bare `while not complete`. Locks the v1 shape: one round max.
        completion_source = (ROOT / "codey" / "operations" / "project_completion_flow.py").read_text(encoding="utf-8")
        engine_source = (ROOT / "codey" / "completion" / "engine.py").read_text(encoding="utf-8")
        self.assertIn("MAX_COMPLETION_REPAIR_ROUNDS = 1", completion_source)
        self.assertNotIn("_COMPLETION_BLOCKED_NOTE", completion_source)
        self.assertIn("COMPLETION_BLOCKED_NOTES", engine_source)
        for reason in (
            "unobserved",
            "max_repair_rounds",
            "turn_budget_exhausted",
            "environment_failure",
            "provider_failure",
            "repair_context_unavailable",
            "repair_not_admitted",
        ):
            with self.subTest(reason=reason):
                self.assertIn(f'"{reason}"', engine_source)

    def test_completion_and_repair_surfaces_never_become_model_tools(self) -> None:
        # 0.4.13 boundary lock: proofs, evidence, and the repair context are
        # runtime-owned projections. Neither protocol contract may grow a
        # tool for them, and neither surface may rename tools across domains:
        # coding keeps its read/write vocabulary and research keeps its own.
        from codey.research.tool_contract import TOOL_CONTRACTS as RESEARCH_CONTRACTS
        from codey.toolchain.definition import TOOL_DEFINITIONS
        from codey.toolchain.tool_prompt import render_coding_tool_contract_text as render_tool_contract

        self.assertEqual(
            {spec.name for spec in TOOL_DEFINITIONS},
            {
                "list_dir",
                "read_file",
                "read_files",
                "grep",
                "find_references",
                "parallel",
                "edit",
                "run",
                "shell",
                "done",
            },
        )
        self.assertEqual(
            set(RESEARCH_CONTRACTS),
            {
                "web_search",
                "open_url",
                "source_search",
                "knowledge_search",
                "knowledge_read",
                "knowledge_write",
                "knowledge_link",
                "done",
            },
        )
        rendered = render_tool_contract()
        forbidden_tokens = (
            "completion_proof",
            "completion_contract",
            "completion_repair_context",
            "repair_context",
            "evidence_ledger",
            "web_search",
            "open_url",
            "knowledge_write",
            "source_search",
        )
        for token in forbidden_tokens:
            with self.subTest(token=token):
                self.assertNotIn(token, rendered)

    def test_repair_context_is_rejected_as_an_unknown_model_tool(self) -> None:
        # Behavioral side of the same boundary: calling the repair context
        # through either codec is a typed unknown-tool error, not a tool.
        from codey.protocols.json_codec import JsonToolCodec
        from codey.research.protocols import JsonToolCodec as ResearchCodec
        from codey.research.tool_contract import PROTOCOL_UNKNOWN_TOOL

        payload = json.dumps(
            {
                "tool": "completion_repair_context",
                "args": {"failure": "product"},
            }
        )
        coding_plan = JsonToolCodec().parse(payload)
        self.assertEqual(coding_plan.calls, [])
        self.assertIsNone(coding_plan.control)
        self.assertIn("unknown tool: completion_repair_context", coding_plan.protocol_error)

        research_plan = ResearchCodec().parse(payload)
        self.assertEqual(research_plan.calls, [])
        self.assertIsNone(research_plan.control)
        self.assertEqual(research_plan.protocol_error_kind, PROTOCOL_UNKNOWN_TOOL)

    def test_tool_args_repair_module_is_pure_and_has_no_codey_imports(self) -> None:
        path = ROOT / "codey" / "toolchain" / "tool_args_repair.py"
        modules = imported_modules(path)
        forbidden = [name for name in modules if name.startswith("codey")]
        self.assertEqual(
            forbidden,
            [],
            f"codey/toolchain/tool_args_repair.py must be pure and have no codey imports: {forbidden}",
        )

    def test_toolchain_leaves_no_orphan_tool_modules_at_package_root(self) -> None:
        # tool_args_repair / tool_prompt live in toolchain/ next to the tool
        # definitions they serve; the package root keeps no tool files.
        orphans = [
            path.name
            for path in sorted((ROOT / "codey").glob("tool_*.py"))
        ]
        self.assertEqual(orphans, [])

    def test_tool_definitions_do_not_contain_write_file_or_create_file(self) -> None:
        from codey.toolchain.definition import TOOL_DEFINITION_BY_NAME, TOOL_DEFINITIONS

        all_names = {name for spec in TOOL_DEFINITIONS for name in (spec.name, *spec.aliases)}
        self.assertNotIn("write_file", all_names)
        self.assertNotIn("create_file", all_names)
        self.assertNotIn("write", all_names)
        self.assertNotIn("write_file", TOOL_DEFINITION_BY_NAME)
        self.assertNotIn("create_file", TOOL_DEFINITION_BY_NAME)

    def test_json_codec_keeps_one_tool_plan_parse_path(self) -> None:
        source = (ROOT / "codey" / "protocols" / "json_codec.py").read_text(encoding="utf-8")

        self.assertNotIn("def _parse_object(", source)
        self.assertNotIn("def _text(", source)


    def test_run_trace_only_consumes_research_projection_leaves(self) -> None:
        # Run Trace is audit infra: it may validate/normalize research refs,
        # but only through projection-only leaves. A behavior import
        # (runner/tools/pipeline/contracts) would drag execution into audit.
        path = ROOT / "codey" / "runs" / "trace.py"
        imports = imported_modules(path)
        research_imports = sorted(
            name for name in imports if name == "codey.research" or name.startswith("codey.research.")
        )
        allowed = {
            "codey.research.artifact_lineage",
            "codey.research.evidence_runtime",
            "codey.research.guards",
            "codey.research.review_finding",
            "codey.research.source_trust",
        }
        self.assertTrue(set(research_imports) <= allowed, sorted(set(research_imports) - allowed))


    def test_runtime_package_has_no_import_cycles(self) -> None:
        # The runtime kernel is a DAG: session_log -> {projection, compaction}
        # -> leaves, effects -> {operation_state, session_log}, write on top.
        # A cycle here would let audit, execution, and projection silently
        # depend on each other. Tarjan over all intra-package imports,
        # including function-local ones.
        pkg = ROOT / "codey" / "runtime"
        modules: dict[str, set[str]] = {}
        for path in sorted(pkg.rglob("*.py")):
            name = "codey.runtime." + path.relative_to(pkg).with_suffix("").as_posix().replace("/", ".")
            if name.endswith(".__init__"):
                name = name[: -len(".__init__")]
            names: set[str] = set()
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    mod = node.module
                    if mod == "codey.runtime" or mod.startswith("codey.runtime."):
                        names.add(mod)
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name == "codey.runtime" or alias.name.startswith("codey.runtime."):
                            names.add(alias.name)
            modules[name] = names
        index: dict[str, int] = {}
        low: dict[str, int] = {}
        on_stack: set[str] = set()
        stack: list[str] = []
        counter = [0]
        cycles: list[list[str]] = []

        def strongconnect(node: str) -> None:
            index[node] = low[node] = counter[0]
            counter[0] += 1
            stack.append(node)
            on_stack.add(node)
            for dep in sorted(modules.get(node, ())):
                if dep not in modules:
                    continue
                if dep not in index:
                    strongconnect(dep)
                    low[node] = min(low[node], low[dep])
                elif dep in on_stack:
                    low[node] = min(low[node], index[dep])
            if low[node] == index[node]:
                component = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                if len(component) > 1:
                    cycles.append(sorted(component))

        for module in sorted(modules):
            if module not in index:
                strongconnect(module)
        self.assertEqual(cycles, [])

    def test_runtime_log_entry_types_are_not_reexported_from_session_log(self) -> None:
        # Cold-start API shape: entries.py owns the entry/error model.
        # session_log.py is only the durable store boundary, not a compat facade.
        import codey.runtime.log.session_log as session_log

        self.assertEqual(getattr(session_log, "__all__", ()), ["RuntimeSessionLog"])
        for name in (
            "RuntimeLogEntry",
            "RuntimeLogError",
            "RuntimeLogCorruption",
            "RuntimeLogWriteError",
        ):
            with self.subTest(name=name):
                self.assertFalse(hasattr(session_log, name))

    def test_local_openai_runtime_has_no_config_or_discovery_facades(self) -> None:
        # Local model has one owner per concern: local_openai is the provider
        # runtime, local_config owns persistence, local_discovery owns probes.
        import codey.providers.local_openai as local_openai

        for name in (
            "LocalEndpoint",
            "default_local_base_url",
            "detect_local_endpoints",
            "load_local_config",
            "local_config_payload",
            "local_endpoint_available",
            "local_native_tools_enabled",
            "probe_local_endpoint",
            "probe_local_endpoint_detail",
            "resolve_local_context_budgets",
            "resolve_local_endpoint",
            "save_local_config",
        ):
            with self.subTest(name=name):
                self.assertFalse(hasattr(local_openai, name))

    def test_runtime_core_has_no_unconsumed_contract_stubs(self) -> None:
        import codey.runtime.core.operation as operation
        import codey.runtime.core.outcome as outcome
        import codey.runtime.write.file_mutation_queue as queue

        for module, names in (
            (operation, ("Operation",)),
            (outcome, ("CompletionVerdict", "TaskCompletionStatus")),
            (queue, ("key_for_call",)),
        ):
            for name in names:
                with self.subTest(module=module.__name__, name=name):
                    self.assertFalse(hasattr(module, name))

    def test_confirmed_dead_convenience_exports_do_not_exist(self) -> None:
        import codey.agents.runaway_guard as runaway_guard
        import codey.ghost._common as ghost_common
        import codey.ghost.affinity as ghost_affinity
        import codey.ghost.learning_loop as ghost_learning
        import codey.operations.ghost_context as ghost_context
        import codey.operations.research_flow as research_flow
        import codey.providers.base as provider_base
        import codey.providers.error_classification as provider_errors
        import codey.providers.web_drivers.common as web_common
        import codey.toolchain.openai_tools as openai_tools

        for module, names in (
            (research_flow, ("record_research_result_trace", "record_evidence_ledger_write_trace")),
            (ghost_context, ("ghost_directive_text", "ghost_continuity_text")),
            (runaway_guard, ("repeated_failure_key",)),
            (provider_base, ("StructuredChatProvider",)),
            (provider_errors, ("is_auth_message",)),
            (web_common, ("last_response_text", "clean_whitespace")),
            (openai_tools, ("research_openai_tools",)),
            (ghost_affinity, ("apply_affinity_research_boost",)),
            (ghost_common, ("normalize_scope",)),
            (ghost_learning, ("ClosableSignalProvider",)),
        ):
            exported = set(getattr(module, "__all__", ()))
            for name in names:
                with self.subTest(module=module.__name__, name=name):
                    self.assertFalse(hasattr(module, name))
                    self.assertNotIn(name, exported)

    def test_retired_compatibility_shims_do_not_exist(self) -> None:
        self.assertFalse((ROOT / "codey" / "workspace" / "task_context.py").exists())
        self.assertFalse((ROOT / "codey" / "reviews" / "scan_report.py").exists())
        # services.py was split into review/consensus/shell_service (+ provider
        # warmup into provider_services): no forwarder facade may come back.
        self.assertFalse((ROOT / "codey" / "app" / "services.py").exists())
        offenders: list[str] = []
        for path in sorted((ROOT / "codey").rglob("*.py")):
            imported = imported_modules(path)
            if "codey.workspace.task_context" in imported or "codey.reviews.scan_report" in imported:
                offenders.append(path.relative_to(ROOT).as_posix())
        self.assertEqual(offenders, [])
        service_offenders: list[str] = []
        for path in sorted((ROOT / "codey").rglob("*.py")):
            imported = imported_modules(path)
            if "codey.app.services" in imported:
                service_offenders.append(path.relative_to(ROOT).as_posix())
        self.assertEqual(service_offenders, [])

    def test_mypy_baseline_config_exists_but_does_not_gate(self) -> None:
        # mypy is baseline-only (493 errors / 106 files as of 2026-09-21):
        # config must exist so counts are reproducible, CI stays ruff+pytest.
        import tomllib

        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        mypy = pyproject.get("tool", {}).get("mypy", {})
        self.assertEqual(mypy.get("python_version"), "3.11")
        self.assertTrue(mypy.get("ignore_missing_imports"))

    def test_adapter_repair_surface_is_closed_and_has_no_forwarders(self) -> None:
        # Security boundary: repair may only touch one provider driver plus
        # the shared web files. New files need an explicit test update, and
        # pure forwarder helpers must not come back.
        from codey.repairs.adapter_surface import (
            PROVIDER_DRIVER_FILES,
            SHARED_WEB_ADAPTER_FILES,
            adapter_repair_surface,
        )

        self.assertEqual(
            set(PROVIDER_DRIVER_FILES),
            {"deepseek", "qwen", "mimo", "stepfun", "glm"},
        )
        self.assertEqual(
            tuple(SHARED_WEB_ADAPTER_FILES),
            (
                "codey/providers/web_provider.py",
                "codey/providers/web_driver.py",
                "codey/providers/web_drivers/common.py",
                "codey/providers/profiles.py",
                "codey/providers/profiles.json",
                "codey/providers/controls.py",
                "codey/providers/flow.py",
                "codey/providers/send_loop.py",
                "codey/providers/submission.py",
                "codey/providers/timeouts.py",
                "codey/automation/web_clipboard.py",
                "codey/automation/browser.py",
            ),
        )
        self.assertEqual(adapter_repair_surface("unknown-provider"), ())
        self.assertEqual(
            adapter_repair_surface("deepseek"),
            (*PROVIDER_DRIVER_FILES["deepseek"], *SHARED_WEB_ADAPTER_FILES),
        )
        surface_source = (ROOT / "codey" / "repairs" / "adapter_surface.py").read_text(encoding="utf-8")
        self.assertNotIn("def driver_files(", surface_source)
        policy_source = (ROOT / "codey" / "repairs" / "policy.py").read_text(encoding="utf-8")
        self.assertNotIn("def allowed_adapter_files(", policy_source)

    def test_provider_single_entry_and_server_stays_http_only(self) -> None:
        # Single entry: api/context resolve providers via provider_services.
        # server.py keeps HTTP/SSE routing + STATE/boot; TaskRunDeps assembly
        # lives in task_submit.py, task execution in operations/.
        api_source = (ROOT / "codey" / "app" / "api.py").read_text(encoding="utf-8")
        self.assertIn("from codey.app import provider_services", api_source)
        self.assertIn("provider_services.provider_availability(ctx)", api_source)
        self.assertIn("provider_services.provider_payload(", api_source)
        self.assertIn("provider_services.provider_catalog()", api_source)
        self.assertNotIn("statuses = services.provider_availability(ctx)", api_source)
        self.assertNotIn('"providers": services.provider_payload(', api_source)
        self.assertNotIn('"providers": services.provider_catalog(', api_source)
        context_source = (ROOT / "codey" / "app" / "context.py").read_text(encoding="utf-8")
        self.assertNotIn("def provider_tab_availability(", context_source)
        self.assertNotIn("def connect_provider(", context_source)
        server_source = (ROOT / "codey" / "app" / "server.py").read_text(encoding="utf-8")
        self.assertIn("from codey.app import task_submit", server_source)
        self.assertNotIn("TaskRunDeps(", server_source)
        self.assertNotIn("TaskSubmission(", server_source)
        self.assertNotIn("from codey.agents.runner import", server_source)
        self.assertNotIn("from codey.operations.task_entry import", server_source)
        submit_source = (ROOT / "codey" / "app" / "task_submit.py").read_text(encoding="utf-8")
        self.assertIn("TaskRunDeps(", submit_source)
        self.assertIn("run_task_submission(", submit_source)

    def test_long_files_do_not_grow(self) -> None:
        # 1000-line guardrail (warning-grade): the files above the line are
        # known cohesive stores/flows. They must shrink over time; this test
        # fails only when a NEW file crosses the line or a tracked file
        # grows, so refactors stay honest without a hard fail on legacy.
        over_limit: dict[str, int] = {}
        for path in sorted((ROOT / "codey").rglob("*.py")):
            size = sum(1 for _ in path.open(encoding="utf-8"))
            if size > 1000:
                over_limit[path.relative_to(ROOT / "codey").as_posix()] = size
        baseline = {
            "agents/consensus.py",
            "ghost/affinity.py",
            "ghost/continuity.py",
            "ghost/hebbian.py",
            "ghost/inbox.py",
            "ghost/router.py",
            "ghost/work_queue.py",
            "operations/project_completion_flow.py",
            "providers/controls.py",
            "research/browser_search.py",
            "research/evidence_ledger.py",
            "research/runner.py",
            "research/source_connectors.py",
            "runs/trace.py",
            "runtime/core/operation_state.py",
            "toolchain/runtime.py",
            "workspace/changes.py",
        }
        self.assertEqual(set(over_limit) - baseline, set())
        # Reverse direction: a baseline entry that shrank back under the
        # line must leave the baseline, or regrowth up to the old ceiling
        # would never alarm.
        self.assertEqual(baseline - set(over_limit), set())
        ceiling = {
            "agents/consensus.py": 1100,
            "ghost/affinity.py": 2750,
            "ghost/continuity.py": 1350,
            "ghost/hebbian.py": 1300,
            "ghost/inbox.py": 1200,
            "ghost/router.py": 1180,
            "ghost/work_queue.py": 2750,
            "operations/project_completion_flow.py": 1750,
            "providers/controls.py": 1400,
            "research/browser_search.py": 1230,
            "research/evidence_ledger.py": 1550,
            # Boundary hardening: named done-review result + multiline build
            # (advisor_count preserved), loop.py -25 lines in the same round.
            "research/runner.py": 1400,
            "research/source_connectors.py": 1420,
            "runs/trace.py": 2450,
            "runtime/core/operation_state.py": 1110,
            # LF/CRLF line-boundary note (was a one-line overclaim).
            "toolchain/runtime.py": 1310,
            # Read-only require_baseline + stat-size capacity guard.
            "workspace/changes.py": 1180,
        }
        grown = {
            name: size
            for name, size in over_limit.items()
            if size > ceiling.get(name, 1000)
        }
        self.assertEqual(grown, {})


if __name__ == "__main__":
    unittest.main()
