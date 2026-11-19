"""The test owner can remove deep state paths created by production writes."""
from __future__ import annotations

import os
import warnings
from pathlib import Path

from codey.storage.atomic_io import filesystem_path
from tests.support.pytest_temp import TempRun


def test_successful_run_cleanup_handles_long_production_state_paths(tmp_path: Path) -> None:
    root = tmp_path / "run-long-path"
    nested = root / ("state" * 20) / ("project" * 12) / ("task" * 20) / "baseline.txt"
    assert len(str(nested.absolute())) > 260
    target = filesystem_path(nested)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"retained source baseline")
    run = TempRun(root, os.getpid())
    run.finish(0)

    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        run.cleanup()

    assert not recorded, [str(item.message) for item in recorded]
    assert not root.exists()
