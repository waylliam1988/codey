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


def test_support_adapter_stays_fixture_only() -> None:
    """测试适配器只保留形状/夹具：行为（分发/校验/状态判定）一律走生产。"""
    from tests.support import research_iteration_adapter as adapter

    assert callable(adapter.ResearchIteration)
    for retired in ("ResearchToolOutcome", "_dispatch"):
        assert not hasattr(adapter, retired), retired
        assert retired not in dir(adapter.ResearchIteration), retired
