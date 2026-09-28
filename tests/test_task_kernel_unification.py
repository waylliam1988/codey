"""Red-first locks for the unified task kernel (TDD, no behavior change yet).

Each test pins one deterministic defect named in the unification plan:

1. ToolRegistry.snapshot fails open (unknown profile / empty set -> all tools).
2. build_completion_contract silently truncates beyond MAX_COMPLETION_CHECKS.
3. Completion domains are a closed list with no registration path.
4. No persistent, explainable TaskPolicy; model_hint could be mistaken for grants.
5. Research controller priority state would ban project tools in a hybrid run.
6. done has two arg names (summary vs answer) with no single reader.
7. hybrid forces fresh_chat + drops the research handoff.
"""

from __future__ import annotations

import unittest


class RegistryFailClosedTests(unittest.TestCase):
    def test_unknown_profile_snapshot_is_empty_not_all(self) -> None:
        from codey.toolchain.registry import ToolRegistry

        snapshot = ToolRegistry().snapshot(profile_name="definitely_unknown_profile_xyz", mode="coding")
        self.assertEqual(tuple(snapshot.definitions), ())
        self.assertEqual(tuple(snapshot.names), ())

    def test_profile_without_coding_tools_snapshots_empty(self) -> None:
        from codey.toolchain.registry import ToolRegistry

        # The research permission profile owns no coding tools; a coding
        # snapshot for it must be empty, never the full writer set.
        snapshot = ToolRegistry().snapshot(profile_name="research", mode="coding")
        self.assertEqual(tuple(snapshot.names), ())


class CompletionOverflowTests(unittest.TestCase):
    def test_overflow_checks_fail_closed_instead_of_silent_truncate(self) -> None:
        from codey.completion.contract import (
            CHECK_PASS,
            MAX_COMPLETION_CHECKS,
            build_completion_contract,
            completion_check,
        )

        rows = [completion_check(f"check_{i}", CHECK_PASS) for i in range(MAX_COMPLETION_CHECKS + 1)]
        self.assertEqual(len([r for r in rows if r is not None]), MAX_COMPLETION_CHECKS + 1)
        contract = build_completion_contract(
            domain="coding",
            subject_ref="run:overflow",
            checks=rows,
        )
        self.assertIsNone(contract)

    def test_completion_domain_registration_opens_new_task_kind(self) -> None:
        from codey.completion import contract as contract_mod

        self.assertTrue(hasattr(contract_mod, "register_completion_domain"))
        contract_mod.register_completion_domain("planning")
        contract = contract_mod.build_completion_contract(
            domain="planning",
            subject_ref="run:planning-1",
            checks=[contract_mod.completion_check("plan_present", "pass")],
        )
        self.assertIsNotNone(contract)
        self.assertEqual(contract.domain, "planning")


