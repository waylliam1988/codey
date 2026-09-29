"""Red-first: Auto ACTION must hand a fresh window to executors.

Bug 2: run_auto_mode() does a second new_chat() after ACTION then sets
fresh_chat=False, so project/planning executors send the short
"continue" prompt instead of the full project_intro().
The mode executor must own the ACTION-after reset.
"""
from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from codey.operations.auto_loop import run_auto_mode
from codey.operations.result import ModeOutcome
from codey.task.model import TaskSubmission


class _CountingProvider:
    name = "Fake"

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.new_chat_calls = 0
        self.prompts: list[str] = []

    def new_chat(self, timeout: float | None = None) -> None:
        del timeout
        self.new_chat_calls += 1

    def send(self, text: str, timeout: float | None = None) -> str:
        del timeout
        self.prompts.append(text)
        return self.reply

    def close(self) -> None:
        pass


def _frame(task: str, provider: _CountingProvider, project: str = "/repo"):
    return SimpleNamespace(
        request=TaskSubmission("session-1", project, task, 8, False, "local"),
        run_id="run-1",
        task_kind="auto",
        provider=provider,
        provider_id="local",
        project_text=str(Path(project).expanduser().resolve()),
        conversation=mock.Mock(),
        fresh_chat=True,
        handoff="",
        trace=None,
        recovered_tool_outcomes=(),
    )


def _deps(state, mode_deps):
    from codey.operations.auto_loop import AutoRunDeps

    return AutoRunDeps(
        state=state,
        mode_deps=mode_deps,
        config_result=None,
        acquire_writer=lambda _p: True,
        release_writer=lambda _p: None,
        open_ledger_for=lambda _k: None,
    )


def _ok(mode: str) -> ModeOutcome:
    return ModeOutcome({
        "type": "task_done", "run_id": "run-1", "session_id": "session-1",
        "summary": "ok", "stop_reason": "done", "turns": 1,
        "max_turns": 8, "provider": "local", "mode": mode,
    })


