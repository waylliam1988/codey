"""Agent-level admission of the completion repair context (0.4.13)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from codey.agents import runner as agent
from codey.completion.repair_context import (
    CONTEXT_SOURCE_KEY,
    project_repair_context,
)
from codey.workspace.context_epoch import context_epoch_id
from codey.agents.handoff import ConversationContext


class FakeProvider:
    name = "Fake Provider"
    location = "fake://provider"

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.sent: list[str] = []
        self.new_chat_calls = 0

    def new_chat(self) -> None:
        self.new_chat_calls += 1

    def send(self, _text: str, timeout: float | None = None) -> str:
        del timeout
        self.sent.append(_text)
        return self.replies.pop(0)

    def close(self) -> None:
        pass


class _CapturingTrace:
    """Records the calls the admission chain makes, FailOpen-compatible."""

    def __init__(self) -> None:
        self.sections: list[dict] = []
        self.source_rows: list[dict] = []
        self.admissions: list[dict] = []

    def record_prompt_section(self, name, text, **kwargs) -> None:
        self.sections.append({"name": name, "text": text, **kwargs})

    def record_context_sources(self, sources, *, epoch_id="", **kwargs) -> None:
        self.source_rows.append({
            "sources": [
                {
                    "key": getattr(source, "key", ""),
                    "text": getattr(source, "text", ""),
                }
                for source in sources
            ],
            "epoch_id": epoch_id,
            **kwargs,
        })

    def record_completion_repair_context(self, payload, *, epoch_id="") -> None:
        self.admissions.append({"payload": payload, "epoch_id": epoch_id})

    def __getattr__(self, _name):
        def call(*_args, **_kwargs):
            return None
        return call


def _repair_context() -> tuple[str, dict[str, object]]:
    projection = project_repair_context(
        proof={
            "status": "failed",
            "proof_id": "completion_proof:" + "a" * 16,
            "contract_id": "completion_contract:" + "b" * 16,
            "reason_codes": ["relevant_verification_failed"],
            "checks": [
                {
                    "check_id": "relevant_verification",
                    "status": "fail",
                    "reason_code": "relevant_verification_failed",
                }
            ],
        },
        failure_class="product_failure",
        decisive_checks=[{
            "command": "pytest -q",
            "cwd": ".",
            "exit_code": 1,
            "result_summary": "1 failed",
        }],
    )
    return projection.prompt_text, projection.to_payload()


_DONE = '{"tool":"done","args":{"summary":"fixed"}}'


class ContinuationAdmissionTests(unittest.TestCase):
    def run_agent(self, trace, provider, root, context, **overrides):
        kwargs = dict(
            on_event=lambda _event: None,
            fresh_chat=False,
            conversation=context,
            provider_id="deepseek",
            permission_profile="coding_writer",
            trace_recorder=trace,
        )
        text, payload = _repair_context()
        kwargs["completion_repair_context"] = text
        kwargs["completion_repair_context_payload"] = payload
        kwargs.update(overrides)
        return agent.run(provider, root, "Fix the failing test", **kwargs)

    def test_repair_context_rides_the_first_outbound_prompt(self) -> None:
        trace = _CapturingTrace()
        provider = FakeProvider(_DONE)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            context = ConversationContext(hard_limit=100_000)
            context.begin_window("deepseek", "project", str(root))
            result = self.run_agent(trace, provider, root, context)

        self.assertEqual(result.stop_reason, "done")
        self.assertIn("Completion repair context. Facts only.", provider.sent[0])
        # Exactly once: later turns (none here) must not repeat it.
        self.assertEqual(
            sum("Completion repair context." in prompt for prompt in provider.sent),
            1,
        )

    def test_admission_rows_bind_to_the_outbound_epoch_and_record_once(self) -> None:
        trace = _CapturingTrace()
        provider = FakeProvider(_DONE)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            context = ConversationContext(hard_limit=100_000)
            context.begin_window("deepseek", "project", str(root))
            self.run_agent(trace, provider, root, context)

        bound = [
            row
            for row in trace.source_rows
            if any(item["key"] == CONTEXT_SOURCE_KEY for item in row["sources"])
        ]
        self.assertEqual(len(bound), 1)
        expected_epoch = context_epoch_id(provider.sent[0])
        self.assertEqual(bound[0]["epoch_id"], expected_epoch)

        self.assertEqual(len(trace.admissions), 1)
        admission = trace.admissions[0]
        self.assertEqual(admission["epoch_id"], expected_epoch)
        self.assertTrue(admission["payload"]["admitted"])
        self.assertNotIn("1 failed", json.dumps(admission["payload"]))

    def test_gate_closed_profile_never_admits_or_records(self) -> None:
        trace = _CapturingTrace()
        provider = FakeProvider(_DONE)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            context = ConversationContext(hard_limit=100_000)
            context.begin_window("deepseek", "project", str(root))
            self.run_agent(trace, provider, root, context, permission_profile="planning_readonly")

        self.assertNotIn("Completion repair context.", provider.sent[0])
        for row in trace.source_rows:
            self.assertTrue(
                all(item["key"] != CONTEXT_SOURCE_KEY for item in row["sources"])
            )
        self.assertEqual(trace.admissions, [])

    def test_followup_rides_a_literal_envelope_bound_to_the_send_epoch(self) -> None:
        # The continuation path assembles its prompt through PromptEnvelope
        # like every other admission: both the follow-up request and the
        # repair-facts section are recorded against the sent bytes' epoch.
        trace = _CapturingTrace()
        provider = FakeProvider(_DONE)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            context = ConversationContext(hard_limit=100_000)
            context.begin_window("deepseek", "project", str(root))
            result = self.run_agent(trace, provider, root, context)

        self.assertEqual(result.stop_reason, "done")
        expected_epoch = context_epoch_id(provider.sent[0])
        repair_sections = [
            row for row in trace.sections if row["name"] == CONTEXT_SOURCE_KEY
        ]
        request_sections = [
            row for row in trace.sections if row["name"] == "coding_followup_request"
        ]
        self.assertEqual(len(repair_sections), 1)
        self.assertEqual(len(request_sections), 1)
        self.assertEqual(repair_sections[0]["epoch_id"], expected_epoch)
        self.assertEqual(request_sections[0]["epoch_id"], expected_epoch)
        self.assertEqual(repair_sections[0]["freshness"], "after_tool_result")
        self.assertEqual(
            repair_sections[0]["capability_id"], "completion_repair_context"
        )
        self.assertIn("Completion repair context. Facts only.", provider.sent[0])

    def test_empty_repair_context_leaves_baseline_byte_identical(self) -> None:
        plain_provider = FakeProvider(_DONE)
        empty_provider = FakeProvider(_DONE)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            plain_context = ConversationContext(hard_limit=100_000)
            plain_context.begin_window("deepseek", "project", str(root))
            agent.run(
                plain_provider,
                root,
                "Fix the failing test",
                on_event=lambda _e: None,
                fresh_chat=False,
                conversation=plain_context,
                provider_id="deepseek",
            )
            empty_trace = _CapturingTrace()
            empty_context = ConversationContext(hard_limit=100_000)
            empty_context.begin_window("deepseek", "project", str(root))
            agent.run(
                empty_provider,
                root,
                "Fix the failing test",
                on_event=lambda _e: None,
                fresh_chat=False,
                conversation=empty_context,
                provider_id="deepseek",
                completion_repair_context="",
                completion_repair_context_payload=None,
                trace_recorder=empty_trace,
            )

        # Empty parameters are indistinguishable from absent ones: normal
        # runs pay nothing and see nothing.
        self.assertEqual(plain_provider.sent[0], empty_provider.sent[0])
        self.assertEqual(empty_trace.admissions, [])


class FreshIntroAdmissionTests(unittest.TestCase):
    def test_fresh_intro_carries_section_and_admission_row(self) -> None:
        trace = _CapturingTrace()
        provider = FakeProvider(_DONE)
        text, payload = _repair_context()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            result = agent.run(
                provider,
                root,
                "Fix the failing test",
                on_event=lambda _e: None,
                fresh_chat=True,
                provider_id="deepseek",
                permission_profile="coding_writer",
                completion_repair_context=text,
                completion_repair_context_payload=payload,
                trace_recorder=trace,
            )

        self.assertEqual(result.stop_reason, "done")
        self.assertIn("Completion repair context. Facts only.", provider.sent[0])
        epoch = context_epoch_id(provider.sent[0])
        # Coding intros carry sources inside coding_request_context; the
        # per-source rows come from record_context_sources, not sections.
        bound = [
            row
            for row in trace.source_rows
            if any(item["key"] == CONTEXT_SOURCE_KEY for item in row["sources"])
        ]
        self.assertEqual(len(bound), 1)
        self.assertEqual(bound[0]["epoch_id"], epoch)
        self.assertEqual(len(trace.admissions), 1)
        self.assertEqual(trace.admissions[0]["epoch_id"], epoch)


class RolloverDiscardTests(unittest.TestCase):
    def run_agent(self, trace, provider, root, context, **overrides):
        kwargs = dict(
            on_event=lambda _event: None,
            fresh_chat=False,
            conversation=context,
            provider_id="deepseek",
            permission_profile="coding_writer",
            trace_recorder=trace,
        )
        text, payload = _repair_context()
        kwargs["completion_repair_context"] = text
        kwargs["completion_repair_context_payload"] = payload
        kwargs.update(overrides)
        return agent.run(provider, root, "Fix the failing test", **kwargs)

    def test_rollover_discards_stale_rows_and_readmits_on_the_real_intro(self) -> None:
        # A prepared repair-context section binds to nothing until its prompt
        # actually leaves. When a rollover replaces that prompt wholesale,
        # the stale prepared rows must be discarded -- never attributed to
        # bytes that never left -- and the fresh intro re-admits exactly once.
        trace = _CapturingTrace()
        provider = FakeProvider(
            '{"goal": "fix", "current_state": "repair pending"}',
            _DONE,
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            context = ConversationContext(hard_limit=100_000)
            context.begin_window("deepseek", "project", str(root))
            # Park used_tokens just under the soft limit so the followup
            # prompt plus its repair section trips needs_rollover().
            context.used_tokens = int(context.soft_limit) - 10
            result = self.run_agent(trace, provider, root, context)

        self.assertEqual(result.stop_reason, "done")
        self.assertEqual(len(provider.sent), 2)
        # Send 1 is the handoff summary probe; it never carries the section.
        self.assertIn("fresh model chat", provider.sent[0])
        self.assertNotIn("Completion repair context.", provider.sent[0])
        # Send 2 is the fresh intro; it carries the section exactly once.
        self.assertIn("Completion repair context. Facts only.", provider.sent[1])

        intro_epoch = context_epoch_id(provider.sent[1])
        bound = [
            row
            for row in trace.source_rows
            if any(item["key"] == CONTEXT_SOURCE_KEY for item in row["sources"])
        ]
        self.assertEqual(len(bound), 1)
        self.assertEqual(bound[0]["epoch_id"], intro_epoch)
        self.assertNotEqual(bound[0]["epoch_id"], context_epoch_id(provider.sent[0]))

        # No stale section binding may survive: whatever the fresh intro
        # recorded (the intro embeds sources into coding_request_context,
        # so a standalone section may not exist at all), every recorded
        # repair section carries the intro's epoch -- never empty or an
        # earlier send's.
        self.assertEqual(
            [
                row
                for row in trace.sections
                if row["name"] == CONTEXT_SOURCE_KEY
                and row.get("epoch_id") != intro_epoch
            ],
            [],
        )

        # One admission row, bound to the only outbound prompt that carried
        # the section: assembled-before-rollover never became admitted.
        self.assertEqual(len(trace.admissions), 1)
        admission = trace.admissions[0]
        self.assertEqual(admission["epoch_id"], intro_epoch)
        self.assertTrue(admission["payload"]["admitted"])


if __name__ == "__main__":
    unittest.main()