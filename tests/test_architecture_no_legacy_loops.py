"""Production import graph must not use legacy model loops.

Locks:
- ``codey.research.evidence_followup.run_evidence_followup`` (direct
  provider.send loop) has no production importers; production uses
  ``codey.operations.evidence_followup``.
- Legacy ``agents.prompt_context/result_delivery/tool_turn`` and
  ``protocols.native_openai``/``toolchain.registry`` have no production
  importers (tests/manual only). ``agents.tool_execution`` + ``agents.request``
  stay (still used).
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CODEY = ROOT / "codey"

LEGACY_MODULES = [
    "codey/agents/prompt_context.py",
    "codey/agents/result_delivery.py",
    "codey/agents/tool_turn.py",
    "codey/protocols/native_openai.py",
    "codey/toolchain/registry.py",
]

# Files that are allowed to reference legacy modules (tests, manual, the
# legacy modules themselves, and package re-exports pending removal).
ALLOWED_REFERENCERS_PREFIXES = (
    "tests/",
    "tools/",
    "codey/agents/prompt_context.py",
    "codey/agents/result_delivery.py",
    "codey/agents/tool_turn.py",
    "codey/protocols/native_openai.py",
    "codey/protocols/__init__.py",
    "codey/toolchain/registry.py",
)


def _module_name(path: Path) -> str:
    rel = path.relative_to(ROOT).as_posix()
    assert rel.endswith(".py")
    return rel[:-3].replace("/", ".")


def _imports_of(path: Path) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except Exception:
        return set()
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(str(node.module or ""))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                found.add(str(alias.name or ""))
    return found


class NoLegacyLoopsTests(unittest.TestCase):
    def test_old_evidence_followup_has_no_production_importers(self) -> None:
        offenders = []
        for path in CODEY.rglob("*.py"):
            rel = path.relative_to(ROOT).as_posix()
            if rel.startswith("tests/") or "/tests/" in rel:
                continue
            if rel == "codey/research/evidence_followup.py":
                continue
            if rel == "codey/operations/evidence_followup.py":
                continue
            imports = _imports_of(path)
            if "codey.research.evidence_followup" in imports:
                # Importing rules/controller/prompts is fine; importing the old
                # direct-send loop is not.
                text = path.read_text(encoding="utf-8", errors="ignore")
                if (
                    "run_evidence_followup" in text
                    and "operations.evidence_followup" not in text
                    and rel not in {"codey/research/pipeline.py"}
                ):
                    offenders.append(rel)
        # pipeline.py may import result types; ensure it does not import the loop.
        pipe = (ROOT / "codey/research/pipeline.py").read_text(encoding="utf-8")
        self.assertNotIn("run_evidence_followup", pipe)
        self.assertEqual(offenders, [], f"production importers of old loop: {offenders}")

    def test_legacy_modules_have_no_production_importers(self) -> None:
        legacy_names = {
            "codey.agents.prompt_context",
            "codey.agents.result_delivery",
            "codey.agents.tool_turn",
            "codey.protocols.native_openai",
            "codey.toolchain.registry",
        }
        offenders: list[str] = []
        legacy_files = {
            "codey/agents/prompt_context.py",
            "codey/agents/result_delivery.py",
            "codey/agents/tool_turn.py",
            "codey/protocols/native_openai.py",
            "codey/protocols/__init__.py",
            "codey/toolchain/registry.py",
        }
        for path in CODEY.rglob("*.py"):
            rel = path.relative_to(ROOT).as_posix()
            if rel in legacy_files:
                continue
            if rel.startswith("tests/"):
                continue
            imports = _imports_of(path)
            hit = legacy_names & imports
            # Submodule prefix hits (e.g. codey.agents.prompt_context imported as full path)
            if hit:
                offenders.append(f"{rel}: {sorted(hit)}")
        self.assertEqual(offenders, [], f"production still imports legacy modules: {offenders}")

    def test_kept_agents_modules_still_used(self) -> None:
        # Guard against over-deletion: tool_execution + request must stay.
        import importlib.util

        for mod in ("codey.agents.tool_execution", "codey.agents.request"):
            spec = importlib.util.find_spec(mod)
            self.assertIsNotNone(spec, f"{mod} must remain importable")


if __name__ == "__main__":
    unittest.main()
