"""Portable paths for the archived manual memory-routing experiments."""

from __future__ import annotations

import os
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO = SCRIPT_DIR.parents[2]
DATA_ROOT = Path(
    os.environ.get("GHOST_EXPERIMENT_HOME")
    or REPO / "artifacts" / "ghost_memory_routing"
).resolve()
DATA_ROOT.mkdir(parents=True, exist_ok=True)
