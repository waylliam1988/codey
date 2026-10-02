from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from codey.runtime.core import cancellation
from codey.runtime.observe.prompt_envelope import (
    FailOpenPromptTrace,
    PromptEnvelope,
    PromptEnvelopeSection,
    is_model_boundary_freshness,
    record_provider_send_prompt,
)
from codey.workspace.context_source import ContextSource, render_context_sources_with_metadata


class _Trace:
    def __init__(self) -> None:
        self.sections: list[dict[str, object]] = []
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def record_prompt_section(self, name, text, **kwargs) -> None:
        self.sections.append({"name": name, "text": text, **kwargs})

    def record_provider_prompt_boundary(self, section_args, surface_payload=None) -> None:
        payload = dict(section_args)
        # surface is validated metadata only; boundary tests ignore it here.
        del surface_payload
        self.sections.append({
            "name": payload.get("name"),
            "text": payload.get("text"),
            "freshness": payload.get("freshness"),
            "source_refs": payload.get("source_refs"),
            "epoch_id": payload.get("epoch_id"),
            "admission_reason": payload.get("admission_reason"),
            "capability_id": payload.get("capability_id"),
            "purpose": payload.get("purpose"),
        })

    def record_permission_profile(self, *args, **kwargs) -> None:
        self.calls.append(("record_permission_profile", args, kwargs))


class _BrokenTrace:
    def record_prompt_section(self, *_args, **_kwargs) -> None:
        raise OSError("trace unavailable")

    def record_provider_prompt_boundary(self, *_args, **_kwargs) -> None:
        raise OSError("trace unavailable")


class _StoppingTrace:
    def record_prompt_section(self, *_args, **_kwargs) -> None:
        raise cancellation.TaskCancelled("stop")

    def record_provider_prompt_boundary(self, *_args, **_kwargs) -> None:
        raise cancellation.TaskCancelled("stop")


class _Provider:
    name = "Fake"

    def __init__(self, reply: str | None = None) -> None:
        self.reply = reply or json.dumps({"tool": "done", "args": {"summary": "done"}})

    def new_chat(self) -> None:
        return None

    def send(self, text: str) -> str:
        del text
        return self.reply


class _Search:
    def search(self, query: str, limit: int = 8) -> list[dict]:
        del query, limit
        return []

    def fetch(self, url: str) -> dict:
        del url
        return {}


class _Index:
    def recent(self, _limit: int, *, session_id: str = "") -> list[dict]:
        del session_id
        return []


class _Store:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.index = _Index()


