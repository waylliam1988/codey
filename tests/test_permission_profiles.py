from __future__ import annotations

import unittest

from codey.permission_profiles import (
    KNOWN_CONTEXT_SOURCE_KEYS,
    KNOWN_REVIEW_CONTEXT_SOURCE_KEYS,
    PERMISSION_PROFILES,
    allowed_coding_tool_names,
    allows_context_source,
    profile_for_name,
    profile_for_task_kind,
)
from codey.research.tool_contract import TOOL_CONTRACTS as RESEARCH_TOOL_CONTRACTS
from codey.tool_definition import TOOL_DEFINITIONS


class PermissionProfileTests(unittest.TestCase):
    def test_expected_profiles_exist_and_are_internal(self) -> None:
        self.assertEqual(
            set(PERMISSION_PROFILES),
            {"chat", "research", "coding_writer", "reviewer", "planning_readonly"},
        )
        for profile in PERMISSION_PROFILES.values():
            with self.subTest(profile=profile.name):
                self.assertFalse(profile.user_visible)

    def test_coding_writer_allows_current_writer_tools_and_context(self) -> None:
        profile = profile_for_name("coding_writer")

        self.assertTrue(profile.project_read)
        self.assertTrue(profile.project_write)
        self.assertTrue(profile.can_request_shell)
        self.assertEqual(
            set(allowed_coding_tool_names(profile)),
            {definition.name for definition in TOOL_DEFINITIONS},
        )
        self.assertIn("edit", allowed_coding_tool_names(profile))
        self.assertIn("run", allowed_coding_tool_names(profile))
        self.assertIn("shell", allowed_coding_tool_names(profile))
        self.assertTrue(allows_context_source(profile, "coding_current_context"))
        self.assertFalse(allows_context_source(profile, "ghost_directive"))
        self.assertFalse(allows_context_source(profile, "ghost_continuity"))

    def test_planning_readonly_excludes_mutating_and_verification_tools(self) -> None:
        tools = set(allowed_coding_tool_names("planning_readonly"))

        self.assertEqual(
            tools,
            {
                "list_dir",
                "read_file",
                "read_files",
                "grep",
                "find_references",
                "parallel",
                "done",
            },
        )
        self.assertFalse(profile_for_name("planning_readonly").project_write)
        self.assertNotIn("edit", tools)
        self.assertNotIn("run", tools)
        self.assertNotIn("shell", tools)
        self.assertTrue(allows_context_source("planning_readonly", "ghost_directive"))
        self.assertTrue(allows_context_source("planning_readonly", "ghost_continuity"))

    def test_reviewer_and_research_profiles_do_not_write_projects(self) -> None:
        reviewer = profile_for_name("reviewer")
        research = profile_for_name("research")

        self.assertFalse(reviewer.project_write)
        self.assertEqual(allowed_coding_tool_names(reviewer), ())
        self.assertFalse(allows_context_source(reviewer, "ghost_directive"))
        self.assertFalse(allows_context_source(reviewer, "ghost_continuity"))
        self.assertFalse(allows_context_source(reviewer, "research_topic_continuity"))
        self.assertFalse(research.project_write)
        self.assertEqual(allowed_coding_tool_names(research), ())
        self.assertEqual(set(research.research_tools), set(RESEARCH_TOOL_CONTRACTS))
        self.assertFalse(allows_context_source(research, "ghost_directive"))
        self.assertFalse(allows_context_source(research, "ghost_continuity"))
        # Research admits only its own bounded topic-continuity source.
        self.assertTrue(allows_context_source(research, "research_topic_continuity"))

    def test_research_topic_continuity_stays_out_of_chat_and_writer_profiles(self) -> None:
        for name in ("chat", "coding_writer", "planning_readonly", "reviewer"):
            with self.subTest(profile=name):
                self.assertFalse(
                    allows_context_source(name, "research_topic_continuity")
                )

    def test_profile_context_keys_are_known(self) -> None:
        for profile in PERMISSION_PROFILES.values():
            with self.subTest(profile=profile.name):
                self.assertLessEqual(set(profile.context_sources), KNOWN_CONTEXT_SOURCE_KEYS)
                self.assertLessEqual(
                    set(profile.review_context_sources),
                    KNOWN_REVIEW_CONTEXT_SOURCE_KEYS,
                )

    def test_task_kind_mapping_is_internal_and_stable(self) -> None:
        self.assertEqual(profile_for_task_kind("chat").name, "chat")
        self.assertEqual(profile_for_task_kind("research").name, "research")
        self.assertEqual(profile_for_task_kind("project").name, "coding_writer")
        self.assertEqual(profile_for_task_kind("research", phase="review").name, "reviewer")
        self.assertEqual(profile_for_task_kind("hybrid", phase="research").name, "research")
        self.assertEqual(profile_for_task_kind("hybrid", phase="writer").name, "coding_writer")
        self.assertEqual(profile_for_task_kind("project", phase="review").name, "reviewer")
        self.assertEqual(
            profile_for_task_kind("project", phase="readonly").name,
            "planning_readonly",
        )

    def test_completion_repair_context_is_coding_writer_only(self) -> None:
        # 0.4.13: bounded failure facts are admitted only into the coding
        # writer phase. Chat, research, reviewer, and planning never see it.
        for name in ("coding_writer",):
            with self.subTest(profile=name):
                self.assertTrue(allows_context_source(profile_for_name(name), "completion_repair_context"))
        for name in ("chat", "research", "reviewer", "planning_readonly"):
            with self.subTest(profile=name):
                self.assertFalse(allows_context_source(profile_for_name(name), "completion_repair_context"))
        self.assertIn("completion_repair_context", KNOWN_CONTEXT_SOURCE_KEYS)


if __name__ == "__main__":
    unittest.main()
