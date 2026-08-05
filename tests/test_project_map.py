from __future__ import annotations

import json
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from codey import project_map


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).strip() + "\n", encoding="utf-8")


def _build_deep_focus_fixture(root: Path) -> None:
    for index in range(project_map.MAX_SYMBOL_FILES + 15):
        _write(
            root / "apps" / "admin" / "src" / "generated" / f"admin_report_{index:03d}.py",
            f"""
            class AdminReport{index:03d}:
                def render_overview(self, user, flags):
                    return "admin-report-{index:03d}"

            def build_admin_report_{index:03d}(config):
                return AdminReport{index:03d}()
            """,
        )
    _write(
        root
        / "apps"
        / "commerce"
        / "src"
        / "domain"
        / "billing"
        / "policies"
        / "proration_policy.py",
        """
        class SubscriptionProrationPolicy:
            def calculate_unused_credit(self, previous_plan, new_plan, period):
                return "BODY_SHOULD_NOT_APPEAR"

            def build_invoice_adjustment(self, credit, upgrade_delta):
                return {"credit": credit, "delta": upgrade_delta}
        """,
    )
    _write(
        root
        / "apps"
        / "commerce"
        / "src"
        / "domain"
        / "billing"
        / "invoices"
        / "adjustment_builder.py",
        """
        def create_invoice_adjustment(subscription_id, credit, delta):
            return {"subscription_id": subscription_id, "unused_credit": credit}
        """,
    )
    _write(
        root
        / "apps"
        / "commerce"
        / "tests"
        / "billing"
        / "test_proration_policy.py",
        """
        def test_subscription_upgrade_unused_credit_creates_adjustment():
            assert True
        """,
    )


