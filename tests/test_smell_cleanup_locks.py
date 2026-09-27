"""Deterministic locks for the five smell cleanups + prompt follow-up (TDD red-first).

Each test pins a deterministic bug/shape before the fix so the fix can be
verified without a full-suite run. Golden prompt fixtures remain the
byte-parity guard for writer/planning_readonly.
"""
from __future__ import annotations

import inspect
import unittest


class ToolPromptUnificationLocks(unittest.TestCase):
    def test_restricted_toolset_yields_readonly_rules(self) -> None:
        """Rules follow allowed_tool_names (no dead profile_name label)."""
        from codey.toolchain import definition as tool_defs
        from codey.toolchain.tool_prompt import render_coding_system_prompt

        readonly_names = {
            "list_dir", "read_file", "read_files", "grep",
            "find_references", "parallel", "done",
        }
        defs = tool_defs.definitions_for_tool_names(readonly_names)
        prompt = render_coding_system_prompt(
            defs,
            allowed_tool_names=set(readonly_names),
        )
        # A readonly toolset must not emit writer-only rules.
        self.assertNotIn("Use edit for all file changes", prompt)
        self.assertNotIn("Use run only for verification", prompt)
        self.assertIn("This phase is read-only", prompt)

    def test_single_render_path_without_legacy_system_prompt(self) -> None:
        """RED before fix: legacy _system_prompt still exists."""
        import codey.toolchain.tool_prompt as tool_prompt

        self.assertFalse(
            hasattr(tool_prompt, "_system_prompt"),
            "legacy _system_prompt must be removed; single unified renderer only",
        )


class ToolPromptSingleSourceLocks(unittest.TestCase):
    def test_shared_rule_fragments_are_single_source(self) -> None:
        """RED before follow-up: writer lines duplicate builder strings."""
        import codey.toolchain.tool_prompt as tool_prompt

        for attr in (
            "_RULE_OUTPUT_JSON",
            "_RULE_NATIVE_DENIAL",
            "_RULE_READFILE_TRAILING",
            "_RULE_EDIT_BLOCK",
            "_RULE_PATHS",
            "_RULE_NO_REPEAT",
            "_RULE_RUN_VERIFICATION",
            "_RULE_TOOL_RESULT",
            "_RULE_DONE",
        ):
            self.assertTrue(
                hasattr(tool_prompt, attr),
                f"shared fragment {attr} must exist exactly once",
            )
        fragments: list[str] = []
        for attr in (
            "_RULE_OUTPUT_JSON",
            "_RULE_NATIVE_DENIAL",
            "_RULE_EDIT_BLOCK",
            "_RULE_PATHS",
            "_RULE_DONE",
        ):
            fragments.extend(getattr(tool_prompt, attr))
        for line in fragments:
            self.assertIn(line, tool_prompt._WRITER_RULE_LINES)

    def test_writer_detection_requires_exact_match(self) -> None:
        """RED before follow-up: subset check accepts a superset as writer."""
        from codey.toolchain.tool_prompt import _WRITER_TOOL_NAMES, _is_full_writer_toolset

        superset = set(_WRITER_TOOL_NAMES) | {"bogus_extra_tool"}
        self.assertFalse(
            _is_full_writer_toolset(superset),
            "writer detection must be an exact match, not a subset check",
        )
        self.assertTrue(_is_full_writer_toolset(set(_WRITER_TOOL_NAMES)))

    def test_inconsistent_allowed_and_definitions_raise(self) -> None:
        """RED before follow-up: allowed-full + definitions-missing-edit silently demands edit."""
        from codey.toolchain import definition as tool_defs
        from codey.toolchain.tool_prompt import _WRITER_TOOL_NAMES, render_coding_system_prompt

        allowed = set(_WRITER_TOOL_NAMES)
        without_edit = [d for d in tool_defs.TOOL_DEFINITIONS if d.name != "edit"]
        self.assertTrue(any(d.name == "edit" for d in tool_defs.TOOL_DEFINITIONS))
        with self.assertRaises(ValueError):
            render_coding_system_prompt(
                tuple(without_edit),
                allowed_tool_names=set(allowed),
            )


