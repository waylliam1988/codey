"""Red-first locks for the remaining unified kernel (TDD).

Covers: JSON/native parity, controller enforcement on both paths, web-only
multi-turn, done gate (search-only/edit-stale/strict vs non-strict), hybrid
interleaving, readonly denial, third-task generalization, recovery idempotency.
"""

from __future__ import annotations

import unittest


def _project_runtime(case):
    import tempfile
    from pathlib import Path

    from codey.workspace.revision import WorkspaceRevisionStore

    temporary = tempfile.TemporaryDirectory()
    case.addCleanup(temporary.cleanup)
    root = Path(temporary.name)
    project = root / "project"
    project.mkdir()
    return project, WorkspaceRevisionStore(root / "state")


def _policy(**kwargs):
    from codey.policies.task_policy import build_task_policy
    from codey.task.model import TaskSubmission

    base = {
        "session_id": "s1",
        "project": "demo",
        "task": "check docs then fix bug",
        "max_turns": 8,
        "continue_task": False,
        "provider_id": "local",
    }
    base.update(kwargs.pop("submission", {}))
    submission = TaskSubmission(**base)
    return build_task_policy(
        submission,
        task_kind=kwargs.pop("task_kind", "hybrid"),
        strict_research=kwargs.pop("strict_research", False),
    )


class NormalizeParityTests(unittest.TestCase):
    def test_same_snapshot_same_capability_json_and_native(self) -> None:
        from codey.operations.kernel_protocol import normalize_turn
        from codey.providers.base import AssistantTurn, ProviderToolCall

        policy = _policy(
            submission={"requested_capabilities": ("web.read",)},
            task_kind="hybrid",
        )
        json_plan = normalize_turn(
            '{"tool":"read_file","args":{"path":"app.py"}}',
            policy=policy,
        )
        self.assertEqual(json_plan.protocol_error, "")
        self.assertEqual(len(json_plan.calls), 1)

        native_plan = normalize_turn(
            AssistantTurn(
                text="",
                tool_calls=(ProviderToolCall(id="c1", name="read_file", arguments={"path": "app.py"}),),
            ),
            policy=policy,
        )
        self.assertEqual(native_plan.protocol_error, "")
        self.assertEqual(len(native_plan.calls), 1)
        self.assertEqual(native_plan.calls[0].name, json_plan.calls[0].name)

    def test_illegal_args_rejected_before_executor_on_both_paths(self) -> None:
        from codey.operations.kernel_protocol import normalize_turn
        from codey.providers.base import AssistantTurn, ProviderToolCall

        policy = _policy(
            submission={"requested_capabilities": ("web.read",)},
            task_kind="hybrid",
        )
        json_plan = normalize_turn(
            '{"tool":"read_file","args":{}}',
            policy=policy,
        )
        self.assertNotEqual(json_plan.protocol_error, "")
        self.assertEqual(json_plan.calls, [])

        native_plan = normalize_turn(
            AssistantTurn(
                text="",
                tool_calls=(ProviderToolCall(id="c1", name="read_file", arguments={}),),
            ),
            policy=policy,
        )
        self.assertNotEqual(native_plan.protocol_error, "")
        self.assertEqual(native_plan.calls, [])

    def test_native_rejection_answers_every_call_id(self) -> None:
        from codey.operations.kernel_execution import execute_turn
        from codey.operations.kernel_protocol import normalize_turn
        from codey.providers.base import AssistantTurn, ProviderToolCall

        policy = _policy(task_kind="planning")
        plan = normalize_turn(
            AssistantTurn(
                text="",
                tool_calls=(
                    ProviderToolCall(id="c1", name="edit", arguments={"path": "a.py", "content": "x"}),
                    ProviderToolCall(id="c2", name="edit", arguments={"path": "b.py", "content": "y"}),
                ),
            ),
            policy=policy,
        )
        # Planning must reject edit at parse time; execution must still answer
        # every native call id with a legal result instead of dropping one.
        self.assertNotEqual(plan.protocol_error, "")
        from codey.operations.task_session import TaskSession

        session = TaskSession(policy=policy, task_kind="planning", project="demo", max_turns=8)
        results = execute_turn(
            session,
            [
                # Synthesize the denied calls the parser refused, as the native
                # delivery layer would still need to answer each call id.
                __import__("codey.runtime.core.models", fromlist=["ToolCall"]).ToolCall(
                    "edit", {"path": "a.py", "content": "x"}, "c1"
                ),
                __import__("codey.runtime.core.models", fromlist=["ToolCall"]).ToolCall(
                    "edit", {"path": "b.py", "content": "y"}, "c2"
                ),
            ],
            executors={},
        )
        self.assertEqual(len(results), 2)
        ids = {r.call.call_id for r in results}
        self.assertEqual(ids, {"c1", "c2"})
        for result in results:
            self.assertIn("ERROR", result.model_text)

    def test_controller_denial_applies_to_json_and_native(self) -> None:
        from codey.operations.kernel_protocol import normalize_turn
        from codey.providers.base import AssistantTurn, ProviderToolCall

        policy = _policy(task_kind="research", strict_research=True)
        self.assertTrue(policy.allows("knowledge.write"))
        # No source opened yet -> controller forbids knowledge_write on both paths.
        json_plan = normalize_turn(
            '{"tool":"knowledge_write","args":{"type":"fact","title":"t","body":"b"}}',
            policy=policy,
            controller_allowed=("web_search",),
        )
        self.assertNotEqual(json_plan.protocol_error, "")
        self.assertIn("not allowed", json_plan.protocol_error)

        native_plan = normalize_turn(
            AssistantTurn(
                text="",
                tool_calls=(
                    ProviderToolCall(
                        id="c1",
                        name="knowledge_write",
                        arguments={"type": "fact", "title": "t", "body": "b"},
                    ),
                ),
            ),
            policy=policy,
            controller_allowed=("web_search",),
        )
        self.assertNotEqual(native_plan.protocol_error, "")


