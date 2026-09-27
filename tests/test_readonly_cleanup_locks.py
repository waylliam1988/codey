"""Deterministic locks for readonly-audit cleanup (TDD red-first).

Covers: dead profile_name param, test-only thin wrappers, test-exclusive
models/aliases, server._run_task compat entry, and three deduplications.
"""
from __future__ import annotations

import inspect
import unittest


class PromptSignatureLocks(unittest.TestCase):
    def test_render_has_no_dead_profile_name_param(self) -> None:
        from codey.toolchain import tool_prompt as tool_prompt

        params = set(inspect.signature(tool_prompt.render_coding_system_prompt).parameters)
        self.assertNotIn("profile_name", params)

    def test_render_without_name_still_separates_writer_and_readonly(self) -> None:
        from codey.toolchain import definition as tool_defs
        from codey.toolchain.tool_prompt import render_coding_system_prompt

        writer_defs = tool_defs.TOOL_DEFINITIONS
        writer_prompt = render_coding_system_prompt(
            writer_defs,
            allowed_tool_names={d.name for d in writer_defs},
        )
        readonly_names = {
            "list_dir", "read_file", "read_files", "grep",
            "find_references", "parallel", "done",
        }
        readonly_defs = tool_defs.definitions_for_tool_names(readonly_names)
        readonly_prompt = render_coding_system_prompt(
            readonly_defs,
            allowed_tool_names=set(readonly_names),
        )
        self.assertIn("Use edit for all file changes", writer_prompt)
        self.assertNotIn("Use edit for all file changes", readonly_prompt)
        self.assertIn("This phase is read-only", readonly_prompt)


class ThinWrapperLocks(unittest.TestCase):
    def test_no_test_only_run_command_wrapper(self) -> None:
        from codey.toolchain import runtime as tool_runtime

        self.assertFalse(hasattr(tool_runtime, "_is_allowed_run_command"))

    def test_run_policy_reached_directly(self) -> None:
        from codey.policies.run_command_semantics import is_allowed_run_command

        self.assertTrue(is_allowed_run_command(["python", "-m", "pytest", "-q"]))
        self.assertFalse(is_allowed_run_command(["pip", "install", "requests"]))

    def test_no_test_only_cdp_port_wrapper(self) -> None:
        from codey.automation import browser as browser

        self.assertFalse(hasattr(browser, "_ensure_cdp_port"))

class TestExclusiveModelLocks(unittest.TestCase):
    def test_no_test_only_context_snapshot_model(self) -> None:
        from codey.workspace import context_epoch as context_epoch

        self.assertFalse(hasattr(context_epoch, "ContextSnapshot"))
        # Admission projection stays available via ContextEpoch path.
        self.assertTrue(hasattr(context_epoch, "ContextEpoch"))
        self.assertTrue(hasattr(context_epoch, "admission_from_rendered_source"))

    def test_no_test_only_readiness_stale_class(self) -> None:
        from codey.providers import diagnostics as diagnostics

        self.assertFalse(hasattr(diagnostics, "ReadinessStale"))
        # The failure category constant stays in production use.
        self.assertEqual(diagnostics.FAILURE_READINESS_STALE, "readiness_stale")

    def test_failure_facts_cleaned_without_test_only_class(self) -> None:
        from types import SimpleNamespace

        from codey.providers.diagnostics import FAILURE_READINESS_STALE, capture_provider_failure

        class _Stale(TimeoutError):
            provider_failure_kind = FAILURE_READINESS_STALE
            provider_failure_stage = "new_chat"

            def __init__(self, message, *, facts=None) -> None:
                from codey.providers.diagnostics import sanitize_failure_facts

                self.provider_failure_facts = sanitize_failure_facts(facts)
                super().__init__(message)

        failure = capture_provider_failure(
            model="Qwen",
            action="new_chat",
            page=SimpleNamespace(url="https://chat.qwen.ai/", title=lambda: "Qwen"),
            error=_Stale("stale", facts={"composer_visible": True, "prompt": "secret"}),
        )
        self.assertEqual(failure.kind, FAILURE_READINESS_STALE)
        self.assertIn("composer_visible", failure.facts)
        self.assertNotIn("prompt", failure.facts)

    def test_no_research_url_denial_alias(self) -> None:
        from codey.policies import action as action

        self.assertFalse(hasattr(action, "research_url_denial_reason"))

    def test_url_guard_reached_directly_via_network_policy(self) -> None:
        from codey.policies.network import check_fetch_url

        self.assertEqual(
            check_fetch_url("http://example.com:99999/path", resolve=False),
            "invalid URL port",
        )
        self.assertIsNone(check_fetch_url("https://example.com/doc", resolve=False))


