from __future__ import annotations

import unittest
from pathlib import Path
import tempfile

from codey.runtime import cancellation
from codey.runtime.prompt_envelope import record_provider_send_prompt
from codey.runtime.prompt_surface import (
    PROMPT_SURFACE_SCHEMA_VERSION,
    PromptSurfaceSection,
    build_prompt_surface_record,
    prompt_surface_id,
    validate_prompt_surface_payload,
)
from codey.runs.trace import RunTraceStore


class _BrokenTrace:
    def record_prompt_section(self, *_args, **_kwargs) -> None:
        raise OSError("trace unavailable")

    def record_prompt_surface(self, *_args, **_kwargs) -> None:
        raise OSError("trace unavailable")


class _StoppingTrace:
    def record_prompt_section(self, *_args, **_kwargs) -> None:
        raise cancellation.TaskCancelled("stop")

    def record_prompt_surface(self, *_args, **_kwargs) -> None:
        raise cancellation.TaskCancelled("stop")


class _CaptureTrace:
    def __init__(self) -> None:
        self.sections: list[dict] = []
        self.surfaces: list[dict] = []

    def record_prompt_section(self, name, text, **kwargs) -> None:
        self.sections.append({"name": name, "text": text, **kwargs})

    def record_prompt_surface(self, payload) -> None:
        self.surfaces.append(dict(payload))