class TaskPolicyTests(unittest.TestCase):
    def _submission(self, **kwargs):
        from codey.task.model import TaskSubmission

        base = {
            "session_id": "s1",
            "project": "demo",
            "task": "fix the login bug",
            "max_turns": 8,
            "continue_task": False,
            "provider_id": "local",
        }
        base.update(kwargs)
        return TaskSubmission(**base)

    def test_model_hint_cannot_grant_web(self) -> None:
        from codey.policies.task_policy import build_task_policy

        submission = self._submission(
            task="fix the login bug",
            model_hint="please use web_search for docs",
        )
        policy = build_task_policy(submission, task_kind="project", strict_research=False)
        self.assertFalse(policy.allows("web.read"))

    def test_explicit_web_capability_grants_web(self) -> None:
        from codey.policies.task_policy import build_task_policy

        submission = self._submission(
            task="fix the login bug; check official docs at https://example.com",
            requested_capabilities=("web.read",),
        )
        policy = build_task_policy(submission, task_kind="project", strict_research=False)
        self.assertTrue(policy.allows("web.read"))

    def test_unknown_capability_is_denied(self) -> None:
        from codey.policies.task_policy import build_task_policy

        submission = self._submission(requested_capabilities=("fly.to.moon",))
        policy = build_task_policy(submission, task_kind="project", strict_research=False)
        self.assertFalse(policy.allows("fly.to.moon"))
        self.assertFalse(policy.allows("project.write") and "fly.to.moon" in policy.grants)

    def test_research_with_project_is_readable_but_not_writable_by_default(self) -> None:
        from codey.policies.task_policy import build_task_policy

        submission = self._submission(task="research caching strategies")
        policy = build_task_policy(submission, task_kind="research", strict_research=True)
        self.assertTrue(policy.strict_research)
        self.assertTrue(policy.allows("web.read"))
        self.assertTrue(policy.allows("project.read"))
        self.assertTrue(policy.allows("project.verify"))
        self.assertFalse(policy.allows("project.write"))

    def test_research_write_requires_explicit_request(self) -> None:
        from codey.policies.task_policy import build_task_policy

        submission = self._submission(
            task="research then fix the bug in the repo",
            requested_capabilities=("project.write",),
        )
        policy = build_task_policy(submission, task_kind="research", strict_research=True)
        self.assertTrue(policy.allows("project.write"))

    def test_no_project_grants_no_project_tools(self) -> None:
        from codey.policies.task_policy import build_task_policy

        submission = self._submission(project=None, task="explain recursion")
        policy = build_task_policy(submission, task_kind="project", strict_research=False)
        self.assertFalse(policy.allows("project.read"))
        self.assertFalse(policy.allows("project.write"))
        self.assertFalse(policy.allows("project.verify"))

    def test_policy_round_trips_for_recovery(self) -> None:
        from codey.policies.task_policy import build_task_policy

        submission = self._submission(requested_capabilities=("web.read",))
        policy = build_task_policy(submission, task_kind="project", strict_research=False)
        payload = policy.to_payload()
        from codey.policies.task_policy import TaskPolicy

        revived = TaskPolicy.from_payload(payload)
        self.assertEqual(revived, policy)


class HybridScopeTests(unittest.TestCase):
    def test_priority_controller_state_does_not_ban_project_tools(self) -> None:
        from codey.policies.task_policy import build_task_policy
        from codey.task.model import TaskSubmission
        from codey.toolchain.tool_spec import visible_tool_names_for_snapshot

        submission = TaskSubmission(
            session_id="s1",
            project="demo",
            task="check docs then fix bug",
            max_turns=8,
            continue_task=False,
            provider_id="local",
            requested_capabilities=("web.read",),
        )
        policy = build_task_policy(submission, task_kind="hybrid", strict_research=False)
        # Controller in priority mode only allows open_result among research
        # tools; project tools must stay visible for the hybrid run.
        # Single source is ToolSpec; the old registry snapshot was removed.
        names = visible_tool_names_for_snapshot(policy, ("open_result",))
        self.assertIn("read_file", names)
        self.assertIn("edit", names)


class DoneCompatTests(unittest.TestCase):
    def test_done_text_reads_both_summary_and_answer(self) -> None:
        from codey.protocols.done_compat import read_done_text

        self.assertEqual(read_done_text({"summary": "hello"}), "hello")
        self.assertEqual(read_done_text({"answer": "hello"}), "hello")
        self.assertEqual(read_done_text({}), "")


class HybridHandoffTests(unittest.TestCase):
    def test_hybrid_preserves_research_handoff_without_forcing_fresh_chat(self) -> None:
        # Old two-phase run_hybrid_mode was deleted; single entry run_task_mode
        # owns hybrid with one TaskSession (no fresh_chat, handoff preserved).
        from codey.operations import research_flow as rf

        self.assertFalse(hasattr(rf, "run_hybrid_mode"))
        from codey.operations.task_entry import run_task_mode

        self.assertTrue(callable(run_task_mode))
        # Single-session hybrid keeps prior handoff (no forced fresh chat):
        # verified via dispatch cutover locks in test_unified_cutover.


if __name__ == "__main__":
    unittest.main()
