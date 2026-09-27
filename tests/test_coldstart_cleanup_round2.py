"""Cold-start cleanup round2 locks (red-first).

Covers the four cleanups requested under "cold start, keep real runtime
fault-tolerance":

1. provider dead fields are gone, live fields keep working;
2. prompt trace uses only ``record_provider_prompt_boundary``;
3. ghost CLI has a single parser entry;
4. strict ``_nonnegative_int`` lives once in ``codey.utils.refs``.

These tests assert the CLEANED state, so they fail on the pre-cleanup code
(reproducing the problem) and pass after the cleanup.
"""
from __future__ import annotations

import dataclasses
import unittest


class ProviderDeadFieldsGoneTests(unittest.TestCase):
    def test_dead_capability_fields_are_removed(self) -> None:
        from codey.providers import capabilities as cap

        dead = (
            "json_reliability",
            "context_budget_hint",
            "native_tool_interference_risk",
            "needs_canary_by_default",
            "failure_families",
            "tool_protocol",
            "max_tools_per_turn",
        )
        names = {f.name for f in dataclasses.fields(cap.ProviderCapability)}
        for field_name in dead:
            with self.subTest(field=field_name):
                self.assertNotIn(field_name, names)

    def test_reliability_helpers_are_removed(self) -> None:
        from codey.providers import capabilities as cap

        for name in ("Reliability", "RELIABILITY_HIGH", "RELIABILITY_MEDIUM", "RELIABILITY_LOW"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(cap, name), f"{name} should be removed")
        self.assertNotIn("Reliability", cap.__all__)
        for name in ("RELIABILITY_HIGH", "RELIABILITY_MEDIUM", "RELIABILITY_LOW"):
            with self.subTest(export=name):
                self.assertNotIn(name, cap.__all__)

    def test_live_capability_fields_still_work(self) -> None:
        from codey.providers.capabilities import capability_for, rank_providers

        cap = capability_for("mimo")
        # live ranking fields
        self.assertEqual(cap.research_fit, "avoid")
        self.assertEqual(cap.coding_fit, "ok")
        # live native/context fields
        local = capability_for("local")
        self.assertTrue(local.supports_native_tools)
        self.assertTrue(local.native_tools_default)
        self.assertGreater(local.context_window_tokens, 0)
        self.assertGreater(local.context_reserve_tokens, 0)
        self.assertGreater(local.context_keep_recent_tokens, 0)
        # ranking still works
        self.assertEqual(
            rank_providers(("mimo", "stepfun", "deepseek"), "research"),
            ("stepfun", "deepseek", "mimo"),
        )


