"""Legacy loops must be gone: files deleted, no importers in prod or tests.

Locks:
- Both the former direct-send follow-up loop and the unreachable outer
  investigation branch are retired, with no production or test importers.
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
    def test_legacy_modules_deleted(self) -> None:
        for rel in (
            "codey/agents/prompt_context.py",
            "codey/agents/result_delivery.py",
            "codey/agents/tool_turn.py",
            "codey/protocols/native_openai.py",
            "codey/research/evidence_followup.py",
            "codey/operations/evidence_followup.py",
            "codey/operations/research_investigation.py",
            "codey/operations/research_synthesis.py",
            "codey/research/evidence_rules.py",
            "codey/research/plan_executor.py",
            "codey/research/record_merge.py",
            "codey/research/followup_selection.py",
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
            "codey.research.evidence_followup",
            "codey.operations.evidence_followup",
            "codey.operations.research_investigation",
            "codey.operations.research_synthesis",
            "codey.research.evidence_rules",
            "codey.research.plan_executor",
            "codey.research.record_merge",
            "codey.research.followup_selection",
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
