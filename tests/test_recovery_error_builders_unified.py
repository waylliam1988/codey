"""Recovery errors must come from unified builders, never scattered ToolResult.

Locks P2 builder unification:
- ``_replay_settled_slot`` contains no direct ``ToolResult(...)`` construction
- mismatch/failed helpers live in ``kernel_recovery_result`` (single owner)
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path


class RecoveryErrorBuildersUnifiedTests(unittest.TestCase):
    def test_replay_uses_unified_builders(self) -> None:
        root = Path(__file__).resolve().parents[1]
        path = root / "codey" / "operations" / "kernel_recovery.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        direct: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = ""
                if isinstance(func, ast.Name):
                    name = func.id
                elif isinstance(func, ast.Attribute):
                    name = func.attr
                if name == "ToolResult":
                    try:
                        src = ast.unparse(node)[:80]
                    except Exception:
                        src = "ToolResult(...)"
                    direct.append(src)
        self.assertEqual(direct, [], f"kernel_recovery must not construct ToolResult directly: {direct}")

    def test_unified_builders_exist(self) -> None:
        from codey.operations import kernel_recovery_result as krr

        self.assertTrue(callable(getattr(krr, "build_recovery_error_result", None)))
        self.assertTrue(callable(getattr(krr, "build_recovery_mismatch_result", None)))


if __name__ == "__main__":
    unittest.main()