class ServerCompatEntryLocks(unittest.TestCase):
    def test_no_server_run_task_compat_wrapper(self) -> None:
        from codey.app import server as server

        self.assertFalse(hasattr(server, "_run_task"))

    def test_task_submit_run_task_is_direct_entry(self) -> None:
        import inspect

        from codey.app import task_submit as task_submit

        params = set(inspect.signature(task_submit.run_task).parameters)
        self.assertIn("get_state", params)


class ByteLimitLabelLocks(unittest.TestCase):
    def test_runtime_reuses_shared_byte_limit_label(self) -> None:
        from codey.toolchain import runtime as tool_runtime
        from codey.utils import scan_report as scan_report

        self.assertFalse(hasattr(tool_runtime, "_byte_limit_label"))
        self.assertTrue(hasattr(scan_report, "byte_limit_label"))
        self.assertEqual(scan_report.byte_limit_label(512), "512 bytes")
        self.assertEqual(scan_report.byte_limit_label(1023), "1023 bytes")
        self.assertEqual(scan_report.byte_limit_label(1024), "1 KiB")
        self.assertEqual(scan_report.byte_limit_label(1536), "1 KiB")
        self.assertEqual(scan_report.byte_limit_label(1024 * 1024), "1 MiB")
        self.assertEqual(scan_report.byte_limit_label(3 * 1024 * 1024), "3 MiB")


class ResponseCharsetLocks(unittest.TestCase):
    def test_charset_helper_lives_once_in_http_redirects(self) -> None:
        from codey.research import browser_search as browser_search
        from codey.research import connector_search as connector_search
        from codey.research import http_redirects as http_redirects

        self.assertFalse(hasattr(browser_search, "_text_response_charset"))
        self.assertFalse(hasattr(connector_search, "_response_charset"))
        self.assertTrue(hasattr(http_redirects, "response_charset"))

    def test_charset_helper_keeps_attribute_error_tolerance(self) -> None:
        from codey.research.http_redirects import response_charset

        class _Headers:
            def __init__(self, charset=None, explode=False) -> None:
                self._charset = charset
                self._explode = explode

            def get_content_charset(self):
                if self._explode:
                    raise AttributeError("no get_content_charset")
                return self._charset

        self.assertEqual(response_charset(_Headers("utf-8")), "utf-8")
        self.assertEqual(response_charset(_Headers(None)), "utf-8")
        self.assertEqual(response_charset(_Headers("gbk")), "gbk")
        self.assertEqual(response_charset(_Headers("x", explode=True)), "utf-8")
        self.assertEqual(response_charset(object()), "utf-8")


class LateResponsePollingLocks(unittest.TestCase):
    def test_common_owns_snapshot_polling_helper(self) -> None:
        from codey.providers.web_drivers import common as driver_common

        self.assertTrue(hasattr(driver_common, "wait_late_response_by_snapshot"))

    def test_glm_and_qwen_delegate_to_common_helper(self) -> None:
        import pathlib

        for name in ("glm.py", "qwen.py"):
            source = (pathlib.Path("codey/providers/web_drivers") / name).read_text(encoding="utf-8")
            self.assertIn("wait_late_response_by_snapshot", source)

    def test_snapshot_helper_detects_replaced_text_and_completion_gate(self) -> None:
        from unittest import mock

        from codey.providers.web_drivers import common as driver_common
        from codey.providers.web_drivers.common import wait_late_response_by_snapshot

        # Controlled virtual clock: no wall-clock dependence, so busy CI
        # cannot flake on a 50ms real-time window.
        now = {"t": 1000.0}

        def _now() -> float:
            return now["t"]

        def _wait(seconds: float) -> None:
            now["t"] += seconds

        # Count unchanged but text updated + generation complete -> final text.
        calls = {"complete": 0}

        def _complete() -> bool:
            calls["complete"] += 1
            return calls["complete"] >= 2

        with (
            mock.patch.object(driver_common.time, "time", side_effect=_now),
            mock.patch.object(driver_common.cancellation, "wait", side_effect=_wait),
        ):
            reply = wait_late_response_by_snapshot(
                response_count=lambda: 1,
                last_text=lambda: "replacement reply",
                generation_complete=_complete,
                final_text=lambda: "raw replacement reply",
                baseline=1,
                baseline_text="old reply",
                grace=10,
                tick=1,
            )
            self.assertEqual(reply, "raw replacement reply")

            # Generation never completes -> empty.
            empty = wait_late_response_by_snapshot(
                response_count=lambda: 2,
                last_text=lambda: "final reply",
                generation_complete=lambda: False,
                final_text=lambda: "raw final reply",
                baseline=1,
                grace=10,
                tick=1,
            )
            self.assertEqual(empty, "")


if __name__ == "__main__":
    unittest.main()
