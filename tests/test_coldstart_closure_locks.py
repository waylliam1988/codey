"""Cold-start closure locks: deterministic gaps must fail before fix, pass after.

Red-first: each test encodes the desired post-fix behavior described in the
115f82b review. They must fail on the pre-fix tree and pass after the fix.
"""
from __future__ import annotations

import unittest


class WorkspaceFingerprintStaleTests(unittest.TestCase):
    def test_edit_run_done_verification_uses_post_edit_fingerprint(self) -> None:
        """Real kernel edit→run→done: verification must carry post-edit identity."""
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        from codey.operations.task_loop import run_task_kernel
        from codey.operations.task_session import TaskSession
        from codey.policies.task_policy import build_task_policy
        from codey.task.model import TaskSubmission

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "proj"
            project.mkdir(parents=True)
            target = project / "a.txt"
            target.write_text("hello", encoding="utf-8")

            submission = TaskSubmission(
                session_id="s-ws",
                project=str(project),
                task="fix file then verify",
                max_turns=5,
                continue_task=False,
                provider_id="local",
                requested_capabilities=("project.write",),
            )
            policy = build_task_policy(submission, task_kind="project", strict_research=False)
            session = TaskSession(
                policy=policy,
                task_kind="project",
                project=str(project),
                max_turns=5,
                task_text="fix file then verify",
                project_changes_required=True,
            )
            # Seed from real outer evidence (revision 1).
            from codey.runtime.observe.execution_evidence import ExecutionEvidence
            from codey.workspace.revision import WorkspaceRevisionStore

            store = WorkspaceRevisionStore(Path(tmp) / "state")
            initial = store.current_state(str(project))
            outer = ExecutionEvidence(
                workspace_revision=initial.revision,
                workspace_fingerprint=initial.fingerprint,
            )
            session.set_workspace_state(initial.revision, initial.fingerprint)

            calls = [
                '{"tool":"read_file","args":{"path":"a.txt"}}',
                '{"tool":"edit","args":{"path":"a.txt","replacements":[{"search":"hello","replace":"hello world"}]}}',
                '{"tool":"run","args":{"path":".","command":"python -m py_compile a.txt"}}',
                '{"tool":"done","args":{"summary":"fixed"}}',
            ]
            state = {"i": 0}

            class FakeProvider:
                def send(self, prompt, timeout=None):
                    idx = state["i"]
                    state["i"] += 1
                    return calls[min(idx, len(calls) - 1)]

            from codey.agents.tools import DEFAULT_TOOL_FNS

            with patch("codey.operations.kernel_transport.provider_uses_native", return_value=False):
                run_task_kernel(
                    session,
                    provider=FakeProvider(),
                    executors={},
                    run_id="r-ws-stale",
                    effect_scope="unified",
                    project_path=project,
                    tool_fns=DEFAULT_TOOL_FNS,
                    session_id="s-ws",
                    permission_profile="coding_writer",
                    user_task="fix file then verify",
                    completion_context={
                        "run_id": "r-ws-stale",
                        "task": "fix file then verify",
                        "question": "fix file then verify",
                        "project": str(project),
                        "execution_evidence": outer,
                    },
                )
            # Must have edited and verified.
            self.assertTrue(session.edited_files, "expected an edit fact")
            self.assertTrue(session.verifications, "expected a run verification")
            last = session.verifications[-1]
            # Post-edit workspace identity (real store state after edit).
            post = store.current_state(str(project))
            # The verification must equal the edited workspace fingerprint,
            # not the stale pre-edit identity.
            self.assertEqual(
                str(last.get("workspace_fingerprint") or ""),
                str(post.fingerprint or ""),
                "verification fingerprint must equal post-edit workspace fingerprint",
            )
            self.assertEqual(
                int(last.get("workspace_revision") or 0),
                int(post.revision or 0),
                "verification revision must equal post-edit revision",
            )
            # Session itself must have advanced to the post-edit state.
            self.assertEqual(
                str(getattr(session, "workspace_fingerprint", "") or ""),
                str(post.fingerprint or ""),
            )


