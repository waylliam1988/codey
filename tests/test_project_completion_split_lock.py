"""Project completion split lock: behavior preserved across module boundaries.

TDD lock written before the split (fails until the four modules exist).
After the split the public orchestration stays in
``project_completion_flow`` while ownership moves to canonical homes:

- context dataclasses + limits -> project_completion_context
- writer phase -> project_writer_phase.run_writer_phase
- review phase -> project_review_phase.run_review_phase
- enforcement -> project_completion_enforcement.enforce_completion

No private forwarders in the main module: tests patch the actual owner.
"""
from __future__ import annotations

import unittest


class ProjectCompletionSplitLockTests(unittest.TestCase):
    def test_shared_context_owns_dataclasses_and_limits(self) -> None:
        from codey.operations import project_completion_context as ctx

        for name in (
            "AgentAccess",
            "PersistenceAccess",
            "VerificationAccess",
            "ReviewAccess",
            "RuntimeAccess",
            "ProjectCompletionDeps",
            "ProjectRun",
            "MAX_COMPLETION_REPAIR_ROUNDS",
            "COMPLETION_REPAIR_FOLLOWUP",
        ):
            self.assertTrue(hasattr(ctx, name), f"context must own {name}")

    def test_phase_modules_expose_public_entries(self) -> None:
        from codey.operations import project_completion_enforcement as enf
        from codey.operations import project_review_phase as rev
        from codey.operations import project_writer_phase as wrt

        self.assertTrue(callable(getattr(wrt, "run_writer_phase", None)))
        self.assertTrue(callable(getattr(rev, "run_review_phase", None)))
        self.assertTrue(callable(getattr(enf, "enforce_completion", None)))

    def test_main_orchestration_calls_phases_without_private_forwarders(self) -> None:
        import ast
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        main = (root / "codey" / "operations" / "project_completion_flow.py").read_text(encoding="utf-8")
        tree = ast.parse(main)
        defined = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        # No private phase implementations may remain in the orchestrator.
        for name in (
            "_run_one_writer_attempt",
            "_select_next_writer",
            "_run_writer_phase",
            "_review_cycle_phase",
            "_enforce_completion",
            "_maybe_run_completion_repair",
        ):
            self.assertNotIn(name, defined, f"main must not define {name}")
        # Orchestrator calls the public phase entries.
        for token in ("run_writer_phase(", "run_review_phase(", "enforce_completion("):
            self.assertIn(token, main, f"main must call {token.rstrip('(')}")

    def test_public_orchestration_still_importable_from_flow(self) -> None:
        from codey.operations import project_completion_flow as flow

        for name in ("run_project_mode", "handle_project_tool_event", "blocked_result", "safe_verification_map"):
            self.assertTrue(callable(getattr(flow, name, None)), f"flow must keep {name}")

    def test_patch_paths_point_at_actual_owner(self) -> None:
        # Import-time check: patch targets must resolve to the owning module,
        # not to a forwarder in the orchestrator.
        import codey.operations.project_completion_enforcement as enf
        import codey.operations.project_review_phase as rev
        import codey.operations.project_writer_phase as wrt

        self.assertTrue(hasattr(wrt, "run_writer_phase"))
        self.assertTrue(hasattr(rev, "run_review_phase"))
        self.assertTrue(hasattr(enf, "enforce_completion"))

    def test_internal_imports_use_true_owner_not_flow_reexport(self) -> None:
        import ast
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        # Production code must import shared helpers from the true owner
        # (context / phase modules), never via flow re-exports. The only
        # allowed flow import is dispatch's run_project_mode (flow owns
        # orchestration).
        checked = {
            "codey/operations/task_phases/hooks.py": {"run_project_mode"},
            "codey/operations/task_phases/dispatch.py": {"run_project_mode"},
            "codey/operations/task_phases/lifecycle.py": set(),
            "codey/operations/review_flow.py": set(),
        }
        for rel, allowed in checked.items():
            source = (root / rel).read_text(encoding="utf-8")
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == "codey.operations.project_completion_flow":
                    for alias in node.names:
                        self.assertIn(
                            alias.name, allowed,
                            f"{rel} must not import {alias.name} via flow re-export",
                        )

    def test_no_project_run_compat_alias(self) -> None:
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        for rel in (
            "codey/operations/project_completion_context.py",
            "codey/operations/project_completion_flow.py",
        ):
            source = (root / rel).read_text(encoding="utf-8")
            self.assertNotIn("_ProjectRun", source, f"{rel} must not keep _ProjectRun alias")


if __name__ == "__main__":
    unittest.main()
