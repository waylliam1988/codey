"""One snapshot per round, shared approval profile, fail-closed receipts.

Step-1 red tests for the task_loop state-flow cleanup (then fix behavior):

- Snapshot failures fail closed: persistent failure means controller_failure
  with no further provider send and no stale contract delivery. Post-execution
  snapshot rebuilds and old-contract fallbacks are blocked by real turn order
  and failure assertions.
- Shell approval uses the same effective project profile as real project
  execution, after TaskPolicy shell.approval authorization. Read-only and
  unauthorized Research never pause; explicit auth + engine approval pauses.
- Native receipt send failures report provider_failure and stop (never None
  + continue). Missing call ids cannot be receipted (defensive empty batch).
- Optional coding-context render failure keeps the base prompt with an
  observable diagnostic (never a silent permission-style failure).
- Provider native identification failure never silently falls back to web.
"""

from __future__ import annotations

import inspect
import unittest
from types import SimpleNamespace
from unittest import mock

from codey.operations.task_loop import KernelExecutionDeps, KernelRunRequest, KernelTransportDeps


def _project_policy():
    from codey.policies.task_policy import build_task_policy
    from codey.task.model import TaskSubmission

    return build_task_policy(
        TaskSubmission("s", "E:/tmp", "t", 8, False, "local"), task_kind="project")


def _strict_research_policy():
    from codey.policies.task_policy import build_task_policy
    from codey.task.model import TaskSubmission

    return build_task_policy(
        TaskSubmission("s", None, "research q", 8, False, "local"),
        task_kind="research", strict_research=True)


class SnapshotFailClosedTests(unittest.TestCase):
    def test_persistent_snapshot_failure_is_controller_failure_with_one_send(self) -> None:
        from codey.operations.task_loop import run_task_kernel
        from codey.operations.task_session import TaskSession

        sends: list[str] = []

        class WebProvider:
            def send(self, prompt, timeout=None):
                sends.append(prompt)
                return '{"tool":"read_file","args":{"path":"a.py"}}'

        session = TaskSession(policy=_project_policy(), task_kind="project",
                              project="", max_turns=4)
        real_visible = None
        try:
            import codey.toolchain.tool_spec as ts

            real_visible = ts.visible_tool_names_for_snapshot
            calls = {"n": 0}

            def flaky(*args, **kwargs):
                calls["n"] += 1
                # One TurnSnapshot builds contract + names (2 underlying
                # calls). Startup builds once and round 1 reuses it; fail on
                # round 2 so exactly one provider send precedes the failure.
                if calls["n"] > 2:
                    raise RuntimeError("snapshot boom")
                return real_visible(*args, **kwargs)

            with mock.patch.object(ts, "visible_tool_names_for_snapshot", flaky):
                result = run_task_kernel(
                    session,
                    request=KernelRunRequest(
                        transport=KernelTransportDeps(
                            provider=WebProvider(),
                            run_id="r-snap",
                            effect_scope="snap",
                        ),
                        execution=KernelExecutionDeps(
                            executors={},
                        ),
                    ),
                )
        finally:
            pass
        self.assertEqual(result.stop_reason, "controller_failure")
        self.assertEqual(len(sends), 1, "no provider send after snapshot failure")

    def test_each_round_builds_snapshot_before_send_without_postexec_rebuild(self) -> None:
        from codey.operations import task_loop as kernel
        from codey.operations.task_session import TaskSession
        from codey.policies.task_policy import TaskPolicy
        from codey.runtime.core.models import ToolResult

        events = []
        replies = iter(['{"tool":"read_file","args":{"path":"a.py"}}',
                        '{"tool":"done","args":{"summary":"done"}}'])
        build = kernel.kernel_protocol.build_turn_snapshot

        def snapshot(*args, **kwargs):
            events.append("snapshot")
            return build(*args, **kwargs)

        def send(prompt):
            events.append("send")
            return next(replies)

        def read(call):
            events.append("execute")
            return ToolResult(ok=True, call=call, model_text="a.py content")

        session = TaskSession(policy=TaskPolicy(frozenset({"control", "project.read"})), max_turns=2)
        with mock.patch.object(kernel.kernel_protocol, "build_turn_snapshot", snapshot):
            result = kernel.run_task_kernel(
                         session,
                         request=KernelRunRequest(
                             transport=KernelTransportDeps(
                                 provider=SimpleNamespace(send=send),
                                 run_id='snapshot-order',
                             ),
                             execution=KernelExecutionDeps(
                                 executors={'read_file': read},
                             ),
                         ),
                     )
        self.assertTrue(result.completed)
        self.assertEqual(events, ["snapshot", "send", "execute", "snapshot", "send"])

    def test_web_drift_notice_still_announced_on_success(self) -> None:
        from codey.operations.task_loop import run_task_kernel
        from codey.operations.task_session import TaskSession
        from codey.runtime.core.models import ToolResult

        prompts: list[str] = []
        replies = [
            '{"tool":"web_search","args":{"query":"login docs"}}',
            '{"tool":"open_url","args":{"url":"https://example.com/docs"}}',
            '{"tool":"done","args":{"summary":"done"}}',
        ]

        class WebProvider:
            def send(self, prompt, timeout=None):
                prompts.append(prompt)
                return replies.pop(0)

        def fake_search(call):
            return ToolResult(ok=True, call=call, model_text="found https://example.com/docs")

        def fake_open(call):
            return ToolResult(ok=True, call=call, model_text="page text")

        from codey.policies.task_policy import build_task_policy
        from codey.task.model import TaskSubmission

        policy = build_task_policy(
            TaskSubmission("s1", None, "check docs", 8, False, "local",
                           requested_capabilities=("web.read",)),
            task_kind="hybrid")
        session = TaskSession(policy=policy, task_kind="hybrid", project="",
                              max_turns=8)
        run_task_kernel(
            session,
            request=KernelRunRequest(
                transport=KernelTransportDeps(
                    provider=WebProvider(),
                    run_id="r-drift",
                ),
                execution=KernelExecutionDeps(
                    executors={"web_search": fake_search, "open_url": fake_open},
                ),
            ),
        )
        joined = "\n".join(prompts[1:])
        self.assertIn("open_url", joined)