class ProjectChangesRequiredChainTests(unittest.TestCase):
    def test_hybrid_fix_without_edits_blocks(self) -> None:
        """Hybrid '修复登录 bug' with no edits must NOT complete."""
        from codey.operations.completion_gate import evaluate
        from codey.operations.task_session import TaskSession
        from codey.policies.task_policy import TaskPolicy

        policy = TaskPolicy(grants=frozenset({"project.read", "project.write", "project.verify", "control"}))
        session = TaskSession(
            policy=policy,
            task_kind="hybrid",
            project="demo",
            max_turns=4,
            task_text="修复登录 bug",
            project_changes_required=True,
        )
        verdict = evaluate(session, "结论\n修复完成\n来源\n无")
        self.assertFalse(verdict.complete, "hybrid requiring changes with no edits must block")
        self.assertIn("project_changes_required", (verdict.followup or "") + str(getattr(verdict.proof, "reason_codes", "")))

    def test_submit_chain_carries_project_changes_required(self) -> None:
        """run_submit_response→submit_task→TaskSubmission must carry the flag."""
        import tempfile
        from pathlib import Path

        from codey.app import api as api_mod

        seen: dict = {}

        def fake_submit(session_id, project, task, max_turns, continue_task, provider_id, intent, **kwargs):
            seen.update(kwargs)
            seen["intent"] = intent
            seen["project"] = project
            seen["task"] = task
            return "run-123"

        with tempfile.TemporaryDirectory() as tmp:
            proj = str(Path(tmp) / "demo")
            Path(proj).mkdir(parents=True, exist_ok=True)
            status, payload = api_mod.run_submit_response(
                {
                    "session_id": "s1",
                    "project": proj,
                    "task": "修复登录 bug",
                    "intent": "hybrid",
                    "max_turns": 4,
                },
                fake_submit,
            )
        self.assertEqual(status, 200)
        # Production chain must set an explicit requires-modification value.
        self.assertIn("project_changes_required", seen, "submit chain must carry project_changes_required")
        self.assertTrue(seen["project_changes_required"] is True, "hybrid fix must require changes")

    def test_readonly_intent_does_not_require_changes(self) -> None:
        import tempfile
        from pathlib import Path

        from codey.app import api as api_mod

        seen: dict = {}

        def fake_submit(session_id, project, task, max_turns, continue_task, provider_id, intent, **kwargs):
            seen.update(kwargs)
            seen["intent"] = intent
            return "run-124"

        with tempfile.TemporaryDirectory() as tmp:
            proj = str(Path(tmp) / "demo")
            Path(proj).mkdir(parents=True, exist_ok=True)
            status, _ = api_mod.run_submit_response(
                {
                    "session_id": "s1",
                    "project": proj,
                    "task": "检查 bug，不要修改任何文件",
                    "intent": "readonly",
                    "max_turns": 4,
                },
                fake_submit,
            )
        self.assertEqual(status, 200)
        self.assertIn("project_changes_required", seen)
        self.assertFalse(seen["project_changes_required"], "readonly must not require changes")

    def test_requires_changes_without_write_is_config_conflict(self) -> None:
        """Entry declaring must-change without write permission must fail fast."""
        import tempfile
        from pathlib import Path as _Path

        from codey.task.model import TaskSubmission

        _tmp = tempfile.mkdtemp()
        _proj_for_req = str(_Path(_tmp) / "p")
        _Path(_proj_for_req).mkdir(parents=True, exist_ok=True)
        submission = TaskSubmission(
            session_id="s1",
            project=_proj_for_req,
            task="修复登录 bug",
            max_turns=4,
            continue_task=False,
            provider_id="local",
            intent="hybrid",
            requested_capabilities=(),
            strict_research=True,
            project_changes_required=True,
        )
        # Building the session/policy must surface a config conflict when
        # requires-changes has no project.write grant.
        import tempfile
        from pathlib import Path

        from codey.operations import task_entry as um
        from codey.operations.context import RunFrame, RunHooks, RunWork
        from codey.runtime.observe.execution_evidence import ExecutionEvidence

        with tempfile.TemporaryDirectory() as tmp:
            proj = str(Path(tmp) / "p")
            Path(proj).mkdir(parents=True, exist_ok=True)
            frame = RunFrame(
                request=submission,
                run_id="r-conflict",
                task_kind="hybrid",
                provider=None,
                provider_id="local",
                project_text=proj,
                conversation=None,  # type: ignore[arg-type]
                fresh_chat=False,
                handoff="",
                research_handoff="",
                prior_snapshot=None,  # type: ignore[arg-type]
                recovered_owner_prompt="",
                provider_session_changed=False,
                preflight_tried=set(),
                preflight_switches=0,
            )
            work = RunWork(recent_events=[], evidence=ExecutionEvidence())
            hooks = RunHooks(
                on_event=lambda e: None,
                on_shell_request=lambda a: None,
                update_checkpoint=lambda a: None,
                record_provider_failure=lambda a, b: None,
                append_ledger=lambda a: None,
                provider_failover_order=lambda: (),
                supervisor=None,
            )
            deps = type("D", (), {"workspace_revisions": None, "knowledge_store": None, "managed_outputs": None, "runtime_mutations": None, "state": None})()
            with self.assertRaises(RuntimeError, msg="must-change without write must be config conflict"):
                um.run_entry_kernel(frame, work, hooks, deps, task_kind="hybrid")


