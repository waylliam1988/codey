"""Coding agent public entry (legacy loop compat + neutral result).

Production task turns run via the single entry ``run_task_mode`` (new names
``task_loop``/``project_adapter``). This module keeps the legacy loop for
old-structure tests/manual scripts; production operation modules must not
import it (locked by architecture tests).
"""

from __future__ import annotations

from codey.agents.loop import (
    DEFAULT_CODEC,
    INFORMATION_TOOL_NAMES,
    SUPPORTED_TOOL_NAMES,
    parse_reply,
    run,
)
from codey.runtime.core.run_result import RunResult  # noqa: F401  (neutral re-export)

__all__ = [
    "DEFAULT_CODEC",
    "INFORMATION_TOOL_NAMES",
    "SUPPORTED_TOOL_NAMES",
    "RunResult",
    "parse_reply",
    "run",
]
