from pathlib import Path

import pytest

from codey.codemode.runtime import NodeWorkerRuntime
from codey.codemode.types import CodeModeLimits


@pytest.mark.parametrize("output", [
    '"[".repeat(5000) + "null" + "]".repeat(5000) + "\\n"',
    "Buffer.from([0xff, 0x0a])",
])
def test_worker_output_decoder_returns_protocol_error(tmp_path: Path, output: str) -> None:
    runner = tmp_path / "invalid-output.mjs"
    runner.write_text(f"process.stdin.once('data', () => process.stdout.write({output}));", encoding="utf-8")
    result = NodeWorkerRuntime(runner=runner).execute(
        "return 1;", catalog=(), invoke=lambda _name, _args: None, limits=CodeModeLimits(timeout_ms=1000),
    )
    assert not result.ok
    assert result.error_code == "protocol"
