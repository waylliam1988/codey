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
        self.assertNotIn("codey.mimo", imports)
        self.assertIn("codey.providers", imports)
        self.assertIn("codey.protocols", imports)

    def test_orchestrators_create_providers_instead_of_browser_sessions(self) -> None:
        for name in ("cli.py", "server.py", "task_runner.py"):
            with self.subTest(name=name):
                imports = imported_modules(ROOT / "codey" / name)
                self.assertNotIn("codey.browser", imports)
                self.assertNotIn("codey.deepseek", imports)
                self.assertNotIn("codey.qwen", imports)
                self.assertNotIn("codey.mimo", imports)

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

    def test_refactor_has_no_test_only_compatibility_residue(self) -> None:
        agent_source = (ROOT / "codey" / "agent.py").read_text(encoding="utf-8")
        tool_source = (ROOT / "codey" / "tool_runtime.py").read_text(encoding="utf-8")

        self.assertNotIn("class StepResult", agent_source)
        self.assertNotIn("compatibility ``tool_*``", tool_source)


if __name__ == "__main__":
    unittest.main()