class ApprovalEffectiveProfileTests(unittest.TestCase):
    def _shell_call(self):
        from codey.runtime.core.models import ToolCall

        return ToolCall("shell", {"command": "rm -rf /tmp/x", "path": "."}, "c1")

    def test_readonly_policy_never_pauses_without_engine_call(self) -> None:
        from codey.operations import task_loop as kernel
        from codey.policies.task_policy import build_task_policy
        from codey.task.model import TaskSubmission

        policy = build_task_policy(
            TaskSubmission("s", "E:/tmp", "plan it", 8, False, "local"),
            task_kind="planning")
        self.assertFalse(policy.allows("shell.approval"))
        seen = []
        with mock.patch("codey.agents.tool_execution.evaluate_tool_call_policy_for",
                        side_effect=lambda *a, **k: seen.append(1) or (_ for _ in ()).throw(
                            AssertionError("engine must not run when policy denies"))):
            result = kernel._approval_stop(
                [self._shell_call()], project_path="E:/tmp",
                on_shell_request=lambda a: seen.append("approval"),
                turn=1, run_id="r", intent_sink=None,
                policy=policy, permission_profile="coding_writer",
            )
        self.assertIsNone(result)
        self.assertNotIn("approval", seen)

    def test_research_without_shell_grant_never_pauses(self) -> None:
        from codey.operations import task_loop as kernel

        policy = _strict_research_policy()
        self.assertFalse(policy.allows("shell.approval"))
        requested = []
        with mock.patch("codey.agents.tool_execution.evaluate_tool_call_policy_for",
                        side_effect=AssertionError("engine must not run when policy denies")):
            result = kernel._approval_stop(
                [self._shell_call()], project_path="E:/tmp",
                on_shell_request=lambda a: requested.append(a),
                turn=1, run_id="r", intent_sink=None,
                policy=policy, permission_profile="research",
            )
        self.assertIsNone(result)
        self.assertEqual(requested, [])

    def test_explicit_auth_uses_effective_profile_and_pauses(self) -> None:
        from codey.operations import task_loop as kernel

        policy = _project_policy()
        self.assertTrue(policy.allows("shell.approval"))
        captured: dict = {}
        approvals: list = []

        def fake_evaluate(call, *, project, permission_profile,
                          approval_available=False, phase="writer"):
            captured["profile"] = permission_profile
            decision = SimpleNamespace(decision="ask_user")
            return decision, SimpleNamespace()

        with mock.patch("codey.agents.tool_execution.evaluate_tool_call_policy_for",
                        fake_evaluate), mock.patch(
                            "codey.agents.tool_execution.policy_asks_user",
                            lambda d: True):
            result = kernel._approval_stop(
                [self._shell_call()], project_path="E:/tmp",
                on_shell_request=approvals.append,
                turn=2, run_id="r", intent_sink=None,
                policy=policy, permission_profile="research",
            )
        self.assertEqual(captured.get("profile"), "coding_writer")
        self.assertIsNotNone(result)
        self.assertEqual(result.stop_reason, "approval")
        self.assertEqual(len(approvals), 1)

    def test_effective_profile_shared_with_execution_delegate(self) -> None:
        from codey.operations import task_loop as kernel
        from codey.operations.task_execution import (
            ExecutionDelegate,
            effective_project_profile,
        )

        self.assertEqual(effective_project_profile("research"), "coding_writer")
        self.assertEqual(effective_project_profile("coding_writer"), "coding_writer")
        source = inspect.getsource(kernel._approval_stop)
        self.assertIn("effective_project_profile", source)
        delegate_source = inspect.getsource(ExecutionDelegate._project_permission_profile)
        self.assertIn("effective_project_profile", delegate_source)