class ToolSpecValidationTests(unittest.TestCase):
    def test_integer_type_mismatch_rejected(self) -> None:
        from codey.toolchain import tool_spec as spec_mod

        name = "custom_int_xyz"
        specs = spec_mod.tool_specs()
        specs.pop(name, None)
        try:
            ok = spec_mod.register_custom_tool(
                name, grant="project.read",
                parameters=(("count", {"type": "integer"}),),
                required=("count",),
                description="probe",
            )
            self.assertTrue(ok)
            err = spec_mod.validate_args_against_spec(name, {"count": "not-an-int"})
            self.assertTrue(err, "integer field given a string must be rejected")
        finally:
            specs.pop(name, None)
            spec_mod._SPECS = None

    def test_extra_undeclared_param_rejected(self) -> None:
        from codey.toolchain import tool_spec as spec_mod

        name = "custom_extra_xyz"
        specs = spec_mod.tool_specs()
        specs.pop(name, None)
        try:
            ok = spec_mod.register_custom_tool(
                name, grant="project.read",
                parameters=(("q", {"type": "string"}),),
                required=("q",),
                description="probe",
            )
            self.assertTrue(ok)
            err = spec_mod.validate_args_against_spec(name, {"q": "hi", "nope": "x"})
            self.assertTrue(err, "extra undeclared param must be rejected")
        finally:
            specs.pop(name, None)
            spec_mod._SPECS = None


class SnapshotFailClosedTests(unittest.TestCase):
    def test_snapshot_build_failure_does_not_fallback(self) -> None:
        """Snapshot import failure must terminate the turn, not show stale tools."""
        from codey.operations import kernel_prompt, kernel_protocol
        from codey.policies.task_policy import TaskPolicy

        policy = TaskPolicy(grants=frozenset({"web.read", "knowledge.read", "control"}), strict_research=True)
        # Force the new snapshot builder to fail.
        import codey.toolchain.tool_spec as spec_mod

        orig = spec_mod.visible_tool_names_for_snapshot

        def _boom(policy_arg, controller_allowed=None):
            raise RuntimeError("snapshot boom")

        spec_mod.visible_tool_names_for_snapshot = _boom  # type: ignore[assignment]
        try:
            # _snapshot_names must not silently fallback to policy-scope list;
            # it must raise/fail-closed so the kernel terminates the turn.
            with self.assertRaises(RuntimeError, msg="snapshot failure must not fallback"):
                kernel_prompt._snapshot_names(policy, ("web_search",))
            with self.assertRaises(RuntimeError, msg="native snapshot failure must not fallback"):
                kernel_protocol._native_tools_for_policy(policy, ("web_search",))
        finally:
            spec_mod.visible_tool_names_for_snapshot = orig  # type: ignore[assignment]


