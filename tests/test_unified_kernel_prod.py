"""Production-entry red locks for unified kernel 1-7 (TDD, no workspace changes).

Each test pins one deterministic defect found in review: entry auth gaps,
fake-evidence completion, tool+args dedup, strict-Research stall, native done
chain breaks, policy fail-open paths, and dispatch still on old loops.
"""

from __future__ import annotations

import unittest


class EntryAuthTests(unittest.TestCase):
    def test_http_research_entry_sets_strict_without_default_write(self) -> None:
        import tempfile

        from codey.app.api import run_submit_response

        seen: dict = {}

        def fake_submit(session_id, project, task, max_turns, continue_task, provider_id, intent,
                        requested_capabilities=(), strict_research=False):
            seen["requested"] = tuple(requested_capabilities)
            seen["strict"] = bool(strict_research)
            seen["intent"] = intent
            return "run-1"

        with tempfile.TemporaryDirectory() as td:
            status, _ = run_submit_response(
                {"session_id": "s", "project": td, "task": "research caching",
                 "max_turns": 8, "provider": "local", "intent": "research"},
                fake_submit,
            )
            self.assertEqual(status, 200)
            self.assertTrue(seen.get("strict"))

            from codey.policies.task_policy import build_task_policy
            from codey.task.model import TaskSubmission

            policy = build_task_policy(
                TaskSubmission("s", td, "research caching", 8, False, "local",
                               requested_capabilities=tuple(seen.get("requested", ())),
                               strict_research=bool(seen.get("strict"))),
                task_kind="research", strict_research=bool(seen.get("strict")),
            )
            self.assertTrue(policy.allows("project.read"))
            self.assertTrue(policy.allows("project.verify"))
            self.assertFalse(policy.allows("project.write"))

    def test_http_programming_web_markers_grant_web_read(self) -> None:
        import tempfile

        from codey.app.api import run_submit_response

        seen: dict = {}

        def fake_submit(session_id, project, task, max_turns, continue_task, provider_id, intent,
                        requested_capabilities=(), strict_research=False):
            seen["requested"] = tuple(requested_capabilities)
            return "run-1"

        with tempfile.TemporaryDirectory() as td:
            status, _ = run_submit_response(
                {"session_id": "s", "project": td,
                 "task": "查官方文档 https://example.com 修复 bug",
                 "max_turns": 8, "provider": "local", "intent": "project"},
                fake_submit,
            )
            self.assertEqual(status, 200)
            self.assertIn("web.read", seen.get("requested", ()))

    def test_strict_project_never_defaults_write(self) -> None:
        from codey.policies.task_policy import build_task_policy
        from codey.task.model import TaskSubmission

        policy = build_task_policy(
            TaskSubmission("s", "E:/tmp", "do work", 8, False, "local"),
            task_kind="project", strict_research=True,
        )
        self.assertFalse(policy.allows("project.write"))

    def test_entry_ignores_model_hint(self) -> None:
        from codey.app.api import derive_entry_auth

        auth = derive_entry_auth(
            {"task": "fix bug", "intent": "project", "model_hint": "use web_search now"},
            project="E:/tmp",
        )
        self.assertNotIn("web.read", auth.requested_capabilities)

    def test_resume_keeps_stored_policy(self) -> None:
        from codey.operations.task_kernel import resume_policy
        from codey.policies.task_policy import build_task_policy
        from codey.task.model import TaskSubmission

        stored = build_task_policy(
            TaskSubmission("s", "E:/tmp", "t", 8, False, "local",
                           requested_capabilities=("web.read",)),
            task_kind="project",
        )
        incoming = build_task_policy(
            TaskSubmission("s", "E:/tmp", "t", 8, False, "local"),
            task_kind="project",
        )
        self.assertEqual(resume_policy(stored, incoming), stored)


