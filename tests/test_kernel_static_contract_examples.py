"""Static negative examples for the kernel's typed contracts."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

BAD_CONTRACT = '''
from codey.operations.kernel_contracts import DeliveryStatus, ToolExecutor
from codey.runtime.core.models import ToolCall


def wrong_executor(call: ToolCall) -> str:
    return "bad result"


executor: ToolExecutor = wrong_executor
delivery: DeliveryStatus = "not-a-delivery-status"
'''


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("mypy", ("--python-version", "3.11")),
        ("pyrefly", ("check",)),
        ("ty", ("check",)),
    ],
)
def test_type_checker_rejects_invalid_kernel_contract(tool: str, arguments: tuple[str, ...]) -> None:
    source = ROOT / "tests" / f"_bad_kernel_contract_{tool}.py"
    source.write_text(BAD_CONTRACT, encoding="utf-8")
    try:
        result = subprocess.run(
            [sys.executable, "-m", tool, *arguments, str(source)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        output = (result.stdout or "") + (result.stderr or "")
    finally:
        source.unlink(missing_ok=True)
    assert result.returncode != 0, output
    assert f"_bad_kernel_contract_{tool}" in output, output
    assert any(marker in output for marker in ("Incompatible", "not assignable", "assignment", "does not match")), output
