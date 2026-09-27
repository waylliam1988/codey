"""Cold-start cleanup locks (TDD red-first, deterministic, no live model).

Post-cleanup expectations for items 1-5 + AppContext isolation:
- Item1: no `check_commands` param; persisted legacy string checks are ignored;
  `{command, cwd}` dicts + CheckEvidence objects still work.
- Item2: `codey.protocols.json_codec` exposes no `get_system_prompt` /
  `SYSTEM_PROMPT` lazy export and no `lru_cache`; single source is
  `JsonToolCodec().system_prompt()`.
- Item3: `codey.runs.ledger/details/trace` share one `clip_text` helper with
  pinned boundary behavior (not `clip_tail` semantics on small limits).
- Item4: dead `ui_state_store._version` and
  `toolchain.runtime._line_body_without_eol` are gone.
- Item5: `codey.app.api` exposes no `build_unified_research_graph` facade;
  `research_graph_response` lazily imports the real builder.
"""
from __future__ import annotations

import inspect
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


class Item1FactsCompatTests(unittest.TestCase):
    def test_no_check_commands_param(self) -> None:
        from codey.workspace.facts import ProjectFactsStore

        sig = inspect.signature(ProjectFactsStore.record_successful_change)
        self.assertNotIn("check_commands", sig.parameters)
        self.assertIn("checks", sig.parameters)

    def test_string_check_object_is_rejected(self) -> None:
        from codey.workspace.facts import _successful_check_from_object

        self.assertIsNone(_successful_check_from_object("python -m unittest"))

    def test_legacy_string_payload_is_ignored(self) -> None:
        from codey.workspace.facts import ProjectFactsStore

        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as std:
            store = ProjectFactsStore(std)
            path = store.path_for(td)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                '{"schema_version":1,"commands":[],"successful_changes":['
                '{"task":"Legacy task","files":["app.py"],'
                '"checks":["python -m unittest"]}]}',
                encoding="utf-8",
            )
            facts = store.load(td)
        self.assertEqual(facts.successful_changes, ())

    def test_structured_checks_still_work(self) -> None:
        from codey.runtime.observe.execution_evidence import CheckEvidence
        from codey.workspace.facts import ProjectFactsStore

        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as std:
            store = ProjectFactsStore(std)
            self.assertTrue(
                store.record_successful_change(
                    td,
                    task="Implement feature",
                    files=["app.py"],
                    checks=[CheckEvidence("python -m unittest", ".")],
                )
            )
            facts = store.load(td)
        self.assertEqual(facts.successful_changes[0].checks[0].command, "python -m unittest")


class Item2SinglePromptSourceTests(unittest.TestCase):
    def test_no_module_lazy_prompt_surface(self) -> None:
        import codey.protocols.json_codec as mod

        self.assertFalse(hasattr(mod, "get_system_prompt"))
        self.assertNotIn("SYSTEM_PROMPT", dir(mod))
        source = Path(mod.__file__).read_text(encoding="utf-8")
        self.assertNotIn("lru_cache", source)
        self.assertNotIn("__getattr__", source)

    def test_codec_prompt_matches_rendered_writer_prompt(self) -> None:
        from codey.protocols.json_codec import JsonToolCodec
        from codey.toolchain import definition as tool_defs
        from codey.toolchain.tool_prompt import render_coding_system_prompt

        expected = render_coding_system_prompt(
            tool_defs.TOOL_DEFINITIONS,
            profile_name="coding_writer",
            allowed_tool_names={d.name for d in tool_defs.TOOL_DEFINITIONS},
        )
        self.assertEqual(JsonToolCodec().system_prompt(), expected)


