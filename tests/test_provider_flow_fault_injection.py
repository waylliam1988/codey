from __future__ import annotations

import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

from codey import provider_controls as controls
from codey import provider_flow as flow
from codey import provider_revival as revival
from codey.provider_diagnostics import ResponseMissing


@dataclass(frozen=True)
class _DomFrame:
    """A text-free local DOM snapshot containing only approved flow facts."""

    response_nonempty: bool = False
    response_stable: bool = False
    stop_visible: bool | None = None
    typing: bool | None = None
    copy_visible: bool = False

    def observation(self) -> flow.FlowObservation:
        return flow.FlowObservation(
            response_nonempty=self.response_nonempty,
            response_stable=self.response_stable,
            stop_visible=self.stop_visible is True,
            stop_hidden=self.stop_visible is False,
            typing_true=self.typing is True,
            typing_false=self.typing is False,
            copy_visible=self.copy_visible,
        )


def _trace(*frames: _DomFrame) -> flow.FlowTrace:
    trace = flow.FlowTrace()
    for frame in frames:
        trace.add(frame.observation())
    return trace


class ProviderFlowFaultInjectionTests(unittest.TestCase):
    def tearDown(self) -> None:
        flow.set_recovery_handler(None)
        controls.end_task_context()

    def test_qwen_recovers_broken_builtin_completion_then_promotes(self) -> None:
        page = mock.Mock(url="https://chat.qwen.ai/")
        generating = _DomFrame(stop_visible=True)
        terminal = _DomFrame(
            response_nonempty=True,
            response_stable=True,
            stop_visible=False,
        )
        trace = _trace(generating, terminal, terminal)

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "provider-controls.json"

            @controls.revival_send("qwen")
            def recover(current_page):
                return controls.flow_stage_ready(
                    current_page,
                    "qwen",
                    flow.STAGE_COMPLETION,
                    trace,
                    terminal.observation(),
                    built_in_ready=False,
                    allow_recovery=True,
                )

            @controls.revival_send("qwen")
            def natural_success(current_page):
                return controls.flow_stage_ready(
                    current_page,
                    "qwen",
                    flow.STAGE_COMPLETION,
                    trace,
                    terminal.observation(),
                    built_in_ready=True,
                )

            helper = mock.Mock()
            flow.set_recovery_handler(helper)
            with mock.patch.object(controls, "CONTROL_STORE", path):
                controls.begin_task_context("fault-recovery")
                self.assertTrue(recover(page))
                provisional = controls.load_controls(path)["qwen"]["_revival"]

                controls.end_task_context()
                controls.begin_task_context("natural-success")
                self.assertTrue(natural_success(page))
                active = controls.load_controls(path)["qwen"]["_revival"]

        helper.assert_not_called()
        self.assertEqual(provisional["status"], "provisional")
        self.assertEqual(active["status"], "active")

    def test_qwen_thinking_pause_with_visible_stop_never_recovers(self) -> None:
        trace = _trace(
            _DomFrame(stop_visible=True),
            _DomFrame(
                response_nonempty=True,
                response_stable=True,
                stop_visible=True,
            ),
            _DomFrame(
                response_nonempty=True,
                response_stable=True,
                stop_visible=True,
            ),
        )
        helper = mock.Mock()
        flow.set_recovery_handler(helper)

        self.assertIsNone(
            flow.request_recovery(
                "qwen", flow.STAGE_COMPLETION, trace, object()
            )
        )
        helper.assert_not_called()

    def test_qwen_missing_terminal_marker_never_guesses_completion(self) -> None:
        trace = _trace(
            _DomFrame(response_nonempty=True, response_stable=True),
            _DomFrame(response_nonempty=True, response_stable=True),
        )
        helper = mock.Mock()
        flow.set_recovery_handler(helper)

        self.assertIsNone(
            flow.request_recovery(
                "qwen", flow.STAGE_COMPLETION, trace, object()
            )
        )
        helper.assert_not_called()

    def test_mimo_typing_transition_recovers_then_promotes(self) -> None:
        page = mock.Mock(url="https://aistudio.xiaomimimo.com/#/c")
        generating = _DomFrame(typing=True)
        terminal = _DomFrame(
            response_nonempty=True,
            response_stable=True,
            typing=False,
        )
        trace = _trace(generating, terminal, terminal)

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "provider-controls.json"

            @controls.revival_send("mimo")
            def recover(current_page):
                return controls.flow_stage_ready(
                    current_page,
                    "mimo",
                    flow.STAGE_COMPLETION,
                    trace,
                    terminal.observation(),
                    built_in_ready=False,
                    allow_recovery=True,
                )

            @controls.revival_send("mimo")
            def natural_success(current_page):
                return controls.flow_stage_ready(
                    current_page,
                    "mimo",
                    flow.STAGE_COMPLETION,
                    trace,
                    terminal.observation(),
                    built_in_ready=True,
                )

            helper = mock.Mock()
            flow.set_recovery_handler(helper)
            with mock.patch.object(controls, "CONTROL_STORE", path):
                controls.begin_task_context("mimo-flow-recovery")
                self.assertTrue(recover(page))
                provisional = controls.load_controls(path)["mimo"]["_revival"]

                controls.end_task_context()
                controls.begin_task_context("mimo-natural-success")
                self.assertTrue(natural_success(page))
                active = controls.load_controls(path)["mimo"]["_revival"]

        helper.assert_not_called()
        self.assertEqual(provisional["status"], "provisional")
        self.assertEqual(active["status"], "active")

    def test_mimo_unreadable_flow_response_rolls_back_after_second_failure(self) -> None:
        page = mock.Mock(url="https://aistudio.xiaomimimo.com/#/c")
        profile_digest = flow.profile_hash(controls.get_profile("mimo"))
        old_flow = {
            flow.STAGE_COMPLETION: (
                flow.PREDICATE_RESPONSE_STABLE,
                flow.PREDICATE_STOP_HIDDEN,
            )
        }
        current_flow = {
            flow.STAGE_COMPLETION: (
                flow.PREDICATE_RESPONSE_STABLE,
                flow.PREDICATE_TYPING_FALSE,
            )
        }
        terminal = _DomFrame(
            response_nonempty=True,
            response_stable=True,
            typing=False,
        )
        trace = _trace(_DomFrame(typing=True), terminal, terminal)

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "provider-controls.json"
            revival.complete_send(
                path,
                "mimo",
                "aistudio.xiaomimimo.com",
                {},
                set(),
                set(),
                staged_flow=old_flow,
                built_in_profile_hash=profile_digest,
            )
            revival.complete_send(
                path,
                "mimo",
                "aistudio.xiaomimimo.com",
                {},
                set(),
                set(),
                learned_flow_verified=True,
                built_in_profile_hash=profile_digest,
            )
            revival.complete_send(
                path,
                "mimo",
                "aistudio.xiaomimimo.com",
                {},
                set(),
                set(),
                staged_flow=current_flow,
                built_in_profile_hash=profile_digest,
            )

            @controls.revival_send("mimo")
            def unreadable(current_page):
                self.assertTrue(
                    controls.flow_stage_ready(
                        current_page,
                        "mimo",
                        flow.STAGE_COMPLETION,
                        trace,
                        terminal.observation(),
                        built_in_ready=False,
                    )
                )
                return controls.read_flow_response(
                    "mimo",
                    flow.STAGE_COMPLETION,
                    lambda: (_ for _ in ()).throw(RuntimeError("unreadable")),
                )

            with mock.patch.object(controls, "CONTROL_STORE", path):
                controls.begin_task_context("failure-1")
                with self.assertRaises(ResponseMissing):
                    unreadable(page)
                first = controls.load_controls(path)["mimo"]["_revival"]

                controls.end_task_context()
                controls.begin_task_context("failure-2")
                with self.assertRaises(ResponseMissing):
                    unreadable(page)
                restored = revival.load_flow_recipe(
                    path, "mimo", profile_digest
                )

        self.assertEqual(first["failures"], 1)
        self.assertEqual(restored, old_flow)

    def test_mimo_and_glm_text_stability_cannot_start_recovery(self) -> None:
        trace = _trace(
            _DomFrame(response_nonempty=True, response_stable=True),
            _DomFrame(response_nonempty=True, response_stable=True),
        )
        helper = mock.Mock()
        flow.set_recovery_handler(helper)

        for provider_id in ("mimo", "glm"):
            with self.subTest(provider_id=provider_id):
                self.assertIsNone(
                    flow.request_recovery(
                        provider_id,
                        flow.STAGE_COMPLETION,
                        trace,
                        object(),
                    )
                )
        helper.assert_not_called()
