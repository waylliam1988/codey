from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


class ArchitectureBoundaryTests(unittest.TestCase):
    def test_agent_runtime_has_no_browser_or_deepseek_dependency(self) -> None:
        imports = imported_modules(ROOT / "codey" / "agent.py")

        self.assertNotIn("playwright.sync_api", imports)
        self.assertNotIn("codey.browser", imports)
        self.assertNotIn("codey.deepseek", imports)
        self.assertNotIn("codey.qwen", imports)
        self.assertNotIn("codey.stepfun", imports)
        self.assertNotIn("codey.glm", imports)
        self.assertIn("codey.providers", imports)
        self.assertIn("codey.protocols", imports)

    def test_orchestrators_create_providers_instead_of_browser_sessions(self) -> None:
        for name in ("cli.py", "server.py", "task_runner.py"):
            with self.subTest(name=name):
                imports = imported_modules(ROOT / "codey" / name)
                self.assertNotIn("codey.browser", imports)
                self.assertNotIn("codey.deepseek", imports)
                self.assertNotIn("codey.qwen", imports)
                self.assertNotIn("codey.stepfun", imports)
                self.assertNotIn("codey.glm", imports)

    def test_http_server_delegates_task_orchestration(self) -> None:
        imports = imported_modules(ROOT / "codey" / "server.py")
        source = (ROOT / "codey" / "server.py").read_text(encoding="utf-8")

        self.assertIn("codey.task_runner", imports)
        self.assertNotIn("on_shell_request(cwd_rel", source)
        self.assertNotIn("conversation.prepare_model_handoff", source)

    def test_task_runner_has_no_http_dependency(self) -> None:
        imports = imported_modules(ROOT / "codey" / "task_runner.py")

        self.assertNotIn("http.server", imports)
        self.assertNotIn("codey.server", imports)

    def test_ghost_runtime_has_no_provider_browser_tool_or_research_dependency(self) -> None:
        forbidden = {
            "torch",
            "transformers",
            "codey.browser",
            "codey.providers",
            "codey.tool_runtime",
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
            "codey.browser",
            "codey.providers",
            "codey.provider_controls",
            "codey.tool_runtime",
            "codey.research.runner",
            "codey.research.tools",
        }
        self.assertTrue(
            forbidden_affinity.isdisjoint(affinity_imports),
            sorted(forbidden_affinity & affinity_imports),
        )

        research_imports = imported_modules(ROOT / "codey" / "research" / "runner.py")
        permission_imports = imported_modules(ROOT / "codey" / "permission_profiles.py")
        repair_source = (ROOT / "codey" / "adapter_repair.py").read_text(encoding="utf-8")
        tool_runtime_imports = imported_modules(ROOT / "codey" / "tool_runtime.py")

        self.assertNotIn("codey.ghost.affinity", research_imports)
        self.assertNotIn("codey.ghost.affinity", permission_imports)
        self.assertNotIn("affinity", repair_source.casefold())
        self.assertNotIn("codey.ghost", tool_runtime_imports)

    def test_prompt_envelope_is_not_a_provider_or_tool_runtime_seam(self) -> None:
        imports = imported_modules(ROOT / "codey" / "prompt_envelope.py")
        forbidden = {
            "codey.browser",
            "codey.deepseek",
            "codey.qwen",
            "codey.stepfun",
            "codey.glm",
            "codey.providers",
            "codey.provider_controls",
            "codey.tool_runtime",
            "codey.research.runner",
            "codey.ghost",
        }

        self.assertTrue(forbidden.isdisjoint(imports), sorted(forbidden & imports))

    def test_capability_registry_is_metadata_only(self) -> None:
        path = ROOT / "codey" / "capabilities.py"
        imports = imported_modules(path)
        source = path.read_text(encoding="utf-8")
        forbidden_imports = {
            "codey.browser",
            "codey.deepseek",
            "codey.qwen",
            "codey.stepfun",
            "codey.glm",
            "codey.providers",
            "codey.provider_controls",
            "codey.tool_runtime",
            "codey.research.runner",
            "codey.server",
            "codey.task_runner",
            "importlib",
            "pkgutil",
        }
        forbidden_source = (
            "entry_points",
            "load_plugin",
            "register_runtime",
            "dispatch(",
            "execute(",
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

    def test_task_runner_does_not_use_capability_registry_for_decisions(self) -> None:
        source = (ROOT / "codey" / "task_runner.py").read_text(encoding="utf-8")

        self.assertIn("self.capabilities = capabilities", source)
        self.assertEqual(source.count("self.capabilities"), 1)
        self.assertNotIn("if capabilities", source)
        self.assertNotIn("if self.capabilities", source)

    def test_builtin_profiles_are_metadata_only(self) -> None:
        path = ROOT / "codey" / "builtin_profiles.py"
        imports = imported_modules(path)
        source = path.read_text(encoding="utf-8")
        forbidden_imports = {
            "codey.browser",
            "codey.deepseek",
            "codey.qwen",
            "codey.stepfun",
            "codey.glm",
            "codey.providers",
            "codey.provider_controls",
            "codey.tool_runtime",
            "codey.research.runner",
            "codey.server",
            "codey.task_runner",
            "importlib",
            "pkgutil",
        }
        forbidden_source = (
            "entry_points",
            "load_plugin",
            "register_runtime",
            "dispatch(",
            "execute(",
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

    def test_task_runner_does_not_use_builtin_profiles_for_decisions(self) -> None:
        source = (ROOT / "codey" / "task_runner.py").read_text(encoding="utf-8")

        self.assertIn("self.builtin_profiles = builtin_profiles", source)
        self.assertEqual(source.count("self.builtin_profiles"), 1)
        self.assertNotIn("if builtin_profiles", source)
        self.assertNotIn("if self.builtin_profiles", source)

    def test_action_policy_is_not_runtime_or_plugin_host(self) -> None:
        path = ROOT / "codey" / "action_policy.py"
        imports = imported_modules(path)
        source = path.read_text(encoding="utf-8")
        forbidden_imports = {
            "codey.browser",
            "codey.deepseek",
            "codey.qwen",
            "codey.stepfun",
            "codey.glm",
            "codey.providers",
            "codey.provider_controls",
            "codey.tool_runtime",
            "codey.server",
            "codey.task_runner",
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
        )

        self.assertTrue(
            forbidden_imports.isdisjoint(imports),
            sorted(forbidden_imports & imports),
        )
        for token in forbidden_source:
            with self.subTest(token=token):
                self.assertNotIn(token, source)

    def test_run_details_is_read_only_projection_not_runtime(self) -> None:
        path = ROOT / "codey" / "run_details.py"
        imports = imported_modules(path)
        source = path.read_text(encoding="utf-8")
        forbidden_imports = {
            "codey.browser",
            "codey.deepseek",
            "codey.qwen",
            "codey.stepfun",
            "codey.glm",
            "codey.providers",
            "codey.provider_controls",
            "codey.tool_runtime",
            "codey.server",
            "codey.task_runner",
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

    def test_research_object_model_is_projection_not_runtime(self) -> None:
        path = ROOT / "codey" / "research" / "object_model.py"
        imports = imported_modules(path)
        source = path.read_text(encoding="utf-8")
        forbidden_imports = {
            "codey.browser",
            "codey.deepseek",
            "codey.qwen",
            "codey.stepfun",
            "codey.glm",
            "codey.providers",
            "codey.provider_controls",
            "codey.tool_runtime",
            "codey.server",
            "codey.task_runner",
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

    def test_research_identity_and_evidence_ledger_do_not_import_runtime_layers(self) -> None:
        forbidden_imports = {
            "codey.browser",
            "codey.deepseek",
            "codey.qwen",
            "codey.stepfun",
            "codey.glm",
            "codey.providers",
            "codey.provider_controls",
            "codey.tool_runtime",
            "codey.server",
            "codey.task_runner",
            "codey.ghost",
            "importlib",
            "pkgutil",
            "subprocess",
        }
        for path in (
            ROOT / "codey" / "research" / "identity.py",
            ROOT / "codey" / "research" / "evidence_ledger.py",
        ):
            with self.subTest(path=path.relative_to(ROOT).as_posix()):
                imports = imported_modules(path)
                self.assertTrue(
                    forbidden_imports.isdisjoint(imports),
                    sorted(forbidden_imports & imports),
                )

    def test_research_review_and_local_context_do_not_import_tool_runtime(self) -> None:
        paths = [
            *(ROOT / "codey" / "research").glob("*.py"),
            ROOT / "codey" / "review.py",
            ROOT / "codey" / "review_coordinator.py",
            *(ROOT / "codey" / "ghost").glob("*.py"),
        ]
        for path in paths:
            with self.subTest(path=path.relative_to(ROOT).as_posix()):
                self.assertNotIn("codey.tool_runtime", imported_modules(path))

    def test_refactor_has_no_test_only_compatibility_residue(self) -> None:
        agent_source = (ROOT / "codey" / "agent.py").read_text(encoding="utf-8")
        research_source = (ROOT / "codey" / "research" / "runner.py").read_text(encoding="utf-8")
        task_runner_source = (ROOT / "codey" / "task_runner.py").read_text(encoding="utf-8")
        tool_source = (ROOT / "codey" / "tool_runtime.py").read_text(encoding="utf-8")

        self.assertNotIn("class StepResult", agent_source)
        self.assertNotIn("def trace_call", agent_source)
        self.assertNotIn("def _trace(", research_source)
        self.assertNotIn("def _trace_call", task_runner_source)
        self.assertNotIn("compatibility ``tool_*``", tool_source)


if __name__ == "__main__":
    unittest.main()