class WebOnlyLoopTests(unittest.TestCase):
    def test_web_only_model_completes_mixed_turns_without_native(self) -> None:
        from codey.operations.task_loop import run_task_kernel
        from codey.operations.task_session import TaskSession
        from codey.runtime.core.models import ToolResult

        policy = _policy(
            submission={"requested_capabilities": ("web.read",)},
            task_kind="hybrid",
        )
        replies = [
            '{"tool":"web_search","args":{"query":"login docs"}}',
            '{"tool":"open_url","args":{"url":"https://example.com/docs"}}',
            '{"tool":"read_file","args":{"path":"app.py"}}',
            '{"tool":"edit","args":{"path":"app.py","content":"fixed"}}',
            '{"tool":"run","args":{"command":"pytest -q"}}',
            '{"tool":"done","args":{"summary":"fixed and verified"}}',
        ]

        class WebOnlyProvider:
            def __init__(self):
                self.sent = []
                self.native_touched = False

            def new_chat(self, timeout=None):
                return None

            def send(self, text, timeout=None):
                self.sent.append(text)
                return replies.pop(0)

            def close(self):
                return None

        provider = WebOnlyProvider()
        self.assertFalse(hasattr(provider, "send_turn"))
        self.assertFalse(hasattr(provider, "send_tool_results"))

        def fake_web_search(call):
            return ToolResult(call=call, model_text="found https://example.com/docs")

        def fake_open(call):
            return ToolResult(call=call, model_text="page text about login")

        def fake_read(call):
            return ToolResult(call=call, model_text="old code")

        def fake_edit(call):
            (project / call.args["path"]).write_text(call.args["content"], encoding="utf-8")
            return ToolResult(call=call, model_text="edited app.py")

        def fake_run(call):
            # Structured exit code only; text never implies pass.
            return ToolResult(call=call, model_text="1 passed", audit={"exit_code": 0})

        project, store = _project_runtime(self)
        session = TaskSession(policy=policy, task_kind="hybrid", project=str(project), max_turns=12)
        outcome = run_task_kernel(
            session,
            provider=provider,
            executors={
                "web_search": fake_web_search,
                "open_url": fake_open,
                "read_file": fake_read,
                "edit": fake_edit,
                "run": fake_run,
            },
            project_path=project, workspace_revision_store=store,
        )
        self.assertTrue(outcome.completed)
        self.assertIn("fixed", outcome.summary)
        # Web path used send() repeatedly and never required native methods.
        self.assertGreaterEqual(len(provider.sent), 6)
        self.assertFalse(hasattr(provider, "send_turn"))

    def test_native_parity_same_completion(self) -> None:
        from codey.operations.task_loop import run_task_kernel
        from codey.operations.task_session import TaskSession
        from codey.providers.base import AssistantTurn, ProviderToolCall
        from codey.runtime.core.models import ToolResult

        policy = _policy(
            submission={"requested_capabilities": ("web.read",)},
            task_kind="hybrid",
        )
        turns = [
            AssistantTurn(
                text="",
                tool_calls=(ProviderToolCall(id="c1", name="web_search", arguments={"query": "q"}),),
            ),
            AssistantTurn(
                text="",
                tool_calls=(
                    ProviderToolCall(id="c2", name="open_url", arguments={"url": "https://example.com/docs"}),
                ),
            ),
            AssistantTurn(text='{"tool":"done","args":{"summary":"native done"}}'),
        ]

        class NativeProvider:
            def __init__(self):
                self.tool_results_sent = []

            def send_turn(self, message, tools, timeout=None):
                return turns.pop(0)

            def send_tool_results(self, messages, tools, timeout=None):
                self.tool_results_sent.append(messages)
                return turns.pop(0)

        session = TaskSession(policy=policy, task_kind="hybrid", project="demo", max_turns=8)
        outcome = run_task_kernel(
            session,
            provider=NativeProvider(),
            executors={
                "web_search": lambda call: ToolResult(call=call, model_text="found"),
                "open_url": lambda call: ToolResult(call=call, model_text="page"),
            },
            provider_id="local",
        )
        self.assertTrue(outcome.completed)