class PromptBoundaryOnlyTests(unittest.TestCase):
    def test_no_legacy_fallback_or_inspect_helpers(self) -> None:
        import inspect as std_inspect

        import codey.runtime.observe.prompt_envelope as env

        source = std_inspect.getsource(env.record_provider_send_prompt)
        self.assertNotIn("record_prompt_section", source)
        self.assertNotIn("record_prompt_surface", source)
        self.assertNotIn("getattr_static", source)
        self.assertNotIn("FailOpenPromptTrace", source)
        # module no longer needs inspect or the dead cancellation helper
        self.assertFalse(hasattr(env, "_is_trace_cancellation"))
        self.assertNotIn("import inspect", std_inspect.getsource(env))

    def test_old_style_trace_records_nothing(self) -> None:
        from codey.runtime.observe.prompt_envelope import record_provider_send_prompt

        class OldStyleTrace:
            def __init__(self) -> None:
                self.sections: list = []
                self.surfaces: list = []

            def record_prompt_section(self, name, text, **kwargs) -> None:
                self.sections.append({"name": name, "text": text, **kwargs})

            def record_prompt_surface(self, payload) -> None:
                self.surfaces.append(dict(payload))

        trace = OldStyleTrace()
        record_provider_send_prompt(
            trace,
            name="coding_outbound_prompt",
            text="hello",
            purpose="p",
            source_ref="provider_send:coding",
            phase="writer",
            send_ref="effect_1",
        )
        self.assertEqual(trace.sections, [])
        self.assertEqual(trace.surfaces, [])

    def test_boundary_trace_still_records_and_fail_open(self) -> None:
        import tempfile
        from pathlib import Path

        from codey.runs.trace import RunTraceStore
        from codey.runtime.core import cancellation
        from codey.runtime.observe.prompt_envelope import record_provider_send_prompt

        # None trace returns silently
        record_provider_send_prompt(
            None, name="x", text="y", purpose="p", source_ref="provider_send:coding"
        )

        # real recorder still records section+surface
        with tempfile.TemporaryDirectory() as td:
            store = RunTraceStore(Path(td))
            recorder = store.open(
                run_id="run-lock",
                session_id="sess-lock",
                project=None,
                mode_initial="project",
                provider_initial="deepseek",
            )
            record_provider_send_prompt(
                recorder,
                name="coding_outbound_prompt",
                text="outbound",
                purpose="p",
                source_ref="provider_send:test",
                phase="writer",
                send_ref="eff_lock_1",
            )
            recorder.finish(status="done")
            import json

            obj = json.loads(store.path_for("sess-lock", "run-lock").read_text(encoding="utf-8"))
            self.assertEqual(len(obj["prompt_sections"]), 1)
            self.assertEqual(len(obj["prompt_surfaces"]), 1)

        # boundary failure is fail-open
        class BrokenBoundary:
            def record_provider_prompt_boundary(self, *a, **k) -> None:
                raise OSError("trace unavailable")

        record_provider_send_prompt(
            BrokenBoundary(),
            name="x",
            text="y",
            purpose="p",
            source_ref="provider_send:coding",
            phase="writer",
            send_ref="effect_1",
        )

        # cancellation still propagates
        class StoppingBoundary:
            def record_provider_prompt_boundary(self, *a, **k) -> None:
                raise cancellation.TaskCancelled("stop")

        with self.assertRaises(cancellation.TaskCancelled):
            record_provider_send_prompt(
                StoppingBoundary(),
                name="x",
                text="y",
                purpose="p",
                source_ref="provider_send:coding",
                phase="writer",
                send_ref="effect_1",
            )


class GhostSingleParserTests(unittest.TestCase):
    def test_no_main_ghost_split(self) -> None:
        import codey.app.cli as cli

        self.assertFalse(hasattr(cli, "_main_ghost"), "_main_ghost should be removed")

    def test_ghost_parses_through_main_parser(self) -> None:
        import inspect as std_inspect

        import codey.app.cli as cli

        # main parser must wire ghost subcommands (required) via _add_ghost_subcommands
        source = std_inspect.getsource(cli.main)
        self.assertNotIn("_main_ghost", source)
        self.assertIn("_add_ghost_subcommands", source)
        # ghost parser exists and requires a subcommand; list parses to cmd_ghost
        # Exercise via main() with mocked func to avoid touching real stores.
        from unittest import mock

        with mock.patch.object(cli, "cmd_ghost", return_value=0) as func:
            code = cli.main(["ghost", "list", "--status", ""])
            self.assertEqual(code, 0)
            self.assertEqual(func.call_count, 1)
            args = func.call_args.args[0]
            self.assertEqual(args.ghost_cmd, "list")

    def test_ghost_missing_subcommand_exits_2(self) -> None:
        import codey.app.cli as cli

        with self.assertRaises(SystemExit) as ctx:
            cli.main(["ghost"])
        self.assertEqual(ctx.exception.code, 2)

    def test_ghost_registered_once_with_deferred_agent_default(self) -> None:
        import inspect as std_inspect
        import pathlib

        import codey.app.cli as cli

        cli_path = pathlib.Path(cli.__file__)
        cli_source = cli_path.read_text(encoding="utf-8")
        # Single registration: one def + exactly one call site.
        self.assertEqual(cli_source.count("def _add_ghost_subcommands"), 1)
        self.assertEqual(cli_source.count("_add_ghost_subcommands("), 2)
        # No ghost-only fast-path split and no extra parser builder.
        self.assertFalse(hasattr(cli, "_build_ghost_only_parser"))
        main_source = std_inspect.getsource(cli.main)
        self.assertNotIn('_build_ghost_only_parser', main_source)
        self.assertNotIn('argv[0] == "ghost"', main_source)
        # Agent default must not be fetched at parser-build time (loads toolchain).
        self.assertNotIn("from codey.agents.request import DEFAULT_MAX_TURNS", main_source)
        cmd_agent_source = std_inspect.getsource(cli.cmd_agent)
        self.assertIn("DEFAULT_MAX_TURNS", cmd_agent_source)