class AutoFreshChatOwnershipTests(unittest.TestCase):
    def test_auto_first_prompt_receives_project_scoped_ghost_context(self) -> None:
        provider = _CountingProvider("ACTION: project\nPLAN: inspect")
        frame = _frame("inspect", provider)
        seen: list[tuple[str, str]] = []

        def scoped_context(kind: str):
            def load(*, session_id: str, project: str = ""):
                seen.append((session_id, project))
                text = f"{kind} project context" if project == "/repo" else ""
                return SimpleNamespace(text=text)
            return load

        mode_deps = SimpleNamespace(
            project=lambda *_args, **_kwargs: _ok("project"),
            research=mock.Mock(), planning=mock.Mock(), review=mock.Mock(),
        )
        deps = replace(
            _deps(mock.Mock(), mode_deps),
            ghost_directive_fn=scoped_context("Directive"),
            ghost_continuity_fn=scoped_context("Continuity"),
        )

        run_auto_mode(frame, SimpleNamespace(), SimpleNamespace(), deps)

        self.assertEqual(seen, [("session-1", "/repo"), ("session-1", "/repo")])
        self.assertIn("Directive project context", provider.prompts[0])
        self.assertIn("Continuity project context", provider.prompts[0])
        self.assertEqual(len(provider.prompts), 1)

    def test_project_handoff_is_fresh_and_auto_does_not_reset(self) -> None:
        provider = _CountingProvider("ACTION: project\nPLAN: fix it")
        frame = _frame("fix it", provider)
        seen: dict[str, object] = {}

        def run_project(active_frame, _work, _hooks, config_result=None):
            del _work, _hooks, config_result
            seen["fresh_chat"] = active_frame.fresh_chat
            # Auto must not have consumed the reset: exactly the decision
            # window reset so far; the executor owns the next one.
            seen["new_chats_at_handoff"] = provider.new_chat_calls
            return _ok("project")

        mode_deps = SimpleNamespace(
            project=run_project, research=mock.Mock(),
            planning=mock.Mock(), review=mock.Mock(),
        )
        run_auto_mode(
            frame, SimpleNamespace(), SimpleNamespace(),
            _deps(mock.Mock(), mode_deps),
        )
        self.assertTrue(
            seen.get("fresh_chat"),
            "project executor must see fresh_chat=True after Auto ACTION",
        )
        self.assertEqual(
            seen.get("new_chats_at_handoff"), 1,
            "Auto must not do the ACTION-after reset itself "
            "(only the decision-window reset)",
        )

    def test_planning_handoff_is_fresh_and_auto_does_not_reset(self) -> None:
        provider = _CountingProvider("ACTION: planning_readonly\nPLAN: look")
        frame = _frame("look", provider)
        seen: dict[str, object] = {}

        def run_planning(active_frame, _work, config_result=None):
            del _work, config_result
            seen["fresh_chat"] = active_frame.fresh_chat
            seen["new_chats_at_handoff"] = provider.new_chat_calls
            return _ok("planning_readonly")

        mode_deps = SimpleNamespace(
            project=mock.Mock(), research=mock.Mock(),
            planning=run_planning, review=mock.Mock(),
        )
        run_auto_mode(
            frame, SimpleNamespace(), SimpleNamespace(),
            _deps(mock.Mock(), mode_deps),
        )
        self.assertTrue(seen.get("fresh_chat"))
        self.assertEqual(seen.get("new_chats_at_handoff"), 1)

    def test_executor_reset_failure_must_not_reuse_action_window(self) -> None:
        # The executor owns the reset: if its own new_chat fails with
        # strict semantics, it must raise instead of reusing the window
        # that still contains the ACTION scaffolding.
        # Production entry: project_adapter strict fresh-chat path.
        import tempfile
        from codey.agents.request import AgentRequest

        with tempfile.TemporaryDirectory() as td:
            from pathlib import Path as _Path

            req = AgentRequest(
                provider=_FailingProvider(),  # type: ignore[arg-type]
                project=_Path(td),
                task="fix",
                on_event=lambda _e: None,
                fresh_chat=True,
                strict_fresh_chat=True,
                permission_profile="coding_writer",
                provider_id="local",
                max_turns=1,
            )
            from codey.operations import project_adapter as adapter

            with self.assertRaises(RuntimeError):
                adapter.run(req)

    def test_project_executor_sends_full_intro_exactly_once(self) -> None:
        """Near-real chain: one ACTION-after reset + full intro, no reuse."""
        provider = _CountingProvider("ACTION: project\nPLAN: fix it")
        frame = _frame("fix it", provider)

        def run_project(active_frame, _work, _hooks, config_result=None):
            del _work, _hooks, config_result
            # Mimic the real Writer first call: strict reset once, then the
            # full project intro (never the short "continue" prompt).
            if not active_frame.fresh_chat:
                provider.send(
                    "Continue with the established project and JSON tool protocol."
                )
                return _ok("project")
            provider.new_chat()
            provider.send(
                "PROJECT_INTRO with project instructions and JSON tool protocol"
            )
            return _ok("project")

        mode_deps = SimpleNamespace(
            project=run_project, research=mock.Mock(),
            planning=mock.Mock(), review=mock.Mock(),
        )
        run_auto_mode(
            frame, SimpleNamespace(), SimpleNamespace(),
            _deps(mock.Mock(), mode_deps),
        )
        # Decision reset (1) + executor reset (1) = exactly 2; the ACTION
        # scaffolding window is never reused.
        self.assertEqual(provider.new_chat_calls, 2)
        exec_prompts = provider.prompts[1:]
        self.assertEqual(len(exec_prompts), 1)
        self.assertIn("PROJECT_INTRO", exec_prompts[0])
        self.assertIn("tool protocol", exec_prompts[0].lower())

    def test_project_executor_reset_failure_does_not_execute(self) -> None:
        provider = _CountingProvider("ACTION: project\nPLAN: fix it")
        frame = _frame("fix it", provider)

        def run_project(active_frame, _work, _hooks, config_result=None):
            del _work, _hooks, config_result
            # Strict executor reset: failure must propagate, never fall
            # back to the ACTION window.
            raise RuntimeError("executor reset failed")

        mode_deps = SimpleNamespace(
            project=run_project, research=mock.Mock(),
            planning=mock.Mock(), review=mock.Mock(),
        )
        with self.assertRaisesRegex(RuntimeError, "executor reset failed"):
            run_auto_mode(
                frame, SimpleNamespace(), SimpleNamespace(),
                _deps(mock.Mock(), mode_deps),
            )
        # Only the decision prompt ran; no executor prompt leaked.
        self.assertEqual(len(provider.prompts), 1)


class _FailingProvider:
    name = "Failing"

    def new_chat(self, timeout: float | None = None) -> None:
        del timeout
        raise RuntimeError("reset failed")


if __name__ == "__main__":
    unittest.main()