class Item3SharedClipTests(unittest.TestCase):
    def test_runs_share_one_clip_helper(self) -> None:
        from codey.runs import text_clip

        self.assertTrue(hasattr(text_clip, "clip_text"))
        for mod_name in ("codey.runs.ledger", "codey.runs.details", "codey.runs.trace"):
            source = Path(__import__(mod_name, fromlist=["x"]).__file__).read_text(
                encoding="utf-8"
            )
            self.assertNotIn("def _clip(", source, mod_name)

    def test_clip_boundary_behavior_pinned(self) -> None:
        from codey.runs.text_clip import TRUNCATED_TEXT_SUFFIX, clip_text
        from codey.utils.text_budget import clip_tail

        self.assertEqual(TRUNCATED_TEXT_SUFFIX, "...")
        self.assertEqual(clip_text("", 10), "")
        self.assertEqual(clip_text("  hi  ", 10), "hi")
        self.assertEqual(clip_text("a\r\nb\rc", 10), "a\nb\nc")
        # small-limit branch returns raw prefix, NOT the clip_tail marker prefix
        self.assertEqual(clip_text("abcdef", 3), "abc")
        self.assertEqual(clip_text("abcdef", 2), "ab")
        self.assertNotEqual(clip_text("abcdef", 2), clip_tail("abcdef", 2))
        self.assertEqual(clip_text("abcdef", 6), "abcdef")
        # overflow branch rstrips before appending suffix
        self.assertEqual(clip_text("abcdef", 5), "ab...")
        self.assertEqual(clip_text("abc   defgh", 8), "abc...")


class Item4DeadCodeTests(unittest.TestCase):
    def test_dead_private_helpers_are_gone(self) -> None:
        import codey.storage.ui_state_store as u
        import codey.toolchain.runtime as r

        self.assertFalse(hasattr(u, "_version"))
        self.assertFalse(hasattr(r, "_line_body_without_eol"))


class Item5ApiFacadeTests(unittest.TestCase):
    def test_no_build_facade_in_api(self) -> None:
        import codey.app.api as api

        self.assertFalse(hasattr(api, "build_unified_research_graph"))
        source = Path(api.__file__).read_text(encoding="utf-8")
        self.assertNotIn("mock.patch.object", source)

    def test_importing_api_keeps_graph_stack_unloaded(self) -> None:
        # Low-cost guard against future regression: importing api must not
        # pull the knowledge graph stack. Checked in a fresh interpreter
        # because this process may already have concepts loaded.
        probe = (
            "import sys; "
            "import codey.app.api; "
            "loaded = [m for m in sys.modules "
            "if m == 'codey.knowledge.concepts' "
            "or m.startswith('codey.knowledge.concepts.')]; "
            "print('LOADED' if loaded else 'CLEAN')"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CLEAN", result.stdout)

    def test_graph_import_is_lazy_not_top_level(self) -> None:
        import ast

        import codey.app.api as api

        source = Path(api.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        top_level = [
            node
            for node in tree.body
            if isinstance(node, (ast.Import, ast.ImportFrom))
        ]
        for node in top_level:
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            else:
                names = [f"{node.module or ''}.{alias.name}" for alias in node.names]
            with self.subTest(import_names=names):
                self.assertFalse(
                    any("knowledge.concepts" in name for name in names),
                    f"top-level graph import would raise api startup cost: {names}",
                )
        self.assertIn(
            "from codey.knowledge.concepts import build_unified_research_graph",
            source,
        )

    def test_research_graph_response_uses_real_builder_lazily(self) -> None:
        from types import SimpleNamespace

        from codey.app import api as app_api
        from codey.app import server

        state = server.AppContext()
        state.knowledge_store = object()
        graph = SimpleNamespace(to_dict=lambda: {"nodes": [], "edges": []})
        with mock.patch(
            "codey.knowledge.concepts.build_unified_research_graph",
            return_value=graph,
        ) as build:
            status, payload = app_api.research_graph_response(
                state, {"session_id": ["s1"], "focus": ["f1"]}
            )
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"ok": True, "graph": {"nodes": [], "edges": []}})
        build.assert_called_once()
        state.close()


if __name__ == "__main__":
    unittest.main()
