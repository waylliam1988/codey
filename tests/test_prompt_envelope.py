from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from codey import cancellation
from codey.context_source import ContextSource, render_context_sources_with_metadata
from codey.prompt_envelope import (
    FailOpenPromptTrace,
    PromptEnvelope,
    PromptEnvelopeSection,
)
from codey.research.controller import controller_system_prompt
from codey.research.runner import ResearchRunner


class _Trace:
    def __init__(self) -> None:
        self.sections: list[dict[str, object]] = []
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def record_prompt_section(self, name, text, **kwargs) -> None:
        self.sections.append({"name": name, "text": text, **kwargs})

    def record_permission_profile(self, *args, **kwargs) -> None:
        self.calls.append(("record_permission_profile", args, kwargs))


class _BrokenTrace:
    def record_prompt_section(self, *_args, **_kwargs) -> None:
        raise OSError("trace unavailable")


class _StoppingTrace:
    def record_prompt_section(self, *_args, **_kwargs) -> None:
        raise cancellation.TaskCancelled("stop")


class _Provider:
    name = "Fake"

    def __init__(self, reply: str | None = None) -> None:
        self.reply = reply or json.dumps({"tool": "done", "args": {"answer": "done"}})

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
            class ControlTeachCancelled(RuntimeError):
                pass

            def record_prompt_section(self, *_args, **_kwargs) -> None:
                raise self.ControlTeachCancelled("stop")

        with self.assertRaises(_ControlTeachStoppingTrace.ControlTeachCancelled):
            FailOpenPromptTrace(_ControlTeachStoppingTrace()).record_section(
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

    def test_research_intro_envelope_preserves_existing_join_shape(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = _Store(Path(td))
            runner = ResearchRunner(_Provider(), _Search(), store, session_id="s")

            intro = runner._intro("Why alpha?")

        expected = "\n\n".join((
            controller_system_prompt(include_source_search=True),
            "Your local knowledge library is empty; this is the first run.",
            "Research question:\nWhy alpha?",
        ))
        self.assertEqual(intro, expected)

    def test_research_prompt_trace_does_not_claim_trace_attr(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = _Store(Path(td))
            runner = ResearchRunner(_Provider(), _Search(), store, session_id="s")
            runner.trace = object()

            intro = runner._intro("Why beta?")

        self.assertIn("Research question:\nWhy beta?", intro)

    def test_research_request_trace_is_distinct_from_model_question(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = _Store(Path(td))
            trace = _Trace()
            runner = ResearchRunner(
                _Provider(),
                _Search(),
                store,
                session_id="s",
                controller_enabled=False,
                trace_recorder=trace,
            )

            list(runner.run("Why gamma?"))

        names = [section["name"] for section in trace.sections]
        self.assertEqual(names[0], "research_request")
        self.assertIn("research_question", names)

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


if __name__ == "__main__":
    unittest.main()
