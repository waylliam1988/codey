"""Convergence repro locks: deterministic defects must fail before fix, pass after.

Covers the 7 behavior items + recovery cross-provider + required_checks hygiene.
Red-first: these tests encode the desired post-fix behavior.
"""
from __future__ import annotations

import unittest


class BatchMismatchTests(unittest.TestCase):
    def test_mismatch_aborts_batch_and_preserves_receipt(self) -> None:
        from codey.operations.kernel_execution import execute_turn
        from codey.operations.task_session import TaskSession, turn_effect_id
        from codey.policies.task_policy import TaskPolicy
        from codey.runtime.core.models import ToolCall

        policy = TaskPolicy(grants=frozenset({"project.read", "project.write", "project.verify", "control"}))
        session = TaskSession(policy=policy, task_kind="project", project="demo", max_turns=4)
        run_id = "run-batch-1"
        active_turn = 1
        # Prior settled success for slot 0: edit file A.
        good_identity = turn_effect_id(run_id, active_turn, 0)
        session.executed[good_identity] = {
            "name": "edit",
            "ok": True,
            "call_id": "c0",
            "excerpt": "ok",
            "args_digest": __import__("codey.runtime.effects.effect_records", fromlist=["compute_args_digest"]).compute_args_digest({"path": "a.txt", "content": "hello"}),
        }
        # New batch: slot 0 mismatched args, slot 1 fresh edit that must NOT run.
        bad_call = ToolCall(name="edit", args={"path": "a.txt", "content": "OTHER"}, call_id="c0b")
        second_call = ToolCall(name="edit", args={"path": "b.txt", "content": "new"}, call_id="c1")
        executed = []

        def _exec(call):
            executed.append(call.name)
            from codey.runtime.core.models import ToolResult
            return ToolResult(call=call, model_text="ok")

        results = execute_turn(
            session, [bad_call, second_call],
            executors={"edit": _exec},
            run_id=run_id, turn=active_turn,
        )
        self.assertEqual(len(results), 2)
        # Both slots must be errors without executing real tools.
        self.assertTrue(str(results[0].model_text).startswith("ERROR: recovery mismatch"))
        self.assertTrue(str(results[1].model_text).startswith("ERROR: recovery mismatch"))
        self.assertEqual(executed, [])
        # Original receipt must be preserved, not overwritten with error.
        self.assertTrue(session.executed[good_identity].get("ok") is True)
        self.assertEqual(session.executed[good_identity].get("excerpt"), "ok")


class FakeFingerprintTests(unittest.TestCase):
    def test_no_synthesized_fingerprint(self) -> None:
        from codey.operations.project_completion_checks import _evidence_with_session_facts
        from codey.operations.task_session import TaskSession
        from codey.policies.task_policy import TaskPolicy
        from codey.runtime.observe.execution_evidence import ExecutionEvidence

        policy = TaskPolicy(grants=frozenset({"project.read", "project.write", "project.verify", "control"}))
        session = TaskSession(policy=policy, task_kind="project", project="demo", max_turns=4)
        session.record_edit("a.txt")
        latest = max(session.edited_files.values())
        session.record_verification("pytest -q", latest, True, exit_code=0)
        evidence = ExecutionEvidence(workspace_revision=1, workspace_fingerprint="")
        before_fp = str(getattr(evidence, "workspace_fingerprint", "") or "")
        self.assertEqual(before_fp, "")
        _evidence_with_session_facts(evidence, session)
        after_fp = str(getattr(evidence, "workspace_fingerprint", "") or "")
        # Must NOT synthesize a format-valid fingerprint from kernel-session hash.
        self.assertEqual(after_fp, "")
        self.assertEqual(list(getattr(evidence, "checks_after_edit", []) or []), [])


class ControllerFailureTests(unittest.TestCase):
    def test_controller_failure_is_fail_closed(self) -> None:
        from codey.operations import kernel_protocol as proto
        from codey.operations.task_session import TaskSession
        from codey.policies.task_policy import TaskPolicy

        policy = TaskPolicy(grants=frozenset({"web.read", "knowledge.read", "knowledge.write", "control"}), strict_research=True)
        session = TaskSession(policy=policy, task_kind="research", project="demo", max_turns=2)
        orig = proto.controller_allowed_for_session

        def _boom(_session):
            raise RuntimeError("controller boom")

        proto.controller_allowed_for_session = _boom  # type: ignore[assignment]
        try:
            try:
                snapshot = proto.build_turn_snapshot(session)
            except RuntimeError:
                snapshot = "raised"
            # Must NOT encode failure as None (unlimited): either raise or
            # return an explicit fail-closed sentinel, never None.
            self.assertNotEqual(snapshot, None, "controller failure must not become unlimited")
        finally:
            proto.controller_allowed_for_session = orig  # type: ignore[assignment]

    def test_guarded_slot_exception_does_not_allow(self) -> None:
        from codey.operations import kernel_execution as kernel
        from codey.operations.task_session import TaskSession
        from codey.policies.task_policy import TaskPolicy
        from codey.runtime.core.models import ToolCall

        policy = TaskPolicy(grants=frozenset({"web.read", "control"}), strict_research=True)
        session = TaskSession(policy=policy, task_kind="research", project="demo", max_turns=2)
        call = ToolCall(name="web_search", args={"query": "hi"}, call_id="c0")
        orig_allows = None
        try:
            import codey.operations.kernel_protocol as proto
            orig_allows = proto._controller_allows

            def _boom(tool, allowed):
                raise RuntimeError("ctl boom")

            proto._controller_allows = _boom  # type: ignore[assignment]
            result = kernel._guarded_slot_result(session, "id-x", call, "web_search", 1, None, {"web_search"})
            # Exception in controller check must fail closed, never None (proceed).
            self.assertIsNotNone(result)
            self.assertTrue(str(result.model_text).startswith("ERROR:"))
        finally:
            if orig_allows is not None:
                import codey.operations.kernel_protocol as proto2
                proto2._controller_allows = orig_allows  # type: ignore[assignment]


