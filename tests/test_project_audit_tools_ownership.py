"""Project-audit scanning lives outside consensus orchestration.

``codey.agents.project_audit_tools`` owns path filtering, directory budgets,
file search, and read-only call execution. ``consensus`` keeps multi-model
consultation only; the operations advisor consumes the leaf directly. The
scanning leaf has no provider, model-send, or operations dependencies, and
consensus keeps no scan implementation or compatibility re-exports.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEAF = ROOT / "codey" / "agents" / "project_audit_tools.py"


def test_audit_tools_leaf_exists_with_public_entries() -> None:
    assert LEAF.exists(), "missing codey/agents/project_audit_tools.py"
    from codey.agents import project_audit_tools as tools

    assert callable(getattr(tools, "execute_read_only_call", None))
    assert callable(getattr(tools, "visible_entries", None))


def test_audit_leaf_has_no_provider_or_operations_dependency() -> None:
    import ast

    tree = ast.parse(LEAF.read_text(encoding="utf-8-sig"), filename=str(LEAF))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    forbidden = [name for name in imports if name.startswith(("codey.operations", "codey.providers"))]
    assert forbidden == []
    text = LEAF.read_text(encoding="utf-8-sig")
    assert ".send(" not in text


def test_consensus_has_no_scanning_implementation_or_shims() -> None:
    text = (ROOT / "codey/agents/consensus.py").read_text(encoding="utf-8-sig")
    for legacy in (
        "def _audit_search_files",
        "def _execute_read_only_call",
        "def _audit_visible_entries",
        "def _audit_path_block_reason",
        "PROJECT_AUDIT_MAX_SCAN_FILES =",
        "AUDIT_EXCLUDED_DIRS =",
        "READ_ONLY_AUDIT_PROMPT",
        "def render_project_audit_prompt",
    ):
        assert legacy not in text, legacy


def test_advisor_consumes_audit_leaf_directly() -> None:
    text = (ROOT / "codey/operations/project_audit_advisor.py").read_text(encoding="utf-8-sig")
    assert "project_audit_tools" in text