class UiStateCanonicalShapeLocks(unittest.TestCase):
    def test_sessions_always_carry_research_shape(self) -> None:
        """RED before fix: missing keys stay missing instead of []/False."""
        import tempfile

        from codey.storage.ui_state_store import UiStateStore

        with tempfile.TemporaryDirectory() as td:
            store = UiStateStore(td)
            store.save({
                "active_id": "chat-1",
                "updated_at": 1,
                "revision": 0,
                "sessions": [{
                    "id": "chat-1",
                    "title": "hello",
                    "messages": [],
                    "terminalRuns": [],
                    "createdAt": 0,
                    "projectId": None,
                    "provider": "deepseek",
                }],
                "projects": [],
            }, base_revision=0)
            session = store.load()["sessions"][0]
            self.assertIn("researchRuns", session)
            self.assertEqual(session["researchRuns"], [])
            self.assertIn("research", session)
            self.assertEqual(session["research"], False)

    def test_clean_sessions_is_shape_stable(self) -> None:
        """RED before fix: omitted keys vs explicit []/False compare unequal."""
        from codey.storage.ui_state_store import _clean_sessions

        base = {
            "id": "s",
            "title": "t",
            "messages": [],
            "terminalRuns": [],
            "createdAt": 0,
            "projectId": None,
            "provider": "deepseek",
        }
        without = _clean_sessions([dict(base)])[0]
        with_explicit = _clean_sessions([dict(base, researchRuns=[], research=False)])[0]
        self.assertEqual(without, with_explicit)


class GhostWarningsUnificationLocks(unittest.TestCase):
    def test_no_legacy_slice_event_warnings(self) -> None:
        """RED before fix: legacy slice path still exists in _warnings."""
        import codey.ghost._warnings as warnings_mod

        self.assertFalse(
            hasattr(warnings_mod, "slice_event_warnings"),
            "slice_event_warnings is legacy and must be removed",
        )
        self.assertNotIn("slice_event_warnings", set(warnings_mod.__all__))

    def test_inbox_and_hebbian_use_shared_bounded_loop(self) -> None:
        """RED before fix: inbox/hebbian keep duplicates/empties/unclipped."""
        from codey.ghost import hebbian, inbox
        from codey.ghost._warnings import event_read_warnings

        sample = [
            "hebbian_events.jsonl:too_large",
            "hebbian_events.jsonl:too_large",
            "",
            "x" * 500,
            "custom-warning",
            "custom-warning",
        ]
        self.assertEqual(
            hebbian._event_read_warnings(sample),
            event_read_warnings(
                sample, stream="hebbian_events", limit=hebbian.MAX_HEBBIAN_WARNINGS
            ),
        )
        inbox_sample = [
            "events.jsonl:unreadable",
            "events.jsonl:unreadable",
            "",
            "y" * 500,
            "custom-warning",
            "custom-warning",
        ]
        self.assertEqual(
            inbox._event_read_warnings(inbox_sample),
            event_read_warnings(
                inbox_sample, stream="events", limit=inbox.MAX_EVENT_WARNINGS
            ),
        )
        # The shared loop must dedupe, drop empties, and clip to 180 chars.
        cleaned = event_read_warnings(
            sample, stream="hebbian_events", limit=hebbian.MAX_HEBBIAN_WARNINGS
        )
        self.assertNotIn("", cleaned)
        self.assertEqual(len(cleaned), len(set(cleaned)))
        self.assertTrue(all(len(item) <= 180 for item in cleaned))


class ShellApprovalFallbackLocks(unittest.TestCase):
    def test_stopped_denial_has_no_command_param(self) -> None:
        """RED before fix: _stopped_shell_denial takes a dead command arg."""
        from codey.app import api as app_api

        params = set(inspect.signature(app_api._stopped_shell_denial).parameters)
        self.assertNotIn("command", params)

    def test_no_dead_pending_or_command_fallback(self) -> None:
        """RED before fix: 'pending or {\"command\": command}' is unreachable."""
        from pathlib import Path

        source = Path("codey/app/api.py").read_text(encoding="utf-8")
        self.assertNotIn('pending or {"command": command}', source)

    def test_no_unused_command_locals_in_shell_response(self) -> None:
        """RED before fix: command locals exist only for the dead fallback."""
        from pathlib import Path

        source = Path("codey/app/api.py").read_text(encoding="utf-8")
        self.assertNotIn('command = pending["command"]', source)


if __name__ == "__main__":
    unittest.main()
