"""Browser candidates cannot silently become production tools or runtime files."""
from __future__ import annotations

from pathlib import Path

from codey.launcher.runtime import read_runtime
from codey.research.browser_search import BrowserSearchProvider
from codey.research.search_factory import default_research_search_provider
from codey.research.tool_contract import PROTOCOL_UNKNOWN_TOOL, TOOL_CONTRACTS, validate_tool_args
from tools.browser_experiment_contracts import BROWSER_CONTRACTS


def test_default_research_uses_only_production_provider_and_contracts() -> None:
    provider = default_research_search_provider()
    try:
        assert type(provider.base_provider) is BrowserSearchProvider
        assert not BROWSER_CONTRACTS.keys() & TOOL_CONTRACTS.keys()
        for name in BROWSER_CONTRACTS:
            result = validate_tool_args(name, {})
            assert not result.ok
            assert result.error_kind == PROTOCOL_UNKNOWN_TOOL
    finally:
        provider.close()


def test_runtime_inventory_excludes_browser_candidates() -> None:
    root = Path(__file__).resolve().parents[1]
    files = read_runtime(root)["files"]
    assert isinstance(files, list)
    assert "codey/research/browser_search.py" in files
    assert "codey/research/browser_interaction.py" not in files
    assert "codey/research/resumable_browser.py" not in files
    assert not any(str(path).startswith("tools/") for path in files)