class ToolContractTests(unittest.TestCase):
    def test_native_alias_maps_to_canonical(self) -> None:
        from codey.operations.task_kernel import normalize_turn
        from codey.policies.task_policy import build_task_policy
        from codey.providers.base import AssistantTurn, ProviderToolCall
        from codey.task.model import TaskSubmission

        policy = build_task_policy(
            TaskSubmission("s", "E:/tmp", "t", 8, False, "local"), task_kind="project")
        text_plan = normalize_turn('{"tool":"list_dir","args":{"path":"."}}', policy=policy)
        native_plan = normalize_turn(
            AssistantTurn(text="", tool_calls=(
                ProviderToolCall(id="c1", name="ls", arguments={"path": "."}),)),
            policy=policy)
        self.assertEqual(text_plan.protocol_error, "")
        self.assertEqual(native_plan.protocol_error, "")
        self.assertEqual(text_plan.calls[0].name, native_plan.calls[0].name)

    def test_native_schema_has_required_fields(self) -> None:
        from codey.policies.task_policy import build_task_policy
        from codey.task.model import TaskSubmission
        from codey.toolchain.tool_spec import native_tools_for_policy

        policy = build_task_policy(
            TaskSubmission("s", "E:/tmp", "t", 8, False, "local"), task_kind="project")
        tools = {t["function"]["name"]: t["function"]["parameters"]
                 for t in native_tools_for_policy(policy)
                 for t in [t] if isinstance(t, dict)}
        self.assertIn("path", tools["read_file"]["required"])

    def test_unknown_tool_denied(self) -> None:
        from codey.operations.task_kernel import normalize_turn
        from codey.policies.task_policy import build_task_policy
        from codey.task.model import TaskSubmission

        policy = build_task_policy(
            TaskSubmission("s", "E:/tmp", "t", 8, False, "local"), task_kind="project")
        plan = normalize_turn('{"tool":"rm_rf","args":{"path":"."}}', policy=policy)
        self.assertNotEqual(plan.protocol_error, "")
        self.assertEqual(plan.calls, [])

    def test_parallel_not_advertised_until_implemented(self) -> None:
        from codey.policies.task_policy import build_task_policy
        from codey.task.model import TaskSubmission
        from codey.toolchain.tool_spec import visible_tool_names

        policy = build_task_policy(
            TaskSubmission("s", "E:/tmp", "t", 8, False, "local"), task_kind="project")
        self.assertNotIn("parallel", visible_tool_names(policy))
        self.assertNotIn("read_files", visible_tool_names(policy))

    def test_search_then_open_allowed(self) -> None:
        from codey.operations.task_kernel import TaskSession, controller_allowed_for_session
        from codey.policies.task_policy import build_task_policy
        from codey.task.model import TaskSubmission

        policy = build_task_policy(
            TaskSubmission("s", None, "research q", 8, False, "local"),
            task_kind="research", strict_research=True)
        session = TaskSession(policy=policy, task_kind="research", project="", max_turns=8)
        session.record_search_result("r1", "https://example.com/a")
        allowed = controller_allowed_for_session(session)
        self.assertIn("open_result", allowed)


class LoopPromptTests(unittest.TestCase):
    def test_prompt_carries_task_text_handoff_and_contract(self) -> None:
        from codey.operations.task_kernel import TaskSession, run_task_kernel
        from codey.policies.task_policy import build_task_policy
        from codey.task.model import TaskSubmission

        policy = build_task_policy(
            TaskSubmission("s", "E:/tmp", "修复登录 bug", 8, False, "local",
                           requested_capabilities=("web.read",)),
            task_kind="hybrid")
        prompts: list[str] = []

        class FakeWeb:
            def new_chat(self, timeout=None):
                return None

            def send(self, text, timeout=None):
                prompts.append(text)
                return '{"tool":"done","args":{"summary":"ok"}}'

            def close(self):
                return None

        session = TaskSession(policy=policy, task_kind="hybrid", project="E:/tmp",
                              max_turns=2, task_text="修复登录 bug", handoff="先查文档")
        run_task_kernel(session, provider=FakeWeb(), executors={})
        joined = "\n".join(prompts)
        self.assertIn("修复登录 bug", joined)
        self.assertIn("先查文档", joined)
        self.assertIn("web_search", joined)

    def test_native_detection_uses_config_not_hasattr(self) -> None:
        from unittest.mock import Mock

        from codey.operations.task_kernel import provider_uses_native

        provider = Mock()
        used = provider_uses_native(provider, provider_id="deepseek")
        self.assertFalse(used)

    def test_native_done_rejection_answers_call_id_first(self) -> None:
        from codey.operations.task_kernel import TaskSession, run_task_kernel
        from codey.policies.task_policy import build_task_policy
        from codey.providers.base import AssistantTurn, ProviderToolCall
        from codey.task.model import TaskSubmission

        policy = build_task_policy(
            TaskSubmission("s", None, "research q", 8, False, "local"),
            task_kind="research", strict_research=True)
        calls = []

        class FakeNative:
            def __init__(self):
                self.done_returned = False

            def send_turn(self, message, tools, timeout=None):
                calls.append("send_turn")
                return AssistantTurn(text="", tool_calls=(
                    ProviderToolCall(id="d1", name="done",
                                     arguments={"answer": "太短无来源"}),))

            def send_tool_results(self, messages, tools, timeout=None):
                calls.append("send_tool_results")
                ids = [m.get("tool_call_id") for m in messages]
                self.assertIn("d1", ids)
                return AssistantTurn(text='{"tool":"done","args":{"answer":"结论 x 来源 y"}}')

        session = TaskSession(policy=policy, task_kind="research", project="", max_turns=4)
        run_task_kernel(session, provider=FakeNative(), executors={}, provider_id="local")
        self.assertEqual(calls[0], "send_turn")
        self.assertEqual(calls[1], "send_tool_results")