class TaskEntryOwnershipTests(unittest.TestCase):
    def test_task_entry_owns_implementation_no_unified_mode(self) -> None:
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent
        self.assertFalse((root / "codey" / "operations" / "unified_mode.py").exists(), "unified_mode.py must be deleted; impl lives in task_entry.py")
        text = (root / "codey" / "operations" / "task_entry.py").read_text(encoding="utf-8")
        self.assertIn("def run_task_mode", text, "task_entry must own run_task_mode")
        self.assertIn("def run_task_submission", text, "task_entry must own run_task_submission")
        self.assertNotIn("from codey.operations.task_entry import", text)

    def test_unified_alias_removed_everywhere(self) -> None:
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent
        # API intent list must not contain unified.
        api_text = (root / "codey" / "app" / "api.py").read_text(encoding="utf-8")
        self.assertNotIn('"unified"', api_text, "API intent list must not contain unified alias")
        kind_text = (root / "codey" / "task" / "kind.py").read_text(encoding="utf-8")
        self.assertNotIn('"unified"', kind_text)
        # research_iteration unified name must be renamed.
        ri_text = (root / "codey" / "operations" / "research_iteration.py").read_text(encoding="utf-8")
        self.assertNotIn("run_unified_research_iteration", ri_text)
        self.assertIn("def run_research_iteration", ri_text)
        # done_compat single name: no done_compat alias file with two names?
        # At least assert no unified_evidence_followup module.
        self.assertFalse((root / "codey" / "operations" / "unified_evidence_followup.py").exists())

    def test_old_loops_deleted(self) -> None:
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent
        self.assertFalse((root / "codey" / "agents" / "loop.py").exists(), "agents/loop.py must be deleted")
        self.assertFalse((root / "codey" / "agents" / "runner.py").exists(), "agents/runner.py must be deleted")
        self.assertFalse((root / "codey" / "research" / "runner.py").exists(), "research/runner.py must be deleted")
        # synthesis must not import old runner appendix.
        syn_text = (root / "codey" / "research" / "synthesis.py").read_text(encoding="utf-8")
        self.assertNotIn("research.runner", syn_text)
        # No production import of old loops.
        offenders = []
        for path in (root / "codey").rglob("*.py"):
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            if "from codey.agents.loop import" in text or "from codey.research.runner import" in text:
                offenders.append(str(path.relative_to(root)))
        self.assertEqual(offenders, [], f"production must not import old loops: {offenders}")

    def test_research_controller_and_protocol_codec_are_not_production_surface(self) -> None:
        """Research execution uses the shared kernel, not the retired codec/controller."""
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent
        self.assertFalse(
            (root / "codey" / "research" / "controller.py").exists(),
            "retired ResearchController must not remain in the production package",
        )
        self.assertFalse(
            (root / "codey" / "research" / "protocols.py").exists(),
            "retired Research codec must live with experiment support, not production",
        )
        offenders = []
        for path in (root / "codey").rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="ignore")
            if "codey.research.controller" in text or "codey.research.protocols" in text:
                offenders.append(str(path.relative_to(root)))
        self.assertEqual(offenders, [])

    def test_done_payload_has_one_canonical_field(self) -> None:
        """Cold start has no old-record compatibility reader for done payloads."""
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent
        self.assertFalse((root / "codey" / "protocols" / "done_compat.py").exists())
        offenders = []
        for path in (root / "codey").rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="ignore")
            if "done_compat" in text or "read_done_text" in text:
                offenders.append(str(path.relative_to(root)))
        self.assertEqual(offenders, [])

    def test_task_loop_delegates_transport_and_recovery_helpers(self) -> None:
        """The loop owns orchestration; provider I/O and resume shaping have leaf owners."""
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent
        loop = (root / "codey" / "operations" / "task_loop.py").read_text(encoding="utf-8")
        self.assertTrue((root / "codey" / "operations" / "kernel_transport.py").exists())
        self.assertTrue((root / "codey" / "operations" / "kernel_recovery.py").exists())
        for name in (
            "_call_provider_send",
            "_call_provider_send_turn",
            "_call_provider_send_results",
            "_send_kernel_reply",
            "_repair_native_dangling",
            "_apply_recovery_first",
        ):
            self.assertNotIn(f"def {name}", loop)

    def test_research_ab_scripts_use_experiment_support_and_shared_iteration(self) -> None:
        """Important A/B probes must remain runnable without retired production modules."""
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent
        scripts = (
            "tests/manual/deep_research_core_ab.py",
            "tests/manual/research_repair_prompt_ab.py",
            "tests/manual/concept_context_ab.py",
            "tests/manual/research_source_rendering_ab.py",
        )
        for relative in scripts:
            text = (root / relative).read_text(encoding="utf-8")
            self.assertNotIn("codey.research.controller", text, relative)
            self.assertNotIn("codey.research.protocols", text, relative)
            self.assertTrue(
                "ResearchIteration" in text or "tests.support.research_protocol" in text,
                relative,
            )

    def test_resume_policy_wrapper_deleted(self) -> None:
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent
        text = (root / "codey" / "operations" / "task_loop.py").read_text(encoding="utf-8")
        self.assertNotIn("def resume_policy", text, "resume_policy wrapper must be deleted")


if __name__ == "__main__":
    unittest.main()