class NativeReceiptFailClosedTests(unittest.TestCase):
    def _native_reply(self, calls):
        from codey.providers.base import AssistantTurn

        return AssistantTurn(text="", tool_calls=tuple(calls))

    def _native_call(self, cid, name="web_search", args=None):
        from codey.providers.base import ProviderToolCall

        return ProviderToolCall(id=cid, name=name,
                                arguments=dict(args or {"query": "q"}))

    def test_repair_dangling_send_failure_raises(self) -> None:
        from codey.operations import kernel_transport as transport

        class BoomProvider:
            def send_tool_results(self, messages, tools, timeout=None):
                raise TimeoutError("receipt boom")

            def acknowledge_tool_results(self, results, declared_tools, timeout=None):
                return self.send_tool_results(results, [])

        reply = self._native_reply([self._native_call("c1")])
        with self.assertRaises(TimeoutError):
            transport.repair_native_dangling(
                BoomProvider(), reply, True, [], "bad tool call")

    def test_take_answered_reply_send_failure_raises(self) -> None:
        from codey.operations import kernel_transport as transport

        class BoomProvider:
            def send_tool_results(self, messages, tools, timeout=None):
                raise TimeoutError("receipt boom")

            def acknowledge_tool_results(self, results, declared_tools, timeout=None):
                return self.send_tool_results(results, [])

        reply = self._native_reply([self._native_call("d1", "done", {"summary": "x"})])
        with self.assertRaises(TimeoutError):
            transport._take_answered_reply(
                BoomProvider(), reply, True, [], "not done yet")

    def test_protocol_error_with_failing_receipt_is_provider_failure(self) -> None:
        from codey.operations.task_loop import run_task_kernel
        from codey.operations.task_session import TaskSession
        from codey.providers.base import AssistantTurn, ProviderToolCall

        send_turns: list = []
        receipts: list = []

        class FakeNative:
            def send_turn(self, message, tools, timeout=None):
                send_turns.append(message)
                return AssistantTurn(text="", tool_calls=(
                    ProviderToolCall(id="bad1", name="frobnicate_xyz",
                                     arguments={}),))

            def send_tool_results(self, messages, tools, timeout=None):
                receipts.append(list(messages))
                raise TimeoutError("receipt boom")

            def acknowledge_tool_results(self, results, declared_tools, timeout=None):
                return self.send_tool_results(results, [])

        session = TaskSession(policy=_project_policy(), task_kind="project",
                              project="", max_turns=4)
        result = run_task_kernel(
            session,
            request=KernelRunRequest(
                transport=KernelTransportDeps(
                    provider=FakeNative(),
                    run_id="r-receipt",
                    provider_id="local",
                ),
                execution=KernelExecutionDeps(
                    executors={},
                ),
            ),
        )
        self.assertEqual(result.stop_reason, "provider_failure")
        self.assertEqual(len(send_turns), 1)
        self.assertEqual(len(receipts), 1)

    def test_done_rejection_with_failing_receipt_is_provider_failure(self) -> None:
        from codey.operations.task_loop import run_task_kernel
        from codey.operations.task_session import TaskSession
        from codey.providers.base import AssistantTurn, ProviderToolCall

        send_turns: list = []
        receipts: list = []

        class FakeNative:
            def send_turn(self, message, tools, timeout=None):
                send_turns.append(message)
                return AssistantTurn(text="", tool_calls=(
                    ProviderToolCall(id="d1", name="done",
                                     arguments={"answer": "太短无来源"}),))

            def send_tool_results(self, messages, tools, timeout=None):
                receipts.append(list(messages))
                raise TimeoutError("receipt boom")

            def acknowledge_tool_results(self, results, declared_tools, timeout=None):
                return self.send_tool_results(results, [])

        session = TaskSession(policy=_strict_research_policy(),
                              task_kind="research", project="", max_turns=4)
        result = run_task_kernel(
            session,
            request=KernelRunRequest(
                transport=KernelTransportDeps(
                    provider=FakeNative(),
                    run_id="r-done-receipt",
                    provider_id="local",
                ),
                execution=KernelExecutionDeps(
                    executors={},
                ),
            ),
        )
        self.assertEqual(result.stop_reason, "provider_failure")
        self.assertEqual(len(send_turns), 1)
        self.assertEqual(len(receipts), 1)

    def test_mixed_ids_without_call_id_stays_defensive(self) -> None:
        from codey.operations import kernel_transport as transport
        from codey.runtime.core.models import ToolCall, ToolResult

        session = SimpleNamespace()
        call = ToolCall("web_search", {"query": "q"}, "")
        result = ToolResult(ok=True, call=call, model_text="found")
        self.assertEqual(transport._native_tool_results([result], session), [])