class CompletionGateTests(unittest.TestCase):
    def test_search_only_cannot_complete_strict_research(self) -> None:
        from codey.operations.completion_gate import evaluate
        from codey.operations.task_session import TaskSession

        policy = _policy(task_kind="research", strict_research=True)
        session = TaskSession(policy=policy, task_kind="research", project="demo", max_turns=8)
        session.record_search("q")
        verdict = evaluate(session, "summary with 结论 and 来源 markers")
        self.assertFalse(verdict.complete)
        self.assertTrue(verdict.followup)

    def test_stale_verification_cannot_complete_then_fresh_can(self) -> None:
        from codey.operations.completion_gate import evaluate
        from codey.operations.task_session import TaskSession

        policy = _policy(
            submission={"requested_capabilities": ("web.read",)},
            task_kind="hybrid",
        )
        session = TaskSession(policy=policy, task_kind="hybrid", project="demo", max_turns=8)
        session.set_workspace_state(2, "sha256:" + "a" * 64)
        session.record_search("q")
        session.record_open("https://example.com/docs")
        session.record_evidence("https://example.com/docs", "login excerpt")
        session.record_edit("app.py", revision=2)
        session.record_verification(
            "pytest -q", revision=1, passed=False, exit_code=1,
            workspace_revision=2, workspace_fingerprint="sha256:" + "a" * 64,
        )
        stale = evaluate(session, "fixed")
        self.assertFalse(stale.complete)

        session.record_verification(
            "pytest -q", revision=2, passed=True, exit_code=0,
            workspace_revision=2, workspace_fingerprint="sha256:" + "a" * 64,
        )
        fresh = evaluate(session, "fixed and verified")
        self.assertTrue(fresh.complete)
        self.assertIsNotNone(fresh.proof)
        self.assertEqual(len({fresh.proof.proof_id}), 1)

    def test_programming_with_web_needs_no_research_notes(self) -> None:
        from codey.operations.completion_gate import evaluate
        from codey.operations.task_session import TaskSession

        policy = _policy(
            submission={"requested_capabilities": ("web.read",)},
            task_kind="project",
        )
        self.assertFalse(policy.strict_research)
        session = TaskSession(policy=policy, task_kind="project", project="demo", max_turns=8)
        session.record_search("docs")
        session.record_open("https://example.com/docs")
        verdict = evaluate(session, "done, no code changes needed")
        self.assertTrue(verdict.complete)


