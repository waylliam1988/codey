"""Deterministic tests for safe tool replay validation and candidate projection."""

from __future__ import annotations

import unittest

from codey.runtime.effect_records import (
    EFFECT_CATEGORY_PROVIDER_SEND,
    EFFECT_CATEGORY_REPAIR_ROUND,
    EFFECT_CATEGORY_TOOL_CALL,
    RuntimeEffectError,
    RuntimeEffectIntent,
    RuntimeEffectProjection,
    RuntimeEffectSettlement,
    SETTLEMENT_STATUS_OK,
)
from codey.runtime.models import ToolCall
from codey.runtime.replay_policy import (
    REPLAYABLE_SAFE_TOOL_NAMES,
    ReplayClass,
    SAFE_TOOL_NAMES,
    is_replayable_safe_tool,
)
from codey.runtime.safe_tool_replay import (
    candidate_from_effect,
    replay_args_for_tool_call,
    validate_replay_args,
)


class SafeToolReplayTests(unittest.TestCase):
    def test_replayable_safe_tool_whitelist(self) -> None:
        expected = {"read", "ls", "search", "references"}
        self.assertEqual(REPLAYABLE_SAFE_TOOL_NAMES, frozenset(expected))
        for name in expected:
            self.assertTrue(is_replayable_safe_tool(name))
            self.assertIn(name, SAFE_TOOL_NAMES)

        # project_facts and project_map are safe, but NOT replayable in 0.5.4
        self.assertIn("project_facts", SAFE_TOOL_NAMES)
        self.assertIn("project_map", SAFE_TOOL_NAMES)
        self.assertFalse(is_replayable_safe_tool("project_facts"))
        self.assertFalse(is_replayable_safe_tool("project_map"))

        # Unsafe tools are not replayable
        for name in ("edit", "write", "run", "shell", "unknown_tool", ""):
            self.assertFalse(is_replayable_safe_tool(name))

    def test_validate_replay_args_canonical_success(self) -> None:
        # read with offset/limit
        read_args = validate_replay_args("read", {"path": "foo/bar.py", "offset": 10, "limit": 20})
        self.assertEqual(read_args, {"path": "foo/bar.py", "offset": 10, "limit": 20})

        # ls
        ls_args = validate_replay_args("ls", {"path": "src"})
        self.assertEqual(ls_args, {"path": "src"})

        # search
        search_args = validate_replay_args("search", {"path": ".", "query": "def test"})
        self.assertEqual(search_args, {"path": ".", "query": "def test"})

        # references
        ref_args = validate_replay_args("references", {"path": "main.py", "symbol": "foo"})
        self.assertEqual(ref_args, {"path": "main.py", "symbol": "foo"})

    def test_validate_replay_args_rejects_alias_rewrites(self) -> None:
        # 'cwd' is an alias for 'path' in read, triggers alias_rewrite_count > 0
        with self.assertRaises(RuntimeEffectError) as ctx:
            validate_replay_args("read", {"cwd": "foo/bar.py"})
        self.assertIn("invalid replay args", str(ctx.exception))

        # 'pattern' is an alias for 'query' in search
        with self.assertRaises(RuntimeEffectError) as ctx:
            validate_replay_args("search", {"path": ".", "pattern": "foo"})
        self.assertIn("invalid replay args", str(ctx.exception))

        # String coerced numeric offset triggers repair count
        with self.assertRaises(RuntimeEffectError) as ctx:
            validate_replay_args("read", {"path": "foo.py", "offset": "10"})
        self.assertIn("invalid replay args", str(ctx.exception))

        # Unsupported unknown fields fail closed with RuntimeEffectError
        with self.assertRaises(RuntimeEffectError) as ctx:
            validate_replay_args("read", {"file": "foo/bar.py"})
        self.assertIn("invalid replay args", str(ctx.exception))

    def test_validate_replay_args_rejects_unsafe_and_unknown_tools(self) -> None:
        with self.assertRaises(RuntimeEffectError) as ctx:
            validate_replay_args("edit", {"path": "foo.py", "replacements": []})
        self.assertIn("not a replayable safe tool", str(ctx.exception))

        with self.assertRaises(RuntimeEffectError) as ctx:
            validate_replay_args("project_facts", {})
        self.assertIn("not a replayable safe tool", str(ctx.exception))

    def test_replay_args_for_tool_call(self) -> None:
        # Valid safe call
        call_safe = ToolCall(name="read", args={"path": "a.txt", "offset": 1})
        args = replay_args_for_tool_call(call_safe)
        self.assertEqual(args, {"path": "a.txt", "offset": 1})

        # Safe tool with alias returns None
        call_alias = ToolCall(name="read", args={"file": "a.txt"})
        self.assertIsNone(replay_args_for_tool_call(call_alias))

        # Unsafe call returns None
        call_unsafe = ToolCall(name="edit", args={"path": "a.txt"})
        self.assertIsNone(replay_args_for_tool_call(call_unsafe))

    def test_candidate_from_effect_valid(self) -> None:
        intent = RuntimeEffectIntent(
            effect_id="eff_1",
            effect_category=EFFECT_CATEGORY_TOOL_CALL,
            session_id="sess_1",
            run_id="run_1",
            turn=2,
            tool_index=1,
            tool_name="search",
            replay_class=ReplayClass.SAFE,
            replay_args={"path": "pkg", "query": "my_func"},
        )
        proj = RuntimeEffectProjection(intent=intent)
        candidate = candidate_from_effect(proj)
        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate.effect_id, "eff_1")
        self.assertEqual(candidate.turn, 2)
        self.assertEqual(candidate.tool_index, 1)
        self.assertEqual(candidate.call.name, "search")
        self.assertEqual(candidate.call.args, {"path": "pkg", "query": "my_func"})

    def test_candidate_from_effect_settled_returns_none(self) -> None:
        intent = RuntimeEffectIntent(
            effect_id="eff_1",
            effect_category=EFFECT_CATEGORY_TOOL_CALL,
            session_id="sess_1",
            run_id="run_1",
            turn=1,
            tool_index=0,
            tool_name="read",
            replay_class=ReplayClass.SAFE,
            replay_args={"path": "pkg/file.py"},
        )
        settlement = RuntimeEffectSettlement(
            effect_id="eff_1",
            effect_category=EFFECT_CATEGORY_TOOL_CALL,
            session_id="sess_1",
            run_id="run_1",
            status=SETTLEMENT_STATUS_OK,
            replay_class=ReplayClass.SAFE,
        )
        proj = RuntimeEffectProjection(intent=intent, settlement=settlement)
        self.assertIsNone(candidate_from_effect(proj))

    def test_candidate_from_effect_unsafe_or_missing_args_returns_none(self) -> None:
        # Unsafe tool
        intent_unsafe = RuntimeEffectIntent(
            effect_id="eff_2",
            effect_category=EFFECT_CATEGORY_TOOL_CALL,
            session_id="sess_1",
            run_id="run_1",
            tool_name="edit",
            replay_class=ReplayClass.UNSAFE,
        )
        self.assertIsNone(candidate_from_effect(RuntimeEffectProjection(intent=intent_unsafe)))

        # Provider send category
        intent_prov = RuntimeEffectIntent(
            effect_id="eff_3",
            effect_category=EFFECT_CATEGORY_PROVIDER_SEND,
            session_id="sess_1",
            run_id="run_1",
            provider_id="mock",
        )
        self.assertIsNone(candidate_from_effect(RuntimeEffectProjection(intent=intent_prov)))

        # Repair round category
        intent_rep = RuntimeEffectIntent(
            effect_id="eff_4",
            effect_category=EFFECT_CATEGORY_REPAIR_ROUND,
            session_id="sess_1",
            run_id="run_1",
        )
        self.assertIsNone(candidate_from_effect(RuntimeEffectProjection(intent=intent_rep)))

        # Safe tool with None replay_args (e.g. legacy intent)
        intent_legacy = RuntimeEffectIntent(
            effect_id="eff_5",
            effect_category=EFFECT_CATEGORY_TOOL_CALL,
            session_id="sess_1",
            run_id="run_1",
            tool_name="read",
            replay_class=ReplayClass.SAFE,
            replay_args=None,
        )
        self.assertIsNone(candidate_from_effect(RuntimeEffectProjection(intent=intent_legacy)))


if __name__ == "__main__":
    unittest.main()
