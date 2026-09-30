"""Legacy loops must be gone: files deleted, no importers in prod or tests.

Locks:
- ``codey.research.evidence_followup.run_evidence_followup`` (direct
  provider.send loop) has no production importers; production uses
  ``codey.operations.evidence_followup``.
- Legacy ``agents.prompt_context/result_delivery/tool_turn`` and
  ``protocols.native_openai`` are deleted: files must not exist and neither
  production nor tests may import them. ``agents.tool_execution`` +
  ``agents.request`` stay (still used).
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CODEY = ROOT / "codey"


def _imports_of(path: Path) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise AssertionError(f"cannot parse {path}: {exc}") from exc
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

    def test_legacy_modules_deleted(self) -> None:
        for rel in (
            "codey/agents/prompt_context.py",
            "codey/agents/result_delivery.py",
            "codey/agents/tool_turn.py",
            "codey/protocols/native_openai.py",
            "codey/research/evidence_followup.py",
            "tests/test_native_openai_codec.py",
        ):
            self.assertFalse((ROOT / rel).exists(), f"legacy file must be deleted: {rel}")

    def test_legacy_modules_have_no_importers(self) -> None:
        legacy_names = {
            "codey.agents.prompt_context",
            "codey.agents.result_delivery",
            "codey.agents.tool_turn",
            "codey.protocols.native_openai",
            "codey.toolchain.registry",
        }
        offenders: list[str] = []
        # The independent parity subprocess imports the immutable old checkout,
        # never a compatibility implementation in the production source tree.
        for path in list(CODEY.rglob("*.py")) + list((ROOT / "tests").rglob("*.py")) + list((ROOT / "tools").rglob("*.py")):
            try:
                rel = path.relative_to(ROOT).as_posix()
            except Exception:
                continue
            if rel == "tests/support/kernel_parity_probe.py":
                continue
            imports = _imports_of(path)
            hit = legacy_names & imports
            if hit:
                offenders.append(f"{rel}: {sorted(hit)}")
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            for name in ("agents.prompt_context", "agents.result_delivery", "agents.tool_turn", "protocols.native_openai"):
                if (
                    name in text
                    and "test_architecture_no_legacy_loops" not in rel
                    and (f"codey.{name}" in text or f"codey/{name.replace('.', '/')}.py" in text)
                ):
                    offenders.append(f"{rel}: string ref {name}")
        self.assertEqual(offenders, [], f"legacy modules still referenced: {offenders}")

    def test_kept_agents_modules_still_used(self) -> None:
        # Guard against over-deletion: tool_execution + request must stay.
        import importlib.util

        for mod in ("codey.agents.tool_execution", "codey.agents.request"):
            spec = importlib.util.find_spec(mod)
            self.assertIsNotNone(spec, f"{mod} must remain importable")


if __name__ == "__main__":
    unittest.main()
