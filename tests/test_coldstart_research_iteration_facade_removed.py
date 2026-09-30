"""Cold-start lock for the shared research entry.

The production module exposes the kernel entry only.  The former iterator
facade is test and benchmark scaffolding and must not remain in production.
"""

from __future__ import annotations

import importlib


def test_research_iteration_module_exposes_kernel_entry_only() -> None:
    module = importlib.import_module("codey.operations.research_iteration")

    assert callable(module.run_research_iteration)
    for retired in ("ResearchIteration", "ResearchToolOutcome", "render_research_repair_prompt"):
        assert not hasattr(module, retired), retired
