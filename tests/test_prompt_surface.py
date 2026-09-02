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

    def test_validate_rejects_forbidden_keys_and_strict_shapes(self) -> None:
        good = {
            "schema_version": PROMPT_SURFACE_SCHEMA_VERSION,
            "surface_id": "prompt_surface:" + "a" * 16,
            "send_ref": "effect_1",
            "phase": "writer",
            "prompt_digest": "sha256:" + "b" * 64,
            "prompt_chars": 10,
            "epoch_id": "ctx_epoch:" + "c" * 16,
        }
        self.assertTrue(validate_prompt_surface_payload(good))
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
        for forbidden in ("prompt", "reply", "content", "body", "text", "stdout", "stderr", "diff", "source_body", "result", "model_text"):
            with self.subTest(key=forbidden):
                bad = dict(good)
                bad[forbidden] = "secret"
                self.assertFalse(validate_prompt_surface_payload(bad))
        # also rejects inside sections
        bad_section = dict(good)
        bad_section["sections"] = [{"name": "x", "digest": "sha256:" + "a" * 64, "chars": 1, "prompt": "leak"}]
        self.assertFalse(validate_prompt_surface_payload(bad_section))
        # bad epoch
        bad_epoch = dict(good)
        bad_epoch["epoch_id"] = "ctx_epoch:zzzz"
        self.assertFalse(validate_prompt_surface_payload(bad_epoch))

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
            base_payload = {
                "schema_version": PROMPT_SURFACE_SCHEMA_VERSION,
                "surface_id": "prompt_surface:" + "a" * 16,
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
            second = dict(base_payload)
            second["surface_id"] = "prompt_surface:" + "f" * 16
            second["send_ref"] = "effect_2"
            second["provider_effect_id"] = "effect_2"
            recorder.record_prompt_surface(second)
            # forbidden payload must be ignored
            bad = dict(base_payload)
            bad["surface_id"] = "prompt_surface:" + "1" * 16
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


if __name__ == "__main__":
    unittest.main()