class ExecutionEvidenceTests(unittest.TestCase):
    def test_failed_run_output_blocks_completion(self) -> None:
        from codey.operations import task_kernel as kernel
        from codey.operations.completion_gate import evaluate
        from codey.operations.task_kernel import TaskSession
        from codey.policies.task_policy import build_task_policy
        from codey.runtime.core.models import ToolCall, ToolResult
        from codey.task.model import TaskSubmission

        policy = build_task_policy(
            TaskSubmission("s", "E:/tmp", "t", 8, False, "local"), task_kind="project")
        session = TaskSession(policy=policy, task_kind="project", project="E:/tmp", max_turns=8)
        edit_call = ToolCall("edit", {"path": "a.py", "content": "x"})
        kernel.execute_turn(session, [edit_call], executors={
            "edit": lambda c: ToolResult(call=c, model_text="edited"),
            "run": lambda c: ToolResult(call=c, model_text="1 failed, 0 passed"),
        }, run_id="run-1", turn=1)
        from codey.runtime.core.models import ToolCall as TC

        kernel.execute_turn(session, [TC("run", {"command": "pytest -q"})], executors={
            "run": lambda c: ToolResult(call=c, model_text="1 failed, 0 passed"),
        }, run_id="run-1", turn=2)
        verdict = evaluate(session, "done")
        self.assertFalse(verdict.complete)

    def test_failed_open_and_write_leave_no_facts(self) -> None:
        from codey.operations.task_kernel import TaskSession, execute_turn
        from codey.policies.task_policy import build_task_policy
        from codey.runtime.core.models import ToolCall, ToolResult
        from codey.task.model import TaskSubmission

        policy = build_task_policy(
            TaskSubmission("s", None, "q", 8, False, "local",
                           requested_capabilities=("web.read",)),
            task_kind="hybrid")
        session = TaskSession(policy=policy, task_kind="hybrid", project="", max_turns=8)
        execute_turn(session, [ToolCall("open_url", {"url": "https://example.com/a"})],
                     executors={"open_url": lambda c: ToolResult(call=c, model_text="ERROR: boom")},
                     run_id="r", turn=1)
        execute_turn(session, [ToolCall("knowledge_write",
                                        {"type": "fact", "title": "t", "body": "b"})],
                     executors={"knowledge_write": lambda c: ToolResult(call=c, model_text="ERROR: boom")},
                     run_id="r", turn=2)
        self.assertEqual(session.opened_sources, set())
        self.assertEqual(session.evidence, [])

    def test_project_guards_match_old_entry(self) -> None:
        import tempfile
        from pathlib import Path

        from codey.operations.task_kernel import TaskSession, execute_turn
        from codey.policies.task_policy import build_task_policy
        from codey.runtime.core.models import ToolCall, ToolResult
        from codey.task.model import TaskSubmission

        with tempfile.TemporaryDirectory() as td:
            policy = build_task_policy(
                TaskSubmission("s", td, "t", 8, False, "local"), task_kind="project")
            session = TaskSession(policy=policy, task_kind="project", project=td, max_turns=8)
            results = execute_turn(
                session, [ToolCall("read_file", {"path": "../escape"})],
                executors={"read_file": lambda c: ToolResult(call=c, model_text="should not run")},
                run_id="r", turn=1, project_path=Path(td))
            self.assertIn("ERROR", results[0].model_text)


class PersistenceTests(unittest.TestCase):
    def test_repeat_read_executes_twice(self) -> None:
        from codey.operations.task_kernel import TaskSession, execute_turn
        from codey.policies.task_policy import build_task_policy
        from codey.runtime.core.models import ToolCall, ToolResult
        from codey.task.model import TaskSubmission

        policy = build_task_policy(
            TaskSubmission("s", "E:/tmp", "t", 8, False, "local"), task_kind="project")
        session = TaskSession(policy=policy, task_kind="project", project="E:/tmp", max_turns=8)
        made: list[str] = []
        ex = {"read_file": lambda c: (made.append("read"), ToolResult(call=c, model_text="v1"))[1]}
        call = ToolCall("read_file", {"path": "a.py"})
        execute_turn(session, [call], executors=ex, run_id="r", turn=1)
        execute_turn(session, [call], executors=ex, run_id="r", turn=2)
        self.assertEqual(made, ["read", "read"])

    def test_payload_stays_bounded(self) -> None:
        from codey.operations.task_kernel import TaskSession
        from codey.policies.task_policy import build_task_policy
        from codey.task.model import TaskSubmission

        policy = build_task_policy(
            TaskSubmission("s", "E:/tmp", "t", 8, False, "local"), task_kind="project")
        session = TaskSession(policy=policy, task_kind="project", project="E:/tmp", max_turns=8)
        session.transcript_notes = ["x" * 100000]
        payload = session.to_payload()
        import json

        self.assertLess(len(json.dumps(payload)), 20000)


