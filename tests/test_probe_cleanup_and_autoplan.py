"""Red-first: probe exception must not leak state dir; auto plan must not swallow."""
from __future__ import annotations

import importlib.util
import unittest
from dataclasses import dataclass
from pathlib import Path

_TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"


def _load_probe():
    spec = importlib.util.spec_from_file_location(
        "live_probe_split_cleanup_red", _TOOLS_DIR / "live_probe_split.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


probe = _load_probe()


class ProbeCleanupTests(unittest.TestCase):
    def test_agent_probe_exception_returns_state_home(self) -> None:
        def _boom(_root: Path) -> None:
            raise RuntimeError("fixture boom")

        with unittest.mock.patch.object(
            probe, "run_headless", side_effect=RuntimeError("headless boom"),
        ):
            data = probe._run_agent_probe("pX", "task", _boom, 1)
        self.assertIn("_state_home", data)
        state_home = Path(str(data["_state_home"]))
        # Caller cleanup needs both paths; the callee must hand them back.
        self.assertTrue(str(state_home))
        import shutil

        shutil.rmtree(data["_root"], ignore_errors=True)
        shutil.rmtree(state_home, ignore_errors=True)


class AutoPlanContractTests(unittest.TestCase):
    def test_with_auto_plan_does_not_swallow_replace_errors(self) -> None:
        from codey.operations.auto_loop import with_auto_plan

        @dataclass(frozen=True)
        class _NoHint:
            task: str = "hi"

        with self.assertRaises(TypeError):
            with_auto_plan(_NoHint(), "plan text")


if __name__ == "__main__":
    unittest.main()
