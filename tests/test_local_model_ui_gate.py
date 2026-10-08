from __future__ import annotations

import json
import os
import tempfile
import unittest
from typing import Any
from unittest import mock

from codey.providers import api_transport
from codey.providers.api_provider import ApiProvider
from codey.providers.local_discovery import LocalEndpoint
from tests.test_api_generation_observations_no_replay import Response
from tools import local_model_ui_gate as gate


class LocalModelUiGateTests(unittest.TestCase):
    def test_research_setup_enables_only_isolated_knowledge_store(self) -> None:
        class State:
            knowledge_store: Any = None

        state = State()
        with tempfile.TemporaryDirectory() as root:
            gate._configure_research_store(state, gate.Path(root), ("research",))
            self.assertIsNotNone(state.knowledge_store)
            state.knowledge_store.close()

    def test_provider_history_records_request_and_response(self) -> None:
        provider = ApiProvider("http://127.0.0.1:1/v1", "model")
        with tempfile.TemporaryDirectory() as root, mock.patch.object(
            api_transport, "open_request", return_value=Response(b'{"choices":[]}'),
        ):
            path = os.path.join(root, "provider.jsonl")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write('{"type":"stale"}\n')
            with gate._provider_history_recorder("local", gate.Path(path)):
                provider._generate([], None)
            with open(path, encoding="utf-8") as handle:
                rows = [json.loads(line) for line in handle]
        self.assertEqual([row["type"] for row in rows], ["request", "wire_attempt", "wire_attempt", "response"])
        self.assertEqual(rows[0]["payload"]["model"], "model")

    def test_history_analysis_classifies_done_retry_as_completion_rejection(self) -> None:
        rows = [
            {"type": "request", "payload": {"messages": []}},
            {
                "type": "response",
                "payload": {"choices": [{"message": {"tool_calls": [
                    {"function": {"name": "done"}},
                ]}}]},
            },
            {
                "type": "request",
                "payload": {"messages": [{
                    "role": "tool",
                    "content": "ERROR: Not done yet (required check 'research_evidence_saved')",
                }]},
            },
            {
                "type": "response",
                "payload": {"choices": [{"message": {"tool_calls": [
                    {"function": {"name": "done"}},
                ]}}]},
            },
        ]
        with tempfile.TemporaryDirectory() as root:
            path = gate.Path(root) / "history.jsonl"
            path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
            report = gate.analyze_provider_history(path)
        self.assertEqual(report["done_calls"], 2)
        self.assertEqual(report["completion_rejections"], 1)
        self.assertTrue(report["done_retried_after_completion_rejection"])
        self.assertEqual(report["parse_errors"], 0)
        self.assertEqual(report["transport_errors"], 0)

    def test_history_analysis_counts_web_provider_json_tool_calls(self) -> None:
        rows = [
            {"type": "request", "provider": "qwen", "text": "prompt"},
            {"type": "response", "provider": "qwen", "text": '{"tool":"web_search","args":{}}'},
            {"type": "request", "provider": "qwen", "text": "ERROR: Not done yet (research_evidence_missing)"},
            {"type": "response", "provider": "qwen", "text": '{"tool":"done","args":{}}'},
        ]
        with tempfile.TemporaryDirectory() as root:
            path = gate.Path(root) / "history.jsonl"
            path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
            report = gate.analyze_provider_history(path)
        self.assertEqual(report["tool_calls"], ("web_search", "done"))
        self.assertEqual(report["done_calls"], 1)
        self.assertEqual(report["completion_rejections"], 1)

    def test_parse_cases_accepts_requested_ui_modes_and_rejects_unknown(self) -> None:
        self.assertEqual(
            gate.parse_cases("chat,coding,review,research,ghost"),
            ("chat", "coding", "review", "research", "ghost"),
        )
        with self.assertRaisesRegex(ValueError, "unsupported UI case"):
            gate.parse_cases("chat,unknown")

    def test_wait_terminal_requires_a_new_run(self) -> None:
        self.assertTrue(
            gate._terminal_belongs_to_run(
                {"run_id": "run-2", "stop_reason": "done"}, "run-2"
            )
        )
        self.assertFalse(
            gate._terminal_belongs_to_run(
                {"run_id": "run-1", "stop_reason": "done"}, "run-2"
            )
        )

    def test_terminal_success_reads_protocol_stop_reason(self) -> None:
        self.assertTrue(gate._terminal_succeeded({"type": "task_done", "stop_reason": "done"}))
        self.assertFalse(gate._terminal_succeeded({"type": "task_done", "stop_reason": "error"}))

    def test_research_session_is_created_without_project_context(self) -> None:
        class Page:
            def __init__(self) -> None:
                self.expressions: list[str] = []

            def evaluate(self, expression: str) -> None:
                self.expressions.append(expression)

        page = Page()
        gate._start_unscoped_chat(page)
        self.assertEqual(page.expressions, ["newSession(null)"])

    def test_research_gate_uses_user_task_and_production_supplies_contract(self) -> None:
        prompt = gate._research_gate_prompt()
        self.assertIn("First use web_search", prompt)
        self.assertNotIn("knowledge_write", prompt)
        self.assertNotIn("Strict research completion checklist", prompt)

    def test_browser_url_uses_operator_bootstrap(self) -> None:
        class Server:
            def launch_url(self, base_url: str) -> str:
                return base_url + "#codey_bootstrap=test-token"

        self.assertEqual(
            gate._browser_launch_url(Server(), "http://127.0.0.1:1234/"),
            "http://127.0.0.1:1234/#codey_bootstrap=test-token",
        )

    def test_preflight_requires_explicit_base_url(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(
            RuntimeError, "LOCAL_OPENAI_BASE_URL"
        ):
            gate._preflight()

    def test_preflight_accepts_available_web_provider(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            gate.provider_services,
            "provider_tab_availability",
            return_value={"qwen": True},
        ):
            self.assertEqual(gate._preflight("qwen"), ("qwen", "Qwen"))

    def test_preflight_rejects_unavailable_web_provider(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            gate.provider_services,
            "provider_tab_availability",
            return_value={"qwen": False},
        ), self.assertRaisesRegex(RuntimeError, "qwen.*unavailable"):
            gate._preflight("qwen")

    def test_preflight_uses_endpoint_default_model(self) -> None:
        endpoint = LocalEndpoint("http://127.0.0.1:1234/v1", ("model-from-probe",))
        with mock.patch.dict(
            os.environ,
            {"LOCAL_OPENAI_BASE_URL": endpoint.base_url},
            clear=True,
        ), mock.patch.object(
            gate,
            "probe_local_endpoint_detail",
            return_value=(endpoint, "ok"),
        ) as probe:
            self.assertEqual(gate._preflight(), (endpoint.base_url, "model-from-probe"))
        probe.assert_called_once_with(endpoint.base_url, api_key="", timeout=5)

    def test_preflight_rejects_endpoint_without_model(self) -> None:
        endpoint = LocalEndpoint("http://127.0.0.1:1234/v1", ())
        with mock.patch.dict(
            os.environ,
            {"LOCAL_OPENAI_BASE_URL": endpoint.base_url},
            clear=True,
        ), mock.patch.object(
            gate,
            "probe_local_endpoint_detail",
            return_value=(endpoint, "ok"),
        ), self.assertRaisesRegex(RuntimeError, "LOCAL_OPENAI_MODEL"):
            gate._preflight()

    def test_preflight_rejects_unadvertised_explicit_model(self) -> None:
        endpoint = LocalEndpoint("http://127.0.0.1:1234/v1", ("advertised",))
        with mock.patch.dict(
            os.environ,
            {
                "LOCAL_OPENAI_BASE_URL": endpoint.base_url,
                "LOCAL_OPENAI_MODEL": "typo-model",
            },
            clear=True,
        ), mock.patch.object(
            gate,
            "probe_local_endpoint_detail",
            return_value=(endpoint, "ok"),
        ), self.assertRaisesRegex(RuntimeError, "not advertised"):
            gate._preflight()


if __name__ == "__main__":
    unittest.main()
