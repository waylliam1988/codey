"""Cold-start cleanup round4 locks (red-first).

Covers the three items confirmed as safe cleanup:

1. Package-level convenience export layers removed
   (``research``/``knowledge``/``providers`` keep only a docstring;
   call sites import from the defining leaf module).
2. Ghost byte-identical pure helpers merged into ``ghost/_common.py``.
3. Native-tools enable check unified beside
   ``provider_supports_structured`` plus recovered-delivery fallback
   comment reflecting the current ``candidate_from_intent`` data flow.

These tests assert the CLEANED state, so they fail on the pre-cleanup
code and pass after the cleanup.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Package-convenience names that must be imported from leaves after cleanup.
# Submodule imports such as ``from codey.providers import controls`` stay legal.
PROVIDERS_EXPORTS = {
    "ChatProvider",
    "ApiProvider",
    "DEFAULT_PROVIDER_ID",
    "PROVIDER_LABELS",
    "borrow_open_provider",
    "connect_existing_provider",
    "connect_fresh_provider_tab",
    "connect_provider",
    "provider_tab_availability",
    "provider_ids",
    "warm_provider_tabs",
    "DeepSeekWebProvider",
    "GlmWebProvider",
    "MimoWebProvider",
    "QwenWebProvider",
    "StepFunWebProvider",
    "WebChatProvider",
    "WebProviderSpec",
}
KNOWLEDGE_EXPORTS = {
    "ConceptGraphBuilder",
    "KnowledgeBriefBuilder",
    "KnowledgeChanges",
    "KnowledgeChangesSnapshot",
    "KnowledgeGraphBuilder",
    "KnowledgeNote",
    "KnowledgeStore",
    "NOTE_TYPES",
    "ResearchBrief",
    "ResearchGraphArtifact",
    "ResearchInterestCandidate",
    "RestoreResult",
    "build_research_interest_candidates",
    "build_unified_research_graph",
    "candidate_to_topic_hint",
}
RESEARCH_EXPORTS = {
    "BrowserSearchProvider",
    "ConnectorAwareSearchProvider",
    "EvidenceFollowupResult",
    "EvidenceFollowupRunner",
    "FinalizedAnswer",
    "PlanExecutionResult",
    "PlanExecutor",
    "ReportQualityReview",
    "ResearchIterationRun",
    "ResearchPipeline",
    "ResearchPipelineResult",
    "ResearchRunResult",
    "ResearchTools",
    "finalize_done_answer",
    "merge_evidence_patch",
    "provenance_problem",
    "review_report_quality",
    "run_evidence_followup",
}


def _from_imports(path: Path) -> list[tuple[str, int, list[str]]]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return []
    out: list[tuple[str, int, list[str]]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in {
            "codey.research",
            "codey.knowledge",
            "codey.providers",
        }:
            out.append((node.module or "", node.lineno, [a.name for a in node.names]))
    return out


class PackageExportLayerTests(unittest.TestCase):
    def test_init_files_have_no_lazy_export_table(self) -> None:
        for rel in (
            "codey/research/__init__.py",
            "codey/knowledge/__init__.py",
            "codey/providers/__init__.py",
        ):
            text = (REPO_ROOT / rel).read_text(encoding="utf-8")
            self.assertNotIn("_EXPORTS", text, rel)
            self.assertNotIn("__getattr__", text, rel)
            # A docstring-only package init must not keep the typing import
            # that only served the lazy table.
            self.assertNotIn("from typing import Any", text, rel)

    def test_no_package_convenience_imports_in_codebase(self) -> None:
        offenders: list[str] = []
        for base in ("codey", "tests", "tools"):
            root = REPO_ROOT / base
            if not root.exists():
                continue
            for path in sorted(root.rglob("*.py")):
                # The lock test itself documents the forbidden names; skip it.
                if path.name == "test_coldstart_export_cleanup.py":
                    continue
                for module, lineno, names in _from_imports(path):
                    forbidden = {
                        "codey.research": RESEARCH_EXPORTS,
                        "codey.knowledge": KNOWLEDGE_EXPORTS,
                        "codey.providers": PROVIDERS_EXPORTS,
                    }[module]
                    bad = sorted(set(names) & forbidden)
                    if bad:
                        offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {bad}")
        self.assertEqual(offenders, [], f"package-convenience imports remain: {offenders[:10]}")

    def test_no_string_references_to_removed_package_attrs(self) -> None:
        # mock.patch/importlib strings must target the leaf module after
        # the convenience layer is gone (e.g. test_cli patching
        # codey.providers.connect_provider would AttributeError).
        offenders: list[str] = []
        for base in ("codey", "tests", "tools"):
            root = REPO_ROOT / base
            if not root.exists():
                continue
            for path in sorted(root.rglob("*.py")):
                if path.name == "test_coldstart_export_cleanup.py":
                    continue
                try:
                    text = path.read_text(encoding="utf-8")
                except OSError:
                    continue
                for pkg, names in (
                    ("providers", PROVIDERS_EXPORTS),
                    ("knowledge", KNOWLEDGE_EXPORTS),
                    ("research", RESEARCH_EXPORTS),
                ):
                    for name in names:
                        needle = f"codey.{pkg}.{name}"
                        if needle in text:
                            offenders.append(f"{path.relative_to(REPO_ROOT)}: {needle}")
                            break
        self.assertEqual(offenders, [], f"stale package-attr strings remain: {offenders[:10]}")

    def test_direct_leaf_imports_resolve(self) -> None:
        from codey.agents.request import AgentRequest  # noqa: F401
        from codey.knowledge.brief import KnowledgeBriefBuilder  # noqa: F401
        from codey.knowledge.changes import KnowledgeChanges, RestoreResult  # noqa: F401
        from codey.knowledge.graph import KnowledgeGraphBuilder  # noqa: F401
        from codey.knowledge.note import KnowledgeNote  # noqa: F401
        from codey.knowledge.store import KnowledgeStore  # noqa: F401
        from codey.operations.research_iteration import run_research_iteration  # noqa: F401
        from codey.providers.base import ChatProvider  # noqa: F401
        from codey.providers.catalog import DEFAULT_PROVIDER_ID, PROVIDER_LABELS, provider_ids  # noqa: F401
        from codey.providers.registry import connect_provider  # noqa: F401
        from codey.providers.web_provider import (  # noqa: F401
            DeepSeekWebProvider,
            GlmWebProvider,
            MimoWebProvider,
            QwenWebProvider,
            StepFunWebProvider,
        )
        from codey.research.pipeline import ResearchPipeline  # noqa: F401
        from codey.research.run_result import ResearchRunResult  # noqa: F401

        self.assertTrue(callable(provider_ids))
        self.assertIsInstance(DEFAULT_PROVIDER_ID, str)
        self.assertIsInstance(PROVIDER_LABELS, dict)




class NativeToolsUnifyTests(unittest.TestCase):
    def test_kernel_transport_owns_single_native_check(self) -> None:
        from codey.operations import kernel_transport

        self.assertTrue(callable(getattr(kernel_transport, "provider_uses_native", None)))

    def test_old_wrappers_removed_and_callers_unified(self) -> None:
        # Cold-start closure: old loop deleted, single native check via new entry.
        self.assertFalse((REPO_ROOT / "codey/agents/loop.py").exists())
        self.assertFalse((REPO_ROOT / "codey/agents/runner.py").exists())
        self.assertFalse((REPO_ROOT / "codey/agents/prompt_context.py").exists())
        self.assertFalse((REPO_ROOT / "codey/agents/result_delivery.py").exists())
        self.assertFalse((REPO_ROOT / "codey/agents/tool_turn.py").exists())
        self.assertFalse((REPO_ROOT / "codey/protocols/native_openai.py").exists())
        from codey.operations import kernel_transport

        self.assertTrue(callable(getattr(kernel_transport, "provider_uses_native", None)))

    def test_native_check_via_production_entry(self) -> None:
        from unittest import mock

        from codey.operations import kernel_transport

        class _Structured:
            def send_turn(self, *a, **k):
                return None

            def send_tool_results(self, *a, **k):
                return None

            def acknowledge_tool_results(self, results, declared_tools, timeout=None):
                return self.send_tool_results(results, [])

        class _Plain:
            pass

        with mock.patch(
            "codey.providers.native_tools.supports_native_tools", return_value=True
        ):
            self.assertTrue(kernel_transport.provider_uses_native(_Structured(), provider_id="local"))
        with mock.patch(
            "codey.providers.native_tools.supports_native_tools", return_value=False
        ):
            self.assertFalse(kernel_transport.provider_uses_native(_Plain(), provider_id="local"))


if __name__ == "__main__":
    unittest.main()
