"""Production browser contracts never depend on experiment modules."""
from __future__ import annotations

from pathlib import Path

from codey.launcher.runtime import read_runtime
from codey.research.answer_contract import ANSWER_TOOLS
from codey.research.browser_search import BrowserSearchProvider
from codey.research.search_factory import default_research_search_provider
from codey.research.tool_contract import validate_tool_args
from codey.research.web_sources import WebSourceProvider
from codey.toolchain.tool_spec import custom_executor_for, spec_for_tool

BROWSER_EXPERIMENT_TOOLS = (
    "browser_snapshot", "browser_click", "browser_fill", "browser_scroll", "browser_wait", "browser_read",
)


def test_default_research_uses_only_production_provider_and_contracts() -> None:
    provider = default_research_search_provider()
    try:
        assert type(provider.base_provider) is WebSourceProvider
        assert type(provider.base_provider.browser_source) is BrowserSearchProvider
        for name in BROWSER_EXPERIMENT_TOOLS:
            spec = spec_for_tool(name)
            assert spec is not None and spec.executor == "browser"
            assert custom_executor_for(name) is None
            assert name not in ANSWER_TOOLS
        assert validate_tool_args("browser_snapshot", {}).ok
        assert not validate_tool_args("browser_click", {}).ok
    finally:
        provider.close()


def test_runtime_inventory_excludes_browser_candidates() -> None:
    root = Path(__file__).resolve().parents[1]
    files = read_runtime(root)["files"]
    assert isinstance(files, list)
    assert "codey/research/browser_search.py" in files
    assert "codey/automation/browser_session.py" in files
    assert "codey/research/browser_interaction.py" not in files
    assert "codey/research/resumable_browser.py" not in files
    assert not any(str(path).startswith("tools/") for path in files)
