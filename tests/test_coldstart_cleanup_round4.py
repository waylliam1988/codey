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
    "LocalOpenAIProvider",
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
    "EvidenceNote",
    "EvidencePack",
    "FinalizedAnswer",
    "PlanExecutionResult",
    "PlanExecutor",
    "ReportQualityReview",
    "ResearchIterationRun",
    "ResearchPipeline",
    "ResearchPipelineResult",
    "ResearchRunResult",
    "ResearchRunner",
    "ResearchTools",
    "finalize_done_answer",
    "merge_evidence_patch",
    "provenance_problem",
    "review_report_quality",
    "run_evidence_followup",
    "run_research_advisors",
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
                if path.name == "test_coldstart_cleanup_round4.py":
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
                if path.name == "test_coldstart_cleanup_round4.py":
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
        from codey.research.runner import ResearchRunner  # noqa: F401

        self.assertTrue(callable(provider_ids))
        self.assertIsInstance(DEFAULT_PROVIDER_ID, str)
        self.assertIsInstance(PROVIDER_LABELS, dict)


class GhostCommonHelperTests(unittest.TestCase):
    def test_common_owns_shared_pure_helpers(self) -> None:
        from codey.ghost import _common

        for name in ("event_ts", "valid_nonnegative_int_payload", "reverse_text_sort_key"):
            self.assertTrue(callable(getattr(_common, name, None)), name)
        self.assertIn("event_ts", getattr(_common, "__all__", []))
        self.assertIn("valid_nonnegative_int_payload", getattr(_common, "__all__", []))
        self.assertIn("reverse_text_sort_key", getattr(_common, "__all__", []))

    def test_no_local_duplicate_defs_remain(self) -> None:
        expectations = {
            "codey/ghost/affinity.py": ["def _event_ts", "def _valid_nonnegative_int_payload"],
            "codey/ghost/work_queue.py": [
                "def _event_ts",
                "def _valid_nonnegative_int_payload",
                "def _reverse_text_sort_key",
            ],
            "codey/ghost/continuity.py": ["def _reverse_text_sort_key"],
            "codey/ghost/directive.py": ["def _reverse_text_sort_key"],
        }
        for rel, markers in expectations.items():
            text = (REPO_ROOT / rel).read_text(encoding="utf-8")
            for marker in markers:
                self.assertNotIn(marker, text, f"{rel}: {marker}")

    def test_call_sites_use_common_helpers(self) -> None:
        text_affinity = (REPO_ROOT / "codey/ghost/affinity.py").read_text(encoding="utf-8")
        text_queue = (REPO_ROOT / "codey/ghost/work_queue.py").read_text(encoding="utf-8")
        text_continuity = (REPO_ROOT / "codey/ghost/continuity.py").read_text(encoding="utf-8")
        text_directive = (REPO_ROOT / "codey/ghost/directive.py").read_text(encoding="utf-8")
        self.assertIn("_common.event_ts", text_affinity)
        self.assertIn("_common.valid_nonnegative_int_payload", text_affinity)
        self.assertIn("_common.event_ts", text_queue)
        self.assertIn("_common.valid_nonnegative_int_payload", text_queue)
        self.assertIn("_common.reverse_text_sort_key", text_queue)
        self.assertIn("_common.reverse_text_sort_key", text_continuity)
        self.assertIn("_common.reverse_text_sort_key", text_directive)

    def test_shared_helpers_keep_exact_semantics(self) -> None:
        from codey.ghost import _common

        # bool is not treated as an int; negatives rejected.
        self.assertTrue(_common.valid_nonnegative_int_payload(0))
        self.assertTrue(_common.valid_nonnegative_int_payload(7))
        self.assertFalse(_common.valid_nonnegative_int_payload(True))
        self.assertFalse(_common.valid_nonnegative_int_payload(False))
        self.assertFalse(_common.valid_nonnegative_int_payload(-1))
        self.assertFalse(_common.valid_nonnegative_int_payload(1.0))
        self.assertFalse(_common.valid_nonnegative_int_payload("3"))
        self.assertFalse(_common.valid_nonnegative_int_payload(None))
        # ts clips to 80 chars via the shared schema clip.
        self.assertEqual(_common.event_ts({"ts": "2026-01-01T00:00:00Z"}), "2026-01-01T00:00:00Z")
        self.assertEqual(len(_common.event_ts({"ts": "x" * 200})), 80)
        # Reverse-text key is byte-exact with the removed duplicates.
        self.assertEqual(_common.reverse_text_sort_key("abc"), tuple(-ord(c) for c in "abc"))
        self.assertEqual(_common.reverse_text_sort_key(""), ())
        self.assertEqual(_common.reverse_text_sort_key(None), ())


class NativeToolsUnifyTests(unittest.TestCase):
    def test_prompt_context_owns_single_native_check(self) -> None:
        from codey.agents import prompt_context

        self.assertTrue(callable(getattr(prompt_context, "session_uses_native_tools", None)))
        self.assertIn("session_uses_native_tools", getattr(prompt_context, "__all__", []))

    def test_old_wrappers_removed_and_callers_unified(self) -> None:
        loop_text = (REPO_ROOT / "codey/agents/loop.py").read_text(encoding="utf-8")
        delivery_text = (REPO_ROOT / "codey/agents/result_delivery.py").read_text(encoding="utf-8")
        self.assertNotIn("def _use_native", loop_text)
        self.assertNotIn("def _use_native_delivery", delivery_text)
        self.assertIn("session_uses_native_tools", loop_text)
        self.assertIn("session_uses_native_tools", delivery_text)

    def test_native_check_matches_legacy_condition(self) -> None:
        from types import SimpleNamespace

        from codey.agents.prompt_context import provider_supports_structured, session_uses_native_tools

        class _Provider:
            def __init__(self, structured: bool) -> None:
                if structured:
                    self.send_turn = lambda *a, **k: None
                    self.send_tool_results = lambda *a, **k: None

        def _session(native, structured: bool):  # type: ignore[no-untyped-def]
            return SimpleNamespace(
                config=SimpleNamespace(native_tools=native),
                request=SimpleNamespace(provider=_Provider(structured)),
            )

        self.assertTrue(session_uses_native_tools(_session([], True)))
        self.assertFalse(session_uses_native_tools(_session(None, True)))
        self.assertFalse(session_uses_native_tools(_session([], False)))
        self.assertFalse(session_uses_native_tools(_session(None, False)))
        # Unified helper must equal the two removed wrappers' condition.
        for native in (None, [], [{}]):
            for structured in (False, True):
                session = _session(native, structured)
                expected = session.config.native_tools is not None and provider_supports_structured(session)
                self.assertEqual(session_uses_native_tools(session), expected)

    def test_recovered_fallback_comment_names_current_flow(self) -> None:
        text = (REPO_ROOT / "codey/agents/result_delivery.py").read_text(encoding="utf-8")
        self.assertIn("candidate_from_intent", text)
        self.assertNotIn("predate", text)


if __name__ == "__main__":
    unittest.main()