class HybridAndPlanningTests(unittest.TestCase):
    def test_hybrid_interleaves_web_and_project_in_one_run(self) -> None:
        from codey.operations.task_loop import run_task_kernel
        from codey.operations.task_session import TaskSession
        from codey.runtime.core.models import ToolResult

        policy = _policy(
            submission={"requested_capabilities": ("web.read",)},
            task_kind="hybrid",
        )
        order: list[str] = []
        replies = [
            '{"tool":"web_search","args":{"query":"q"}}',
            '{"tool":"open_url","args":{"url":"https://example.com/a"}}',
            '{"tool":"read_file","args":{"path":"app.py"}}',
            '{"tool":"edit","args":{"path":"app.py","content":"x"}}',
            '{"tool":"run","args":{"command":"pytest -q"}}',
            '{"tool":"done","args":{"summary":"interleaved done"}}',
        ]

        class FakeWeb:
            def new_chat(self, timeout=None):
                return None

            def send(self, text, timeout=None):
                return replies.pop(0)

            def close(self):
                return None

        def make(name, text, exit_code=None):
            def fn(call):
                order.append(name)
                if name == "edit":
                    (project / call.args["path"]).write_text(call.args["content"], encoding="utf-8")
                audit = {"exit_code": exit_code} if exit_code is not None else {}
                return ToolResult(call=call, model_text=text, audit=audit)

            return fn

        project, store = _project_runtime(self)
        session = TaskSession(policy=policy, task_kind="hybrid", project=str(project), max_turns=12)
        outcome = run_task_kernel(
            session,
            provider=FakeWeb(),
            executors={
                "web_search": make("web_search", "found"),
                "open_url": make("open_url", "page"),
                "read_file": make("read_file", "code"),
                "edit": make("edit", "edited"),
                "run": make("run", "passed", exit_code=0),
            },
            project_path=project, workspace_revision_store=store,
        )
        self.assertTrue(outcome.completed)
        self.assertEqual(order, ["web_search", "open_url", "read_file", "edit", "run"])

    def test_readonly_planning_cannot_write(self) -> None:
        from codey.operations.kernel_execution import execute_turn
        from codey.operations.kernel_protocol import normalize_turn
        from codey.operations.task_session import TaskSession
        from codey.runtime.core.models import ToolCall

        policy = _policy(task_kind="planning")
        plan = normalize_turn('{"tool":"edit","args":{"path":"a.py","content":"x"}}', policy=policy)
        self.assertNotEqual(plan.protocol_error, "")

        session = TaskSession(policy=policy, task_kind="planning", project="demo", max_turns=8)
        calls = [ToolCall("edit", {"path": "a.py", "content": "x"}, "c1")]
        results = execute_turn(session, calls, executors={"edit": lambda c: (_ for _ in ()).throw(AssertionError("must not execute"))})
        self.assertEqual(len(results), 1)
        self.assertIn("ERROR", results[0].model_text)


class ThirdTaskTests(unittest.TestCase):
    def test_third_task_runs_without_kernel_change(self) -> None:
        from codey.operations import completion_gate as gate
        from codey.operations import task_loop as kernel
        from codey.operations.task_session import TaskSession
        from codey.runtime.core.models import ToolResult
        from codey.toolchain.tool_spec import register_custom_tool

        # Third-task adapters only: a ToolSpec row plus a profile-scoped check.
        # Neither edits the kernel loop nor leaks into other task kinds.
        register_custom_tool("summarize", grant="control",
                             parameters=(("text", {"type": "string"}),),
                             required=("text",), description="summarize text")
        gate.register_completion_check_provider(
            "summarize_present",
            lambda session: [
                gate.make_check(
                    "summary_present",
                    "pass" if "summary:" in session.notes_text() else "not_run",
                    "" if "summary:" in session.notes_text() else "summary_missing",
                )
            ],
            profile="project",
        )
        try:
            policy = _policy(task_kind="project")
            replies = [
                '{"tool":"summarize","args":{"text":"hello world"}}',
                '{"tool":"done","args":{"summary":"summary: hello"}}',
            ]

            class FakeWeb:
                def new_chat(self, timeout=None):
                    return None

                def send(self, text, timeout=None):
                    return replies.pop(0)

                def close(self):
                    return None

            session = TaskSession(policy=policy, task_kind="project", project="demo", max_turns=8)
            outcome = kernel.run_task_kernel(
                session,
                provider=FakeWeb(),
                executors={"summarize": lambda call: ToolResult(call=call, model_text="summary: hello")},
            )
            self.assertTrue(outcome.completed)
        finally:
            gate.unregister_completion_check_provider("summarize_present")


