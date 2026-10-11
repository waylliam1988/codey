"""Red-first: probe exceptions return the state directory for caller cleanup."""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

_TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"


def _load_probe():
    spec = importlib.util.spec_from_file_location(
        "local_model_diagnostic_probe_cleanup_red", _TOOLS_DIR / "local_model_diagnostic_probe.py"
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