class PromptSurfaceTests(unittest.TestCase):
    def test_prompt_surface_id_is_deterministic_per_send(self) -> None:
        first = prompt_surface_id(phase="writer", send_ref="effect_1", prompt_digest="sha256:" + "b" * 64)
        second = prompt_surface_id(phase="writer", send_ref="effect_1", prompt_digest="sha256:" + "b" * 64)
        self.assertEqual(first, second)
        # same content but different send_ref must be distinct surface
        third = prompt_surface_id(phase="writer", send_ref="effect_2", prompt_digest="sha256:" + "b" * 64)
        self.assertNotEqual(first, third)
        # same send_ref but different phase must differ
        fourth = prompt_surface_id(phase="research", send_ref="effect_1", prompt_digest="sha256:" + "b" * 64)
        self.assertNotEqual(first, fourth)

    def test_build_and_validate_prompt_surface(self) -> None:
        section = PromptSurfaceSection(name="coding_outbound_prompt", digest="sha256:" + "c" * 64, chars=100, source_refs=("provider_send:coding",))
        record = build_prompt_surface_record(
            phase="writer",
            send_ref="effect_1",
            prompt_digest="sha256:" + "d" * 64,
            prompt_chars=100,
            epoch_id="ctx_epoch:" + "e" * 16,
            source_refs=("provider_send:coding",),
            provider_effect_id="effect_1",
            model_tool_contract_hash="sha256:" + "f" * 64,
            runtime_tool_contract_hash="",
            sections=(section,),
        )
        payload = {
            "schema_version": PROMPT_SURFACE_SCHEMA_VERSION,
            "surface_id": record.surface_id,
            "send_ref": record.send_ref,
            "phase": record.phase,
            "prompt_digest": record.prompt_digest,
            "prompt_chars": record.prompt_chars,
            "epoch_id": record.epoch_id,
            "source_refs": list(record.source_refs),
            "provider_effect_id": record.provider_effect_id,
            "model_tool_contract_hash": record.model_tool_contract_hash,
            "runtime_tool_contract_hash": record.runtime_tool_contract_hash,
            "sections": [
                {
                    "name": section.name,
                    "digest": section.digest,
                    "chars": section.chars,
                    "source_refs": list(section.source_refs),
                    "model_visible": True,
                    "freshness": "provider_send",
                    "epoch_id": record.epoch_id,
                    "capability_id": "agent_runner",
                }
            ],
        }
        self.assertTrue(validate_prompt_surface_payload(payload))
        # same phase/prompt but different send_ref must be valid and distinct
        record2 = build_prompt_surface_record(
            phase="writer",
            send_ref="effect_2",
            prompt_digest="sha256:" + "d" * 64,
            prompt_chars=100,
            epoch_id="ctx_epoch:" + "e" * 16,
            source_refs=("provider_send:coding",),
            provider_effect_id="effect_2",
            sections=(section,),
        )
        self.assertNotEqual(record.surface_id, record2.surface_id)
        self.assertEqual(record.prompt_digest, record2.prompt_digest)

    def test_build_prompt_surface_record_normalizes_before_id_generation(self) -> None:
        record = build_prompt_surface_record(
            phase="  writer  ",
            send_ref="  effect_padded  ",
            prompt_digest="  sha256:" + "9" * 64 + "  ",
            prompt_chars=10,
            epoch_id="ctx_epoch:" + "c" * 16,
        )
        self.assertEqual(record.phase, "writer")
        self.assertEqual(record.send_ref, "effect_padded")
        self.assertEqual(record.prompt_digest, "sha256:" + "9" * 64)
        expected_surface = prompt_surface_id(phase="writer", send_ref="effect_padded", prompt_digest="sha256:" + "9" * 64)
        self.assertEqual(record.surface_id, expected_surface)

    def test_validate_rejects_forbidden_keys_and_strict_shapes(self) -> None:
        authentic_surface = prompt_surface_id(phase="writer", send_ref="effect_1", prompt_digest="sha256:" + "b" * 64)
        good = {
            "schema_version": PROMPT_SURFACE_SCHEMA_VERSION,
            "surface_id": authentic_surface,
            "send_ref": "effect_1",
            "phase": "writer",
            "prompt_digest": "sha256:" + "b" * 64,
            "prompt_chars": 10,
            "epoch_id": "ctx_epoch:" + "c" * 16,
        }
        self.assertTrue(validate_prompt_surface_payload(good))
        # mismatched / forged surface_id with correct shape must be rejected
        forged_surface = dict(good)
        forged_surface["surface_id"] = "prompt_surface:" + "0" * 16
        self.assertFalse(validate_prompt_surface_payload(forged_surface))
        # non-canonical phase (> 40 chars, or containing whitespace/special characters)
        bad_long_phase = dict(good)
        bad_long_phase["phase"] = "p" * 41
        self.assertFalse(validate_prompt_surface_payload(bad_long_phase))
        bad_space_phase = dict(good)
        bad_space_phase["phase"] = "writer with space"
        self.assertFalse(validate_prompt_surface_payload(bad_space_phase))
        bad_padded_phase = dict(good)
        bad_padded_phase["phase"] = " writer "
        self.assertFalse(validate_prompt_surface_payload(bad_padded_phase))
        # non-canonical send_ref (> 80 chars, unstripped, containing whitespace or newlines)
        bad_long_send = dict(good)
        bad_long_send["send_ref"] = "s" * 81
        self.assertFalse(validate_prompt_surface_payload(bad_long_send))
        bad_padded_send = dict(good)
        bad_padded_send["send_ref"] = " effect_1 "
        self.assertFalse(validate_prompt_surface_payload(bad_padded_send))
        bad_newline_send = dict(good)
        bad_newline_send["send_ref"] = "effect_1\nmore"
        bad_newline_send["surface_id"] = prompt_surface_id(
            phase="writer",
            send_ref=bad_newline_send["send_ref"],
            prompt_digest=good["prompt_digest"],
        )
        self.assertFalse(validate_prompt_surface_payload(bad_newline_send))
        bad_space_send = dict(good)
        bad_space_send["send_ref"] = "effect 1"
        bad_space_send["surface_id"] = prompt_surface_id(
            phase="writer",
            send_ref=bad_space_send["send_ref"],
            prompt_digest=good["prompt_digest"],
        )
        self.assertFalse(validate_prompt_surface_payload(bad_space_send))
        # padded / non-canonical prompt_digest must be strictly rejected
        bad_padded_digest = dict(good)
        bad_padded_digest["prompt_digest"] = " " + str(good["prompt_digest"]) + " "
        bad_padded_digest["surface_id"] = prompt_surface_id(
            phase="writer",
            send_ref=good["send_ref"],
            prompt_digest=bad_padded_digest["prompt_digest"],
        )
        self.assertFalse(validate_prompt_surface_payload(bad_padded_digest))
        # bool-int loopholes must be rejected
        bad_bool_schema = dict(good)
        bad_bool_schema["schema_version"] = True
        self.assertFalse(validate_prompt_surface_payload(bad_bool_schema))
        bad_bool_chars = dict(good)
        bad_bool_chars["prompt_chars"] = True
        self.assertFalse(validate_prompt_surface_payload(bad_bool_chars))
        bad_bool_section = dict(good)
        bad_bool_section["sections"] = [{"name": "x", "digest": "sha256:" + "a" * 64, "chars": True}]
        self.assertFalse(validate_prompt_surface_payload(bad_bool_section))
        # missing schema_version
        bad_schema = dict(good)
        del bad_schema["schema_version"]
        self.assertFalse(validate_prompt_surface_payload(bad_schema))
        # bad sha
        bad_sha = dict(good)
        bad_sha["prompt_digest"] = "sha256:foo"
        self.assertFalse(validate_prompt_surface_payload(bad_sha))
        # bad surface hex
        bad_surface = dict(good)
        bad_surface["surface_id"] = "prompt_surface:zzzz"
        self.assertFalse(validate_prompt_surface_payload(bad_surface))
        # missing send_ref
        bad_send = dict(good)
        del bad_send["send_ref"]
        self.assertFalse(validate_prompt_surface_payload(bad_send))
        # missing / empty epoch_id must be strictly rejected
        bad_missing_epoch = dict(good)
        del bad_missing_epoch["epoch_id"]
        self.assertFalse(validate_prompt_surface_payload(bad_missing_epoch))
        bad_empty_epoch = dict(good)
        bad_empty_epoch["epoch_id"] = ""
        self.assertFalse(validate_prompt_surface_payload(bad_empty_epoch))
        # bad epoch
        bad_epoch = dict(good)
        bad_epoch["epoch_id"] = "ctx_epoch:zzzz"
        self.assertFalse(validate_prompt_surface_payload(bad_epoch))
        for forbidden in ("prompt", "reply", "content", "body", "text", "stdout", "stderr", "diff", "source_body", "result", "model_text"):
            with self.subTest(key=forbidden):
                bad = dict(good)
                bad[forbidden] = "secret"
                self.assertFalse(validate_prompt_surface_payload(bad))
        # also rejects inside sections
        bad_section = dict(good)
        bad_section["sections"] = [{"name": "x", "digest": "sha256:" + "a" * 64, "chars": 1, "prompt": "leak"}]
        self.assertFalse(validate_prompt_surface_payload(bad_section))

    def test_record_provider_send_prompt_is_fail_open_except_cancellation(self) -> None:
        # broken trace must not raise even with explicit phase/send_ref
        record_provider_send_prompt(
            _BrokenTrace(),
            name="coding_outbound_prompt",
            text="hello",
            purpose="coding prompt sent to provider",
            source_ref="provider_send:coding",
            phase="writer",
            send_ref="effect_1",
        )
        # without phase/send_ref, still fail-open (only section recorded, no surface)
        record_provider_send_prompt(
            _BrokenTrace(),
            name="coding_outbound_prompt",
            text="hello",
            purpose="coding prompt sent to provider",
            source_ref="provider_send:coding",
        )
        # cancellation must propagate
        with self.assertRaises(cancellation.TaskCancelled):
            record_provider_send_prompt(
                _StoppingTrace(),
                name="coding_outbound_prompt",
                text="hello",
                purpose="coding prompt sent to provider",
                source_ref="provider_send:coding",
                phase="writer",
                send_ref="effect_1",
            )

    def test_record_provider_send_prompt_requires_explicit_phase_and_send_ref(self) -> None:
        trace = _CaptureTrace()
        # no phase/send_ref -> only section, no surface
        record_provider_send_prompt(
            trace,
            name="chat_prompt",
            text="hello chat",
            purpose="chat prompt",
            source_ref="provider_send:chat",
        )
        self.assertEqual(len(trace.sections), 1)
        self.assertEqual(len(trace.surfaces), 0)
        # phase + provider_effect_id without send_ref -> NO fallback, surfaces must be empty
        trace_fallback = _CaptureTrace()
        record_provider_send_prompt(
            trace_fallback,
            name="coding_outbound_prompt",
            text="hello fallback",
            purpose="coding prompt sent to provider",
            source_ref="provider_send:coding",
            phase="writer",
            provider_effect_id="effect_legacy",
        )
        self.assertEqual(len(trace_fallback.sections), 1)
        self.assertEqual(len(trace_fallback.surfaces), 0)
        # explicit writer phase and send_ref -> both
        trace2 = _CaptureTrace()
        record_provider_send_prompt(
            trace2,
            name="coding_outbound_prompt",
            text="hello writer",
            purpose="coding prompt sent to provider",
            source_ref="provider_send:coding",
            phase="writer",
            send_ref="effect_99",
            provider_effect_id="effect_99",
        )
        self.assertEqual(len(trace2.sections), 1)
        self.assertEqual(len(trace2.surfaces), 1)
        self.assertEqual(trace2.surfaces[0]["phase"], "writer")
        self.assertEqual(trace2.surfaces[0]["send_ref"], "effect_99")

    def test_run_trace_records_prompt_surface_per_send(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = RunTraceStore(Path(td))
            recorder = store.open(run_id="run-surface", session_id="sess-surface", project=None, mode_initial="project", provider_initial="deepseek")
            surf1 = prompt_surface_id(phase="writer", send_ref="effect_1", prompt_digest="sha256:" + "b" * 64)
            base_payload = {
                "schema_version": PROMPT_SURFACE_SCHEMA_VERSION,
                "surface_id": surf1,
                "send_ref": "effect_1",
                "phase": "writer",
                "prompt_digest": "sha256:" + "b" * 64,
                "prompt_chars": 42,
                "epoch_id": "ctx_epoch:" + "c" * 16,
                "source_refs": ["provider_send:coding"],
                "provider_effect_id": "effect_1",
                "model_tool_contract_hash": "sha256:" + "d" * 64,
                "runtime_tool_contract_hash": "",
                "sections": [
                    {"name": "coding_outbound_prompt", "digest": "sha256:" + "e" * 64, "chars": 42, "source_refs": ["provider_send:coding"]},
                ],
            }
            recorder.record_prompt_surface(base_payload)
            # duplicate same surface_id must be deduped
            recorder.record_prompt_surface(base_payload)
            # same prompt_digest but different send_ref/surface_id must record second surface
            surf2 = prompt_surface_id(phase="writer", send_ref="effect_2", prompt_digest="sha256:" + "b" * 64)
            second = dict(base_payload)
            second["surface_id"] = surf2
            second["send_ref"] = "effect_2"
            second["provider_effect_id"] = "effect_2"
            recorder.record_prompt_surface(second)
            # forbidden payload must be ignored
            surf3 = prompt_surface_id(phase="writer", send_ref="effect_3", prompt_digest="sha256:" + "b" * 64)
            bad = dict(base_payload)
            bad["surface_id"] = surf3
            bad["send_ref"] = "effect_3"
            bad["prompt"] = "raw prompt leak"
            recorder.record_prompt_surface(bad)
            recorder.finish(status="done")
            data = store.path_for("sess-surface", "run-surface").read_text(encoding="utf-8")
            import json

            obj = json.loads(data)
            self.assertEqual(len(obj["prompt_surfaces"]), 2)
            ids = {s["surface_id"] for s in obj["prompt_surfaces"]}
            self.assertIn(base_payload["surface_id"], ids)
            self.assertIn(second["surface_id"], ids)
            # raw payload keys must not appear as exact keys
            for surf in obj["prompt_surfaces"]:
                for forbidden in ("prompt", "reply", "content", "body", "text", "stdout", "stderr", "diff", "source_body", "result", "model_text"):
                    self.assertNotIn(forbidden, surf)
                    for sec in surf.get("sections", []):
                        self.assertNotIn(forbidden, sec)
            # stored payload fields must authentically derive the exact surface_id
            for surf in obj["prompt_surfaces"]:
                derived = prompt_surface_id(
                    phase=surf["phase"],
                    send_ref=surf["send_ref"],
                    prompt_digest=surf["prompt_digest"],
                )
                self.assertEqual(surf["surface_id"], derived)

    def test_record_provider_prompt_boundary_writes_section_and_surface(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = RunTraceStore(Path(td))
            recorder = store.open(run_id="run-boundary", session_id="sess-boundary", project=None, mode_initial="project", provider_initial="deepseek")
            record_provider_send_prompt(
                recorder,
                name="coding_outbound_prompt",
                text="outbound prompt content",
                purpose="test boundary recording",
                source_ref="provider_send:test",
                phase="writer",
                send_ref="eff_provider_send_1234",
            )
            recorder.finish(status="done")
            data = store.path_for("sess-boundary", "run-boundary").read_text(encoding="utf-8")
            import json

            obj = json.loads(data)
            self.assertEqual(len(obj["prompt_sections"]), 1)
            self.assertEqual(len(obj["prompt_surfaces"]), 1)
            self.assertEqual(obj["prompt_sections"][0]["name"], "coding_outbound_prompt")
            self.assertEqual(obj["prompt_surfaces"][0]["phase"], "writer")
            self.assertEqual(obj["prompt_surfaces"][0]["send_ref"], "eff_provider_send_1234")
            derived = prompt_surface_id(
                phase=obj["prompt_surfaces"][0]["phase"],
                send_ref=obj["prompt_surfaces"][0]["send_ref"],
                prompt_digest=obj["prompt_surfaces"][0]["prompt_digest"],
            )
            self.assertEqual(obj["prompt_surfaces"][0]["surface_id"], derived)


if __name__ == "__main__":
    unittest.main()
