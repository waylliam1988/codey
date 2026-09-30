"""Completion checks have dedicated owners; the gate stays the sole proof owner.

- ``operations.project_completion_checks`` owns project/edit/verification
  and engine evidence checks.
- ``operations.research_completion_checks`` owns source/ledger/report checks.
- ``operations.completion_gate`` owns the final contract/proof combination
  and is the only module that mints a completion proof.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_check_provider_modules_exist() -> None:
    assert (ROOT / "codey/operations/project_completion_checks.py").exists()
    assert (ROOT / "codey/operations/research_completion_checks.py").exists()
    from codey.operations import project_completion_checks, research_completion_checks

    assert callable(getattr(project_completion_checks, "project_completion_checks", None))
    assert callable(getattr(research_completion_checks, "source_requirement_checks", None))
    assert callable(getattr(research_completion_checks, "strict_research_checks", None))


def test_gate_is_sole_proof_minter() -> None:
    import ast

    gate_text = (ROOT / "codey/operations/completion_gate.py").read_text(encoding="utf-8-sig")
    assert "project_completion_proof" in gate_text
    for rel in (
        "codey/operations/project_completion_checks.py",
        "codey/operations/research_completion_checks.py",
    ):
        text = (ROOT / rel).read_text(encoding="utf-8-sig")
        assert "project_completion_proof" not in text, rel
        tree = ast.parse(text, filename=rel)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "codey.operations.completion_gate":
                raise AssertionError(f"{rel} must not import the gate")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name != "codey.operations.completion_gate", rel


def test_gate_keeps_single_evaluate_entry() -> None:
    from codey.operations import completion_gate

    assert callable(getattr(completion_gate, "evaluate", None))
    text = (ROOT / "codey/operations/completion_gate.py").read_text(encoding="utf-8-sig")
    assert "def _coding_checks" not in text
    assert "def _ledger_checks" not in text