class ProjectMapTests(unittest.TestCase):
    def test_directory_entry_scan_stops_after_remaining_budget_plus_probe(self) -> None:
        class FakeEntry:
            def __init__(self, name: str) -> None:
                self.name = name

            def is_dir(self) -> bool:
                return False

        class FakeDirectory:
            def __init__(self) -> None:
                self.seen = 0

            def iterdir(self):
                for index in range(100):
                    self.seen += 1
                    if self.seen > 4:
                        raise AssertionError("iterator consumed past remaining + 1")
                    yield FakeEntry(f"file_{index}.py")

        directory = FakeDirectory()

        entries, truncated = project_map._bounded_directory_entries(directory, 3)

        self.assertTrue(truncated)
        self.assertEqual(directory.seen, 4)
        self.assertEqual(len(entries), 3)

    def test_node_project_lists_manifest_and_roots_without_candidate_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "src").mkdir()
            (root / "src" / "app.ts").write_text("export const value = 1;\n", encoding="utf-8")
            (root / "tests").mkdir()
            (root / "tests" / "app.test.ts").write_text("test('ok', () => {})\n", encoding="utf-8")
            (root / "README.md").write_text("# demo\n", encoding="utf-8")
            (root / "package.json").write_text(
                json.dumps({
                    "scripts": {
                        "test": "vitest",
                        "lint": "eslint .",
                        "build": "vite build",
                    }
                }),
                encoding="utf-8",
            )

            rendered = project_map.render_project_map(root)

        self.assertIn("Project Map", rendered)
        self.assertIn("src/", rendered)
        self.assertIn("tests/", rendered)
        self.assertIn("package.json", rendered)
        self.assertIn("README.md", rendered)
        self.assertNotIn("npm test", rendered)
        self.assertNotIn("npm run lint", rendered)
        self.assertNotIn("npm run build", rendered)
        self.assertNotIn("Candidate commands (inspect before running)", rendered)

    def test_explicit_candidate_commands_include_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "web").mkdir()
            (root / "web" / "package.json").write_text(
                json.dumps({"scripts": {"test": "vitest", "build": "vite build"}}),
                encoding="utf-8",
            )

            rendered = project_map.render_project_map(
                root,
                candidate_commands=("web/: npm test", "web/: npm run build"),
            )

        self.assertIn("web/package.json", rendered)
        self.assertIn("web/: npm test", rendered)
        self.assertIn("web/: npm run build", rendered)
        self.assertNotIn("\n- npm test\n", rendered)

    def test_source_and_test_roots_are_rendered_once(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "src").mkdir()
            (root / "tests").mkdir()

            rendered = project_map.render_project_map(root)

        self.assertEqual(rendered.count("- src/"), 1)
        self.assertEqual(rendered.count("- tests/"), 1)

    def test_python_project_lists_pyproject_and_verified_checks_without_candidate_fallback(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app").mkdir()
            (root / "app" / "__init__.py").write_text("", encoding="utf-8")
            (root / "pyproject.toml").write_text(
                "[tool.pytest.ini_options]\npythonpath=['.']\n[tool.ruff]\nline-length=100\n",
                encoding="utf-8",
            )
            facts = "- successful check: python -m unittest\n- successful run: python app.py"

            rendered = project_map.render_project_map(root, facts)

        self.assertIn("pyproject.toml", rendered)
        self.assertNotIn("- python -m pytest", rendered)
        self.assertNotIn("- python -m ruff check .", rendered)
        self.assertIn("Observed successful checks", rendered)
        self.assertIn("successful check: python -m unittest", rendered)
        self.assertNotIn("successful run: python app.py", rendered)

    def test_empty_project_renders_minimal_map_without_fake_commands(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            rendered = project_map.render_project_map(td)

        self.assertEqual(rendered, "Project Map (bounded local scan; relative paths only):")
        self.assertNotIn("Candidate commands", rendered)
        self.assertNotIn("Symbol overview", rendered)

    def test_symbol_overview_is_task_aware_navigation_only(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "codey").mkdir()
            (root / "tests").mkdir()
            (root / "codey" / "json_codec.py").write_text(
                "TOOL_SPECS = {}\n\n"
                "class JsonToolCodec:\n"
                "    def parse_tool_calls(self, payload, context):\n"
                "        return 'BODY_SHOULD_NOT_APPEAR'\n\n"
                "def render_tool_results(results):\n"
                "    return []\n",
                encoding="utf-8",
            )
            (root / "codey" / "unrelated.py").write_text(
                "def paint_screen(canvas):\n"
                "    return canvas\n",
                encoding="utf-8",
            )
            (root / "tests" / "test_json_codec.py").write_text(
                "def test_parse_tool_calls():\n"
                "    assert True\n",
                encoding="utf-8",
            )

            rendered = project_map.render_project_map(
                root,
                task="change JSON tool call parsing and verification tests",
            )

        self.assertIn("Symbol overview", rendered)
        self.assertIn("read files before editing", rendered)
        self.assertIn("codey/json_codec.py", rendered)
        self.assertIn("class JsonToolCodec", rendered)
        self.assertIn("def JsonToolCodec.parse_tool_calls(self, payload, context)", rendered)
        self.assertIn("tests/test_json_codec.py", rendered)
        self.assertLess(
            rendered.index("codey/json_codec.py"),
            rendered.index("codey/unrelated.py"),
        )
        self.assertNotIn("BODY_SHOULD_NOT_APPEAR", rendered)
        self.assertNotIn("return []", rendered)

    def test_focused_subtree_finds_deep_target_past_symbol_file_budget(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _build_deep_focus_fixture(root)
            task = (
                "Find where subscription upgrades calculate unused credit and "
                "create invoice adjustments in the billing flow. Include likely "
                "focused tests."
            )

            symbol_overview = project_map.build_symbol_overview(root, task)
            rendered = project_map.render_project_map(root, task=task)

        self.assertNotIn(
            "apps/commerce/src/domain/billing/policies/proration_policy.py",
            symbol_overview,
        )
        self.assertIn("Focused subtree", rendered)
        self.assertNotIn("Symbol overview", rendered)
        self.assertIn("apps/commerce/", rendered)
        self.assertIn("apps/commerce/src/domain/billing/policies/proration_policy.py", rendered)
        self.assertIn("apps/commerce/src/domain/billing/invoices/adjustment_builder.py", rendered)
        self.assertIn("apps/commerce/tests/billing/test_proration_policy.py", rendered)
        self.assertIn("class SubscriptionProrationPolicy", rendered)
        self.assertNotIn("BODY_SHOULD_NOT_APPEAR", rendered)
        self.assertLessEqual(len(rendered), project_map.MAX_PROJECT_MAP_CHARS + 80)

    def test_focused_subtree_survives_near_project_map_character_cap(self) -> None:
        target_path = "apps/commerce/src/domain/billing/policies/proration_policy.py"
        focused = "\n".join(
            [
                "Focused subtree (task-scored navigation; read files before editing):",
                "- apps/commerce/",
                f"  - {target_path} [source]: class SubscriptionProrationPolicy",
            ]
        )
        symbol = "\n".join(
            [
                "Symbol overview (bounded navigation hints only; read files before editing):",
                *(
                    f"- apps/admin/src/generated/filler_{index:03d}.py: "
                    f"class AdminReport{index:03d}; def build_report_{index:03d}()"
                    for index in range(180)
                ),
            ]
        )
        rendered = project_map.ProjectMap(
            files=tuple(
                f"apps/admin/src/generated/filler_{index:03d}.py"
                for index in range(80)
            ),
            symbol_overview=symbol,
            focused_subtree=focused,
        ).render()

        self.assertIn("Focused subtree", rendered)
        self.assertIn(target_path, rendered)
        self.assertLess(rendered.index("Focused subtree"), rendered.index("Symbol overview"))
        self.assertIn("map truncated by character budget", rendered)
        self.assertLessEqual(len(rendered), project_map.MAX_PROJECT_MAP_CHARS + 80)

    def test_focused_subtree_stops_at_total_byte_budget(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            first = root / "src" / "router_a.py"
            second = root / "src" / "router_b.py"
            _write(
                first,
                """
                def router_dispatch(request):
                    return True
                """,
            )
            _write(
                second,
                """
                def router_dispatch_extra(request):
                    return True
                """
                + ("#" * 400),
            )
            max_bytes = first.stat().st_size + 16

            with mock.patch.object(project_map, "MAX_FOCUS_TOTAL_BYTES", max_bytes):
                focused = project_map.build_focused_subtree_overview(
                    root,
                    "router dispatch",
                )

        self.assertIn("Focused subtree", focused)
        self.assertIn("src/router_a.py", focused)
        self.assertIn("focused subtree scan stopped", focused)
        self.assertIn("byte budget", focused)

    def test_focused_subtree_is_hidden_without_task(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _build_deep_focus_fixture(root)

            rendered = project_map.render_project_map(root)

        self.assertNotIn("Focused subtree", rendered)

    def test_focused_subtree_skips_secret_symlink_large_and_non_utf8_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for index in range(project_map.MAX_SYMBOL_FILES + 5):
                _write(
                    root / "apps" / "admin" / "src" / f"filler_{index:03d}.py",
                    f"def filler_router_{index:03d}():\n    return True\n",
                )
            _write(
                root / "src" / "visible_router.py",
                """
                def visible_router_dispatch(request):
                    return "VISIBLE_BODY_SHOULD_NOT_APPEAR"
                """,
            )
            _write(
                root / ".hidden" / "hidden_router.py",
                "def hidden_router_dispatch():\n    return True\n",
            )
            _write(
                root / "src" / "private_router.py",
                "def private_router_dispatch():\n    return True\n",
            )
            _write(
                root / "src" / "big_router.py",
                "def big_router_dispatch():\n    return True\n"
                + ("#" * (project_map.MAX_FOCUS_SOURCE_BYTES + 1)),
            )
            (root / "src" / "binary_router.py").write_bytes(b"\xff\xfe\x00\x00")
            link = root / "src" / "linked_router.py"
            try:
                link.symlink_to(root / "src" / "visible_router.py")
            except OSError:
                link = None

            rendered = project_map.render_project_map(root, task="router dispatch")
            focused = project_map.build_focused_subtree_overview(
                root,
                "router dispatch",
            )

        self.assertIn("Focused subtree", rendered)
        self.assertIn("src/visible_router.py", focused)
        self.assertIn("visible_router_dispatch", focused)
        self.assertNotIn("VISIBLE_BODY_SHOULD_NOT_APPEAR", focused)
        self.assertNotIn("hidden_router", focused)
        self.assertNotIn("private_router", focused)
        self.assertNotIn("big_router", focused)
        self.assertNotIn("binary_router", focused)
        if link is not None:
            self.assertNotIn("linked_router", focused)

    def test_symbol_overview_includes_lightweight_js_ts_symbols(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "src").mkdir()
            (root / "src" / "router.ts").write_text(
                "export interface RouteConfig { path: string }\n"
                "export type RouteMode = 'hash'\n"
                "export class Router {}\n"
                "export function buildRouter(config: RouteConfig, mode: RouteMode) { return {} }\n"
                "export const ROUTE_TABLE = []\n",
                encoding="utf-8",
            )

            rendered = project_map.render_project_map(root, task="router config")

        self.assertIn("src/router.ts", rendered)
        self.assertIn("interface RouteConfig", rendered)
        self.assertIn("type RouteMode", rendered)
        self.assertIn("class Router", rendered)
        self.assertIn("function buildRouter(config, mode)", rendered)
        self.assertIn("const ROUTE_TABLE", rendered)

    def test_map_skips_secret_excluded_lock_and_symlink_paths(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.py").write_text("print('ok')\n", encoding="utf-8")
            (root / ".env").write_text("SECRET=1\n", encoding="utf-8")
            (root / "prod.env").write_text("SECRET=2\n", encoding="utf-8")
            (root / "credentials.json").write_text("{}", encoding="utf-8")
            (root / "package-lock.json").write_text("{}", encoding="utf-8")
            (root / "node_modules").mkdir()
            (root / "node_modules" / "pkg.js").write_text("leak\n", encoding="utf-8")
            link = root / "linked.py"
            try:
                link.symlink_to(root / "app.py")
            except OSError:
                link = None

            rendered = project_map.render_project_map(root)

        self.assertIn("app.py", rendered)
        self.assertNotIn(".env", rendered)
        self.assertNotIn("prod.env", rendered)
        self.assertNotIn("credentials.json", rendered)
        self.assertNotIn("package-lock.json", rendered)
        self.assertNotIn("node_modules", rendered)
        if link is not None:
            self.assertNotIn("linked.py", rendered)

    def test_configured_ignored_paths_are_project_relative_prefixes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "generated").mkdir()
            (root / "generated" / "client.py").write_text("def hidden():\n    pass\n", encoding="utf-8")
            (root / "src" / "generated").mkdir(parents=True)
            (root / "src" / "generated" / "client.py").write_text(
                "def visible_generated_router():\n    pass\n",
                encoding="utf-8",
            )

            rendered = project_map.render_project_map(
                root,
                task="generated router",
                ignored_paths=("generated",),
            )

        self.assertNotIn("- generated/client.py", rendered)
        self.assertIn("src/generated/client.py", rendered)
        self.assertIn("visible_generated_router", rendered)

    def test_configured_ignored_paths_apply_to_focused_subtree(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write(
                root / "apps" / "generated" / "src" / "router.py",
                """
                def generated_router_dispatch(request):
                    return True
                """,
            )
            _write(
                root / "apps" / "real" / "src" / "router.py",
                """
                def real_router_dispatch(request):
                    return True
                """,
            )
            with mock.patch.object(project_map, "MAX_SYMBOL_FILES", 0):
                focused = project_map.build_focused_subtree_overview(
                    root,
                    "router dispatch",
                    ignored_paths=("apps/generated",),
                )

        self.assertNotIn("apps/generated", focused)
        self.assertIn("apps/real", focused)

    def test_symbol_overview_skips_secret_dot_symlink_large_and_non_utf8_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "src").mkdir()
            (root / ".hidden").mkdir()
            (root / "src" / "visible.py").write_text(
                "def visible_router():\n    return True\n",
                encoding="utf-8",
            )
            (root / ".hidden" / "hidden.py").write_text(
                "def hidden_router():\n    pass\n",
                encoding="utf-8",
            )
            (root / "token_helper.py").write_text(
                "def token_router():\n    pass\n",
                encoding="utf-8",
            )
            (root / "src" / "big.py").write_text(
                "def big_router():\n    pass\n" + ("#" * (project_map.MAX_SYMBOL_FILE_BYTES + 1)),
                encoding="utf-8",
            )
            (root / "src" / "binary.py").write_bytes(b"\xff\xfe\x00\x00")
            link = root / "src" / "linked.py"
            try:
                link.symlink_to(root / "src" / "visible.py")
            except OSError:
                link = None

            rendered = project_map.build_symbol_overview(root, task="router")

        self.assertIn("visible_router", rendered)
        self.assertNotIn("hidden_router", rendered)
        self.assertNotIn("token_router", rendered)
        self.assertNotIn("big_router", rendered)
        self.assertNotIn("binary.py", rendered)
        if link is not None:
            self.assertNotIn("linked.py", rendered)

    def test_symbol_overview_is_bounded_and_marks_truncation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "src").mkdir()
            for index in range(30):
                (root / "src" / f"router_{index:02}.py").write_text(
                    f"def router_{index:02}_dispatch(alpha, beta, gamma):\n"
                    "    return alpha\n",
                    encoding="utf-8",
                )

            overview = project_map.build_symbol_overview(root, "router dispatch", max_chars=180)

        self.assertIn("symbol overview truncated", overview)
        self.assertLessEqual(len(overview), 260)

    def test_symbol_overview_has_directory_budget(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for index in range(5):
                folder = root / f"module_{index}"
                folder.mkdir()
                (folder / "router.py").write_text(
                    f"def router_{index}():\n    return True\n",
                    encoding="utf-8",
                )

            with mock.patch.object(project_map, "MAX_SYMBOL_DIRECTORIES", 2):
                overview = project_map.build_symbol_overview(root, "router")

        self.assertIn("module_0/router.py", overview)
        self.assertNotIn("module_4/router.py", overview)

    def test_large_directory_is_bounded_and_marked_truncated(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for index in range(project_map.MAX_DIRECTORY_ENTRIES + 5):
                (root / f"file_{index:03}.py").write_text("x = 1\n", encoding="utf-8")

            rendered = project_map.render_project_map(root)

        self.assertIn("map truncated", rendered)
        self.assertLessEqual(len(rendered), project_map.MAX_PROJECT_MAP_CHARS + 80)


if __name__ == "__main__":
    unittest.main()
