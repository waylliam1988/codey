"""One shared assistance switch across controls and flow.

Entering any suppress_assistance region -- controls-side or flow-side --
pauses every other assistance entry point until it unwinds. These tests pin
the cross-suppression contract (not just same-module behavior) plus the
task-boundary reset.
"""

from __future__ import annotations

import unittest
from unittest import mock

from codey.providers import assistance as assistance_module
from codey.providers import controls as provider_controls
from codey.providers import flow as provider_flow


def _multi_candidate_trace():
    trace = provider_flow.FlowTrace()
    trace.add(provider_flow.FlowObservation(stop_visible=True, typing_true=True))
    observation = provider_flow.FlowObservation(
        response_stable=True,
        response_nonempty=True,
        stop_hidden=True,
        typing_false=True,
        copy_visible=True,
    )
    trace.add(observation)
    trace.add(observation)
    return trace


class AssistanceSuppressionTests(unittest.TestCase):
    def tearDown(self) -> None:
        provider_flow.set_recovery_handler(None)
        provider_controls.set_teach_handler(None)
        provider_controls.set_doctor_handler(None)
        provider_controls.end_task_context()
        provider_flow.end_task_context()
        assistance_module.reset_assistance()

    def test_controls_suppress_blocks_flow_recovery_handler(self) -> None:
        trace = _multi_candidate_trace()
        handler = mock.Mock(return_value="f1")
        provider_flow.begin_task_context("session-1")
        provider_flow.set_recovery_handler(handler)

        with provider_controls.suppress_assistance():
            result = provider_flow.request_recovery(
                "qwen", provider_flow.STAGE_COMPLETION, trace, object()
            )

        self.assertIsNone(result)
        handler.assert_not_called()

    def test_flow_suppress_blocks_controls_teach(self) -> None:
        provider_controls.set_teach_handler(mock.Mock())
        self.assertTrue(provider_controls.can_teach())

        with provider_flow.suppress_assistance():
            self.assertFalse(provider_controls.can_teach())
            self.assertFalse(provider_controls.can_doctor())

        self.assertTrue(provider_controls.can_teach())

    def test_nested_suppress_unwinds_in_order(self) -> None:
        with provider_controls.suppress_assistance():
            self.assertTrue(assistance_module.assistance_suppressed())
            with provider_flow.suppress_assistance():
                self.assertTrue(assistance_module.assistance_suppressed())
            self.assertTrue(assistance_module.assistance_suppressed())
        self.assertFalse(assistance_module.assistance_suppressed())

    def test_task_boundaries_clear_shared_depth(self) -> None:
        with provider_controls.suppress_assistance():
            self.assertTrue(assistance_module.assistance_suppressed())
            provider_flow.begin_task_context("session-1")
            self.assertFalse(assistance_module.assistance_suppressed())
        with provider_flow.suppress_assistance():
            provider_flow.end_task_context()
            self.assertFalse(assistance_module.assistance_suppressed())


if __name__ == "__main__":
    unittest.main()