class StrictNonnegativeIntSharedTests(unittest.TestCase):
    def test_strict_helper_lives_once_in_refs(self) -> None:
        from codey.utils import refs as refs_mod

        self.assertTrue(hasattr(refs_mod, "strict_nonnegative_int"))
        self.assertIn("strict_nonnegative_int", refs_mod.__all__)
        # loose helper must keep bool->1 semantics (cannot replace strict)
        self.assertEqual(refs_mod.nonnegative_int(True), 1)
        self.assertEqual(refs_mod.strict_nonnegative_int(True), 0)

    def test_strict_semantics_match_legacy_copies(self) -> None:
        from codey.utils.refs import strict_nonnegative_int as strict

        cases: list[tuple[object, int]] = [
            (True, 0),
            (False, 0),
            (5, 5),
            (-3, 0),
            (3.9, 3),
            (-2.5, 0),
            (float("nan"), 0),
            (float("inf"), 0),
            (float("-inf"), 0),
            ("  12  ", 12),
            ("007", 7),
            ("+12", 0),
            ("-5", 0),
            ("12.0", 0),
            ("", 0),
            (None, 0),
            ([], 0),
        ]
        for value, expected in cases:
            with self.subTest(value=repr(value)):
                self.assertEqual(strict(value), expected)

    def test_no_local_duplicates_remain(self) -> None:
        import inspect as std_inspect

        import codey.policies.action as action_mod
        import codey.runtime.core.models as models_mod

        self.assertNotIn("_nonnegative_int", dir(action_mod))
        self.assertNotIn("_nonnegative_int", dir(models_mod))
        # they must import the shared helper instead
        self.assertIn("strict_nonnegative_int", std_inspect.getsource(action_mod))
        self.assertIn("strict_nonnegative_int", std_inspect.getsource(models_mod))

    def test_strict_unicode_superscript_is_fail_closed(self) -> None:
        from codey.policies.action import ActionSubject
        from codey.runtime.core.models import normalized_managed_output
        from codey.utils.refs import strict_nonnegative_int as strict

        # "²".isdigit() is True but int("²") raises ValueError; must not propagate.
        self.assertTrue("²".isdigit())
        self.assertEqual(strict("²"), 0)
        self.assertEqual(strict("  ²  "), 0)
        self.assertEqual(ActionSubject(kind="read_file", byte_count="²").byte_count, 0)
        payload = normalized_managed_output({
            "handle": "out_abc",
            "original_bytes": "²",
            "stored_bytes": "²",
            "sha256": "a" * 64,
            "original_sha256": "",
            "stored_truncated": False,
        })
        self.assertEqual(payload["original_bytes"], 0)
        self.assertEqual(payload["stored_bytes"], 0)

    def test_loose_helper_doc_matches_real_behavior(self) -> None:
        import inspect as std_inspect

        from codey.utils import refs as refs_mod

        # Loose accepts "+12" via int() but rejects "12.0" (ValueError -> 0).
        self.assertEqual(refs_mod.nonnegative_int("+12"), 12)
        self.assertEqual(refs_mod.nonnegative_int("12.0"), 0)
        doc = std_inspect.getsource(refs_mod.strict_nonnegative_int)
        # Doc must not claim loose accepts "12.0"; it must state the split.
        self.assertNotIn('accepts ``"+12"``/``"12.0"``', doc)
        self.assertIn('"12.0"', doc)
        self.assertIn("reject", doc.lower())


if __name__ == "__main__":
    unittest.main()