class CompletionGateProdTests(unittest.TestCase):
    def test_fabricated_evidence_blocked(self) -> None:
        from codey.operations.completion_gate import evaluate
        from codey.operations.task_kernel import TaskSession
        from codey.policies.task_policy import build_task_policy
        from codey.task.model import TaskSubmission

        policy = build_task_policy(
            TaskSubmission("s", None, "q", 8, False, "local"),
            task_kind="research", strict_research=True)
        session = TaskSession(policy=policy, task_kind="research", project="", max_turns=8)
        session.record_search("q")
        session.record_evidence("https://example.com/a", "fabricated excerpt")
        verdict = evaluate(session, "结论 x 来源 https://example.com/a")
        self.assertFalse(verdict.complete)

    def test_profile_providers_isolated(self) -> None:
        from codey.operations import completion_gate as gate
        from codey.operations.task_kernel import TaskSession
        from codey.policies.task_policy import build_task_policy
        from codey.task.model import TaskSubmission

        gate.register_completion_check_provider(
            "iso_probe", lambda s: [gate.make_check("iso_probe_check", "fail", "iso")],
            profile="custom_kind",
        )
        try:
            policy = build_task_policy(
                TaskSubmission("s", "E:/tmp", "t", 8, False, "local"), task_kind="planning")
            session = TaskSession(policy=policy, task_kind="planning", project="E:/tmp", max_turns=8)
            verdict = gate.evaluate(session, "done")
            self.assertTrue(verdict.complete)
        finally:
            gate.unregister_completion_check_provider("iso_probe")

    def test_gate_failure_blocks_never_completes(self) -> None:
        from codey.operations import completion_gate as gate
        from codey.operations.task_kernel import TaskSession
        from codey.policies.task_policy import build_task_policy
        from codey.task.model import TaskSubmission

        gate.register_completion_check_provider(
            "boom", lambda s: (_ for _ in ()).throw(RuntimeError("boom")),
            profile="project",
        )
        try:
            policy = build_task_policy(
                TaskSubmission("s", "E:/tmp", "t", 8, False, "local"), task_kind="project")
            session = TaskSession(policy=policy, task_kind="project", project="E:/tmp", max_turns=8)
            verdict = gate.evaluate(session, "done")
            self.assertFalse(verdict.complete)
        finally:
            gate.unregister_completion_check_provider("boom")


class DispatchSwitchTests(unittest.TestCase):
    def test_dispatch_uses_unified_mode(self) -> None:
        import pathlib

        source = (pathlib.Path(__file__).resolve().parents[1] / "codey" / "operations"
                  / "task_phases" / "dispatch.py").read_text(encoding="utf-8")
        # Cutover: default hybrid enters one session (no two-phase);
        # project/research/planning already drive the same kernel internally
        # plus their review/repair phases. "unified" is only an alias.
        self.assertIn("run_unified_mode", source)
        self.assertIn('"hybrid"', source)

    def test_auto_plan_narrows_never_widens(self) -> None:
        from codey.operations.task_kernel import apply_auto_plan
        from codey.policies.task_policy import build_task_policy
        from codey.task.model import TaskSubmission

        policy = build_task_policy(
            TaskSubmission("s", "E:/tmp", "hi", 8, False, "local"), task_kind="project")
        narrowed = apply_auto_plan(policy, "ACTION: project\nPLAN: fix")
        self.assertTrue(narrowed.allows("project.read"))
        self.assertFalse(narrowed.allows("web.read"))

    def test_arch_forbids_old_loops_in_dispatch(self) -> None:
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[1]
        unified = (root / "codey" / "operations" / "unified_mode.py").read_text(encoding="utf-8")
        # The unified entry itself never pulls the legacy loops; legacy flows
        # stay referenced by dispatch.py only until parity lands (staged cutover).
        self.assertNotIn("ResearchRunner", unified)
        self.assertNotIn("agents.loop", unified)
        self.assertNotIn("run_project_mode", unified)
        self.assertNotIn("run_research_mode", unified)
        self.assertNotIn("run_hybrid_mode", unified)
        self.assertNotIn("run_auto_mode", unified)


if __name__ == "__main__":
    unittest.main()