class RecoveryTests(unittest.TestCase):
    def test_resume_does_not_repeat_dangerous_actions(self) -> None:
        # Call identity is run+turn+index (durable intent slots), never
        # tool+args: same-slot retries reuse, new turns re-execute.
        import tempfile
        from pathlib import Path

        from codey.operations.kernel_execution import execute_turn
        from codey.operations.task_session import TaskSession
        from codey.runtime.core.models import ToolCall, ToolResult
        from codey.workspace.revision import WorkspaceRevisionStore

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        project = root / "project"
        project.mkdir()
        store = WorkspaceRevisionStore(root / "state")

        policy = _policy(
            submission={"requested_capabilities": ("web.read",)},
            task_kind="hybrid",
        )
        calls_made: list[str] = []

        def counting(name):
            def fn(call):
                calls_made.append(name)
                if name == "edit":
                    (project / call.args["path"]).write_text(call.args["content"], encoding="utf-8")
                return ToolResult(call=call, model_text=f"{name} ok", audit={"exit_code": 0} if name == "run" else {})

            return fn

        executors = {
            "edit": counting("edit"),
            "run": counting("run"),
            "knowledge_write": counting("knowledge_write"),
            "open_url": counting("open_url"),
        }
        session = TaskSession(policy=policy, task_kind="hybrid", project=str(project), max_turns=8)
        calls = [
            ToolCall("edit", {"path": "a.py", "content": "x"}),
            ToolCall("run", {"command": "pytest -q"}),
            ToolCall("knowledge_write", {"type": "fact", "title": "t", "body": "b"}),
            ToolCall("open_url", {"url": "https://example.com/a"}),
        ]
        first = execute_turn(session, calls, executors=executors, run_id="run-1", turn=1,
                             project_path=project, workspace_revision_store=store)
        self.assertEqual(len(first), 4)
        self.assertEqual(calls_made, ["edit", "run", "knowledge_write", "open_url"])

        # Same turn slot retried (crash before delivery): no re-execution.
        calls_made.clear()
        second = execute_turn(session, calls, executors=executors, run_id="run-1", turn=1,
                              project_path=project, workspace_revision_store=store)
        self.assertEqual(len(second), 4)
        self.assertEqual(calls_made, [])
        self.assertEqual([r.model_text for r in second], [r.model_text for r in first])

        # New turn with identical args is a new call: reads and post-edit
        # verifications legitimately run again.
        calls_made.clear()
        third = execute_turn(session, calls, executors=executors, run_id="run-1", turn=2,
                             project_path=project, workspace_revision_store=store)
        self.assertEqual(calls_made, ["edit", "run", "knowledge_write", "open_url"])
        self.assertEqual(len(third), 4)

    def test_provider_switch_keeps_results(self) -> None:
        from codey.operations.kernel_execution import execute_turn
        from codey.operations.kernel_protocol import normalize_turn
        from codey.operations.task_session import TaskSession, turn_effect_id
        from codey.runtime.core.models import ToolResult

        policy = _policy(
            submission={"requested_capabilities": ("web.read",)},
            task_kind="hybrid",
        )
        session = TaskSession(policy=policy, task_kind="hybrid", project="demo", max_turns=8)
        executed: list[str] = []

        def fake_search(call):
            executed.append("web_search")
            return ToolResult(call=call, model_text="found https://example.com/a")

        # First run executes once; the durable delivery carries the full
        # result across the switch.
        plan = normalize_turn('{"tool":"web_search","args":{"query":"q"}}', policy=policy)
        results = execute_turn(session, plan.calls, executors={"web_search": fake_search},
                               run_id="run-1", turn=1)
        self.assertEqual(len(results), 1)
        delivered = {turn_effect_id("run-1", 1, 0): results[0]}
        # Explicit state copy: same identity + facts.
        revived = TaskSession(policy=policy, task_kind="hybrid", project="demo", max_turns=8)
        revived.searches = list(session.searches)
        revived.search_results = dict(session.search_results)
        again = execute_turn(revived, plan.calls, executors={"web_search": fake_search},
                             run_id="run-1", turn=1, delivered=delivered)
        self.assertEqual(executed, ["web_search"])
        self.assertEqual(again[0].model_text, results[0].model_text)


if __name__ == "__main__":
    unittest.main()