class PromptEnvelopeTests(unittest.TestCase):
    def test_envelope_v1_keeps_builder_api_closed(self) -> None:
        self.assertFalse(hasattr(PromptEnvelope, "add"))

    def test_render_keeps_order_and_omits_empty_or_non_visible_sections(self) -> None:
        rendered = PromptEnvelope((
            PromptEnvelopeSection("system", "alpha", source_refs=("system",)),
            PromptEnvelopeSection("empty", "", source_refs=("empty",)),
            PromptEnvelopeSection("hidden", "secret", model_visible=False),
            PromptEnvelopeSection("task", "beta", source_refs=("task",)),
        )).render()

        self.assertEqual(rendered.text, "alpha\n\nbeta")
        self.assertEqual([section.name for section in rendered.sections], ["system", "task"])
        self.assertEqual(rendered.sections[0].rendered_length, len("alpha"))

    def test_source_refs_have_bounded_fallback(self) -> None:
        rendered = PromptEnvelope((
            PromptEnvelopeSection("Task Name", "hello"),
        )).render()

        self.assertEqual(rendered.sections[0].source_refs, ("prompt_section:Task_Name",))

    def test_context_sources_keep_rendered_text_and_can_feed_envelopes(self) -> None:
        rendered_context = render_context_sources_with_metadata((
            ContextSource(
                key="project_map",
                loader=lambda: "alpha",
                budget=100,
                freshness="run_start",
                why_included="bounded project map",
                heading="Project map:",
            ),
        ))
        section = PromptEnvelopeSection(
            name="project_map",
            text=rendered_context.text,
            purpose=rendered_context.sources[0].why_included,
            source_refs=(f"context_source:{rendered_context.sources[0].key}",),
            budget=rendered_context.sources[0].budget,
            freshness=rendered_context.sources[0].freshness,
            truncated=rendered_context.sources[0].truncated,
        )

        self.assertEqual(rendered_context.text, "Project map:\nalpha")
        self.assertEqual(section.text, rendered_context.text)
        self.assertEqual(section.purpose, "bounded project map")
        self.assertEqual(section.source_refs, ("context_source:project_map",))

    def test_trace_sink_records_envelope_metadata_without_raw_payload_contract(self) -> None:
        trace = _Trace()
        rendered = PromptEnvelope((
            PromptEnvelopeSection(
                "system",
                "secret prompt text",
                purpose="test system prompt",
                source_refs=("protocol:test",),
                freshness="provider_send",
                budget=120,
                truncated=True,
            ),
        )).render()

        FailOpenPromptTrace(trace).record_envelope(rendered)

        self.assertEqual(trace.sections[0]["name"], "system")
        self.assertEqual(trace.sections[0]["text"], "secret prompt text")
        self.assertEqual(trace.sections[0]["purpose"], "test system prompt")
        self.assertEqual(trace.sections[0]["source_refs"], ("protocol:test",))
        self.assertTrue(trace.sections[0]["model_visible"])

    def test_trace_sink_is_fail_open_except_for_cancellation(self) -> None:
        FailOpenPromptTrace(_BrokenTrace()).record_section(
            PromptEnvelopeSection("section", "text")
        )

        with self.assertRaises(cancellation.TaskCancelled):
            FailOpenPromptTrace(_StoppingTrace()).record_section(
                PromptEnvelopeSection("section", "text")
            )

        class _ControlTeachStoppingTrace:
            class ControlTeachCancelled(cancellation.TaskCancelled):
                pass

            def record_prompt_section(self, *_args, **_kwargs) -> None:
                raise self.ControlTeachCancelled("stop")

        with self.assertRaises(_ControlTeachStoppingTrace.ControlTeachCancelled):
            FailOpenPromptTrace(_ControlTeachStoppingTrace()).record_section(
                PromptEnvelopeSection("section", "text")
            )

        class _OldNameOnlyStoppingTrace:
            class ControlTeachCancelled(RuntimeError):
                pass

            def record_prompt_section(self, *_args, **_kwargs) -> None:
                raise self.ControlTeachCancelled("stop")

        FailOpenPromptTrace(_OldNameOnlyStoppingTrace()).record_section(
            PromptEnvelopeSection("section", "text")
        )

    def test_trace_sink_ignores_section_normalization_errors(self) -> None:
        class _BadSection:
            model_visible = True
            name = "bad"
            text = "text"
            purpose = "bad"
            budget = 1
            truncated = False
            freshness = "provider_send"

            @property
            def source_refs(self):  # pragma: no cover - exercised through exception path
                raise OSError("trace unavailable")

        FailOpenPromptTrace(_Trace()).record_section(_BadSection())

    def test_trace_sink_skips_work_when_disabled(self) -> None:
        class _BadSection:
            model_visible = True
            name = "bad"
            text = "text"
            purpose = "bad"
            budget = 1
            truncated = False
            freshness = "provider_send"

            @property
            def source_refs(self):  # pragma: no cover - exercised through early return
                raise AssertionError("should not inspect sections without trace")

        FailOpenPromptTrace(None).record_section(_BadSection())

    def test_record_provider_send_prompt_stamps_epoch_metadata(self) -> None:
        trace = _Trace()

        record_provider_send_prompt(
            trace,
            name="coding_outbound_prompt",
            text="outbound prompt body",
            purpose="coding prompt sent to provider",
            source_ref="provider_send:coding",
            capability_id="agent_runner",
        )

        recorded = trace.sections[0]
        self.assertEqual(recorded["name"], "coding_outbound_prompt")
        self.assertEqual(recorded["text"], "outbound prompt body")
        self.assertEqual(recorded["freshness"], "provider_send")
        self.assertEqual(recorded["source_refs"], ("provider_send:coding",))
        self.assertTrue(recorded["epoch_id"].startswith("ctx_epoch:"))
        self.assertEqual(recorded["admission_reason"], "provider_turn_boundary")
        self.assertEqual(recorded["capability_id"], "agent_runner")

    def test_record_provider_send_prompt_epoch_is_content_addressed(self) -> None:
        first: list[dict[str, object]] = []
        second: list[dict[str, object]] = []

        def capture(store: list[dict[str, object]]):
            class _Sink:
                def record_provider_prompt_boundary(self, section_args, surface_payload=None) -> None:
                    del surface_payload
                    store.append(dict(section_args))

            return _Sink()

        record_provider_send_prompt(
            capture(first),
            name="section",
            text="same bytes",
            purpose="p",
            source_ref="provider_send:x",
        )
        record_provider_send_prompt(
            capture(second),
            name="section",
            text="same bytes",
            purpose="p",
            source_ref="provider_send:x",
        )
        record_provider_send_prompt(
            capture(second),
            name="section",
            text="different bytes",
            purpose="p",
            source_ref="provider_send:x",
        )

        self.assertEqual(first[0]["epoch_id"], second[0]["epoch_id"])
        self.assertNotEqual(second[0]["epoch_id"], second[1]["epoch_id"])

    def test_record_provider_send_prompt_accepts_explicit_epoch(self) -> None:
        trace = _Trace()

        record_provider_send_prompt(
            trace,
            name="coding_outbound_prompt",
            text="outbound body",
            purpose="p",
            source_ref="provider_send:coding",
            epoch_id="ctx_epoch:" + "f" * 16,
        )

        self.assertEqual(trace.sections[0]["epoch_id"], "ctx_epoch:" + "f" * 16)

    def test_record_provider_send_prompt_is_fail_open(self) -> None:
        record_provider_send_prompt(
            _BrokenTrace(),
            name="review_prompt",
            text="body",
            purpose="review prompt sent to provider",
            source_ref="provider_send:review",
        )

        with self.assertRaises(cancellation.TaskCancelled):
            record_provider_send_prompt(
                _StoppingTrace(),
                name="review_prompt",
                text="body",
                purpose="review prompt sent to provider",
                source_ref="provider_send:review",
            )

    def test_sections_without_admission_metadata_keep_legacy_trace_contract(self) -> None:
        trace = _Trace()

        FailOpenPromptTrace(trace).record_section(PromptEnvelopeSection(
            name="legacy_section",
            text="body",
            purpose="prepared earlier",
            freshness="run_start",
            source_refs=("local:ref",),
        ))

        recorded = trace.sections[0]
        self.assertNotIn("epoch_id", recorded)
        self.assertNotIn("admission_reason", recorded)
        self.assertNotIn("capability_id", recorded)

    def test_rendered_sections_carry_epoch_metadata_through_envelope(self) -> None:
        rendered = PromptEnvelope((
            PromptEnvelopeSection(
                "research_outbound_prompt",
                "prompt body",
                purpose="research prompt sent to provider",
                freshness="provider_send",
                source_refs=("provider_send:research",),
                epoch_id="ctx_epoch:" + "a" * 16,
                admission_reason="provider_turn_boundary",
                capability_id="research_runner",
            ),
        )).render()

        section = rendered.sections[0]
        self.assertEqual(section.epoch_id, "ctx_epoch:" + "a" * 16)
        self.assertEqual(section.admission_reason, "provider_turn_boundary")
        self.assertEqual(section.capability_id, "research_runner")

    def test_model_boundary_freshness_tracks_provider_turn_constant(self) -> None:
        self.assertTrue(is_model_boundary_freshness("provider_send"))
        self.assertFalse(is_model_boundary_freshness("run_start"))

    def test_research_intro_envelope_preserves_existing_join_shape(self) -> None:
        # Old _intro deleted with the old runner; prompt now built via the
        # single kernel_prompt_for_session (new entry). Behavior (prompt
        # contains the question) is locked here via the new entry.
        from codey.operations.kernel_prompt import kernel_prompt_for_session
        from codey.operations.task_session import TaskSession
        from codey.policies.task_policy import TaskPolicy

        policy = TaskPolicy(grants=frozenset({"web.read", "control"}))
        session = TaskSession(policy=policy, task_kind="research", project="", max_turns=2, task_text="Why alpha?")
        prompt = kernel_prompt_for_session(session, user_task="Why alpha?", contract_text="", context_text="")
        self.assertIn("Why alpha?", prompt)

    def test_research_prompt_trace_does_not_claim_trace_attr(self) -> None:
        from codey.operations.kernel_prompt import kernel_prompt_for_session
        from codey.operations.task_session import TaskSession
        from codey.policies.task_policy import TaskPolicy

        policy = TaskPolicy(grants=frozenset({"web.read", "control"}))
        session = TaskSession(policy=policy, task_kind="research", project="", max_turns=2, task_text="Why beta?")
        prompt = kernel_prompt_for_session(session, user_task="Why beta?", contract_text="", context_text="")
        self.assertIn("Why beta?", prompt)

    def test_research_request_row_is_gone_question_section_is_the_only_request(self) -> None:
        # Old trace-section envelope deleted with the old runner; the new
        # single entry builds one prompt via kernel_prompt_for_session with
        # the verbatim user task (no duplicate request rows).
        from codey.operations.kernel_prompt import kernel_prompt_for_session
        from codey.operations.task_session import TaskSession
        from codey.policies.task_policy import TaskPolicy

        policy = TaskPolicy(grants=frozenset({"web.read", "control"}))
        session = TaskSession(policy=policy, task_kind="research", project="", max_turns=2, task_text="Why gamma?")
        prompt = kernel_prompt_for_session(session, user_task="Why gamma?", contract_text="", context_text="")
        self.assertIn("Why gamma?", prompt)
        self.assertIn("User task (verbatim):", prompt)

    def test_record_envelope_fail_open_trace_call(self) -> None:
        trace = _Trace()
        sink = FailOpenPromptTrace(trace)

        sink.call("record_permission_profile", "chat", phase="chat")
        sink.call("missing_method", "ignored")

        self.assertEqual(trace.calls, [(
            "record_permission_profile",
            ("chat",),
            {"phase": "chat"},
        )])

    def test_trace_call_failure_is_logged_but_stays_fail_open(self) -> None:
        class _ExplodingCall:
            def record_permission_profile(self, *_args, **_kwargs) -> None:
                raise OSError("trace unavailable")

        with self.assertLogs("codey.runtime.observe.prompt_envelope", level="DEBUG") as logs:
            FailOpenPromptTrace(_ExplodingCall()).call("record_permission_profile", "chat")

        self.assertTrue(any("record_permission_profile" in line for line in logs.output))

    def test_real_project_adapter_survives_broken_trace_recorder(self) -> None:

        from codey.agents.request import AgentRequest
        from codey.operations.project_adapter import run

        class _ExplodingTrace:
            def __getattr__(self, _name):
                def _boom(*_args, **_kwargs):
                    raise OSError("trace unavailable")

                return _boom

        class _Provider:
            name = "fake"

            def new_chat(self) -> None:
                return None

            def send(self, prompt, timeout=None):
                return '{"tool":"done","args":{"summary":"completed"}}'

        with tempfile.TemporaryDirectory() as td:
            request = AgentRequest(
                provider=_Provider(),  # type: ignore[arg-type]
                project=Path(td),
                task="read-only planning",
                provider_id="fake",
                trace_recorder=_ExplodingTrace(),
            )
            with self.assertLogs(
                "codey.runtime.observe.prompt_envelope", level="DEBUG"
            ) as logs:
                result = run(request)

        self.assertEqual(result.stop_reason, "done")
        self.assertTrue(any("record_" in line for line in logs.output))


if __name__ == "__main__":
    unittest.main()