class ReadonlyNegationTests(unittest.TestCase):
    def test_negative_chinese_does_not_require_modification(self) -> None:
        from codey.operations.completion_gate import evaluate
        from codey.operations.task_session import TaskSession
        from codey.policies.task_policy import TaskPolicy

        policy = TaskPolicy(grants=frozenset({"project.read", "project.write", "project.verify", "control"}))
        session = TaskSession(policy=policy, task_kind="project", project="demo", max_turns=4)
        session.task_text = "检查 bug，不要修改任何文件"
        # Explicit entry requirement: no modification required.
        session.project_changes_required = False  # type: ignore[attr-defined]
        verdict = evaluate(session, "结论\n检查完成\n来源\n无改动")
        # Must not block on project_changes_required.
        self.assertNotIn("project_changes_required", verdict.followup)


class CustomToolTests(unittest.TestCase):
    def test_register_parse_execute_complete(self) -> None:
        from codey.operations.kernel_execution import execute_turn
        from codey.operations.task_session import TaskSession
        from codey.policies.task_policy import TaskPolicy
        from codey.runtime.core.models import ToolCall
        from codey.toolchain import tool_spec as spec_mod

        tool_name = "custom_probe_xyz"
        # Ensure clean state.
        specs = spec_mod.tool_specs()
        specs.pop(tool_name, None)
        # Register with a known grant; use control-adjacent grant that policy allows.
        # Use project.read grant via visible policy: need a grant the test policy allows.
        ok = spec_mod.register_custom_tool(tool_name, grant="project.read", parameters=(("q", {"type": "string"}),), required=("q",), description="probe")
        self.assertTrue(ok)
        try:
            policy = TaskPolicy(grants=frozenset({"project.read", "control"}))
            # JSON parse must accept the registered tool.
            import json

            from codey.operations.kernel_protocol import normalize_turn
            plan = normalize_turn(json.dumps({"tool": tool_name, "args": {"q": "hi"}}), policy=policy, controller_allowed=None)
            self.assertEqual(plan.protocol_error, "")
            self.assertEqual(len(plan.calls), 1)
            self.assertEqual(plan.calls[0].name, tool_name)
            # Native parse must also accept.
            from types import SimpleNamespace
            native_reply = SimpleNamespace(tool_calls=[SimpleNamespace(name=tool_name, arguments={"q": "hi"}, id="n1")], text="")
            plan2 = normalize_turn(native_reply, policy=policy, controller_allowed=None)
            self.assertEqual(plan2.protocol_error, "")
            # Execute via injected executor must succeed.
            session = TaskSession(policy=policy, task_kind="project", project="demo", max_turns=2)
            from codey.runtime.core.models import ToolResult
            results = execute_turn(session, [ToolCall(name=tool_name, args={"q": "hi"}, call_id="c0")], executors={tool_name: lambda c: ToolResult(call=c, model_text="custom ok")}, run_id="r-custom", turn=1)
            self.assertEqual(results[0].model_text, "custom ok")
        finally:
            specs.pop(tool_name, None)
            spec_mod._SPECS = None  # reset cache if needed