class PromptAndTransportHygieneTests(unittest.TestCase):
    def test_coding_context_preparation_failure_returns_no_context_with_diagnostic(self) -> None:
        from codey.operations.project_prompt_context import prepare_coding_context
        from codey.operations.task_session import TaskSession
        from codey.policies.task_policy import TaskPolicy

        session = TaskSession(policy=TaskPolicy(grants=frozenset({"project.read"})), read_files={"a.py"})
        with mock.patch("codey.operations.project_prompt_context.project_completion_checks",
                        side_effect=RuntimeError("context boom")), self.assertLogs(
                            "codey.operations.project_prompt_context", level="WARNING") as logs:
            context = prepare_coding_context(session)
        self.assertIsNone(context)
        self.assertTrue(any("coding" in message.lower() or "context" in message.lower()
                            for message in logs.output))

    def test_provider_identification_failure_never_silently_uses_web(self) -> None:
        from codey.operations.task_loop import run_task_kernel
        from codey.operations.task_session import TaskSession

        sends: list = []

        class WebCountingProvider:
            def send(self, prompt, timeout=None):
                sends.append(prompt)
                return '{"tool":"done","args":{"summary":"x"}}'

        session = TaskSession(policy=_project_policy(), task_kind="project",
                              project="", max_turns=3)
        with mock.patch("codey.providers.native_tools.supports_native_tools",
                        side_effect=RuntimeError("capability boom")):
            result = run_task_kernel(
                session,
                request=KernelRunRequest(
                    transport=KernelTransportDeps(
                        provider=WebCountingProvider(),
                        run_id="r-ident",
                    ),
                    execution=KernelExecutionDeps(
                        executors={},
                    ),
                ),
            )
        self.assertEqual(result.stop_reason, "controller_failure")
        self.assertEqual(sends, [])


if __name__ == "__main__":
    unittest.main()
