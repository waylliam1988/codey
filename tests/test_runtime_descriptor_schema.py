"""Runtime descriptor versions are JSON integers."""

from pathlib import Path

import pytest

from codey.control.protocol import ControlError
from codey.launcher.runtime import DESCRIPTOR, read_runtime
from codey.storage.local_store import write_json_atomic


@pytest.mark.parametrize("schema", [True, 1.0])
def test_runtime_descriptor_rejects_non_integer_schema(tmp_path: Path, schema: bool | float) -> None:
    files = ["codey/__main__.py", "codey/web/index.html"]
    for name in files:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    metadata: dict[str, object] = {
        "schema": schema, "version": "test", "requires_python": ">=3.11",
        "dependencies": [], "mcp_dependencies": [], "files": files,
    }
    descriptor = tmp_path / DESCRIPTOR
    write_json_atomic(descriptor, metadata)

    with pytest.raises(ControlError, match="missing or incomplete"):
        read_runtime(tmp_path)

    metadata["schema"] = 1
    write_json_atomic(descriptor, metadata)
    assert read_runtime(tmp_path) == metadata