class SecondRoundWebPromptTests(unittest.TestCase):
    def test_second_round_web_message_carries_new_contract(self) -> None:
        from codey.operations.task_loop import run_task_kernel
        from codey.operations.task_session import TaskSession
        from codey.policies.task_policy import build_task_policy
        from codey.task.model import TaskSubmission

        submission = TaskSubmission(session_id="s", project="demo", task="research web", max_turns=3, continue_task=False, provider_id="local", strict_research=True)
        policy = build_task_policy(submission, task_kind="research", strict_research=True)
        session = TaskSession(policy=policy, task_kind="research", project="demo", max_turns=3, task_text="research web")
        sent_prompts: list[str] = []

        class FakeWeb:
            def send(self, prompt, timeout=None):
                sent_prompts.append(str(prompt))
                if len(sent_prompts) == 1:
                    return '{"tool":"web_search","args":{"query":"hi"}}'
                return '{"tool":"done","args":{"summary":"结论\n来源\nx"}}'

        # Minimal research tools stub: web_search returns URLs, open allowed after.
        class FakeTools:
            def __init__(self):
                from types import SimpleNamespace as SN
                self.ledger = SN(evidence_items=[], final_url_set=lambda: set(), opened_sources=[], searches=[], search_results_payload=lambda: [], opened_sources_payload=lambda: [], coverage_payload=lambda: {}, evidence_payload=lambda: [])
                self.sources_read = set()
                self.search_result_urls = set()

            def web_search(self, q):
                return "r1: https://example.com/a\nr2: https://example.com/b"

            def open_url(self, url, **kwargs):
                from types import SimpleNamespace as SN
                return SN(model_text=f"Title: T\nBody for {url}", receipt_text=f"Body for {url}")

        # Provide executors via research_tools through delegate? Use direct executors for simplicity:
        # run_task_kernel with research_tools FakeTools will handle web_search/open via delegate.
        from unittest.mock import patch

        with patch("codey.operations.kernel_transport.provider_uses_native", return_value=False):
            run_task_kernel(session, provider=FakeWeb(), executors={}, run_id="r-web2", research_tools=FakeTools(), session_id="s", completion_context=None)
        self.assertGreaterEqual(len(sent_prompts), 2)
        second = sent_prompts[1]
        # Second-round web prompt must carry the new tool contract + change reason,
        # not just result refs. Contract JSON for open_result plus visible-tools line.
        self.assertIn('{"tool":"open_result"', second)
        self.assertTrue(("Visible tools" in second) or ("Tool contract" in second))
        self.assertTrue(("changed" in second.lower()) or ("controller" in second.lower()))


class DoneReceiptFailureTests(unittest.TestCase):
    def test_native_done_receipt_failure_is_not_complete(self) -> None:
        from types import SimpleNamespace

        from codey.operations.task_loop import run_task_kernel
        from codey.operations.task_session import TaskSession
        from codey.policies.task_policy import TaskPolicy

        policy = TaskPolicy(grants=frozenset({"control"}))
        session = TaskSession(policy=policy, task_kind="project", project="", max_turns=2, task_text="hi")

        class FakeNative:
            def send_turn(self, prompt, tools, timeout=None):
                return SimpleNamespace(tool_calls=[SimpleNamespace(name="done", arguments={"summary": "结论\n来源\nx"}, id="d1")], text="")

            def send_tool_results(self, messages, tools, timeout=None):
                raise RuntimeError("delivery boom")

        from unittest.mock import patch

        with (
            patch("codey.operations.kernel_transport.provider_uses_native", return_value=True),
            patch("codey.toolchain.tool_spec.native_tools_for_snapshot", return_value=[{"type": "function", "function": {"name": "done", "description": "d", "parameters": {"type": "object", "properties": {}, "required": []}}}]),
        ):
            result = run_task_kernel(session, provider=FakeNative(), executors={}, run_id="r-done-fail", provider_id="local")
        self.assertFalse(result.completed)
        self.assertIn(result.stop_reason, ("provider_failure", "pending_delivery"))


class CrossProviderRecoveryTests(unittest.TestCase):
    def test_new_provider_session_uses_text_not_old_call_ids(self) -> None:
        from codey.operations.kernel_recovery import apply_recovery_first
        from codey.operations.task_session import TaskSession
        from codey.policies.task_policy import TaskPolicy
        from codey.runtime.core.models import ToolCall, ToolResult

        policy = TaskPolicy(grants=frozenset({"control"}))
        session = TaskSession(policy=policy, task_kind="project", project="", max_turns=2)
        pending = [ToolResult(call=ToolCall(name="web_search", args={"query": "hi"}, call_id="old-id-1"), model_text="old result")]
        prompt, messages = apply_recovery_first(
            session,
            True,
            pending,
            "base",
            None,
            provider_session_changed=True,
            format_results=lambda rows, _session: "\n".join(item.model_text for item in rows),
            native_tool_messages=lambda _rows, _session: [],
        )
        # Must fall back to text re-explanation, not native messages with stale ids.
        self.assertIsNone(messages)
        self.assertIn("old result", prompt)
        self.assertNotIn("old-id-1", str(messages))


class RequiredChecksHygieneTests(unittest.TestCase):
    def test_over_limit_required_checks_is_explicit_error(self) -> None:
        from codey.operations.completion_gate import evaluate
        from codey.operations.task_session import TaskSession
        from codey.policies.task_policy import TaskPolicy

        checks = tuple(f"check_{i}" for i in range(13))
        policy = TaskPolicy(grants=frozenset({"control"}), required_checks=checks)
        session = TaskSession(policy=policy, task_kind="project", project="", max_turns=2)
        verdict = evaluate(session, "done")
        self.assertFalse(verdict.complete)
        self.assertIn("too many", verdict.followup.lower())

    def test_policy_recovery_does_not_silently_truncate(self) -> None:
        from codey.policies.task_policy import TaskPolicy
        payload = {"grants": ["control"], "strict_research": False, "required_checks": [f"c{i}" for i in range(20)], "source": "t", "version": 1}
        revived = TaskPolicy.from_payload(payload)
        # Must not silently truncate to 16; keep all so the gate can report over-limit.
        self.assertEqual(len(revived.required_checks), 20)


if __name__ == "__main__":
    unittest.main()
