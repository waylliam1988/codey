from __future__ import annotations

import unittest
from types import SimpleNamespace

from codey.context_epoch import (
    EPOCH_REF_PREFIX,
    MAX_SNAPSHOT_SOURCES,
    PROVIDER_TURN_ADMISSION,
    PROVIDER_TURN_BOUNDARY,
    SOURCE_REF_PREFIX,
    ContextAdmission,
    ContextEpoch,
    ContextSnapshot,
    context_epoch_id,
    context_source_ref,
    snapshot_from_rendered_sources,
)


def _admission_input(key: str, text: str, **kwargs):
    """Duck-typed stand-in for one rendered context source."""
    return SimpleNamespace(
        key=key,
        text=text,
        budget=kwargs.pop("budget", 100),
        truncated=kwargs.pop("truncated", False),
        freshness=kwargs.pop("freshness", "run_start"),
        capability_id=kwargs.pop("capability_id", ""),
        admission_reason=kwargs.pop("admission_reason", ""),
    )


class ContextEpochIdTests(unittest.TestCase):
    def test_epoch_id_is_content_addressed_and_stable(self) -> None:
        first = context_epoch_id("prompt body")
        second = context_epoch_id("prompt body")
        other = context_epoch_id("different prompt body")

        self.assertEqual(first, second)
        self.assertNotEqual(first, other)
        self.assertTrue(first.startswith(EPOCH_REF_PREFIX))
        suffix = first.removeprefix(EPOCH_REF_PREFIX)
        self.assertEqual(len(suffix), 16)
        self.assertTrue(all(char in "0123456789abcdef" for char in suffix))

    def test_empty_text_is_valid_deterministic_input(self) -> None:
        self.assertEqual(context_epoch_id(""), context_epoch_id(None))


class ContextSourceRefTests(unittest.TestCase):
    def test_source_ref_is_prefixed_and_normalized(self) -> None:
        self.assertEqual(
            context_source_ref("project_instructions"),
            f"{SOURCE_REF_PREFIX}project_instructions",
        )
        self.assertEqual(
            context_source_ref("bad key with spaces"),
            f"{SOURCE_REF_PREFIX}bad_key_with_spaces",
        )


class ContextAdmissionTests(unittest.TestCase):
    def test_payload_is_bounded_and_omits_empty_optional_fields(self) -> None:
        admission = ContextAdmission(
            source_key="research_brief",
            source_ref=context_source_ref("research_brief"),
            digest="sha256:" + "a" * 64,
            chars=120,
            budget=8000,
            truncated=True,
        )

        payload = admission.to_payload()

        self.assertEqual(payload["source_key"], "research_brief")
        self.assertEqual(payload["source_ref"], "context_source:research_brief")
        self.assertEqual(payload["chars"], 120)
        self.assertEqual(payload["budget"], 8000)
        self.assertTrue(payload["truncated"])
        self.assertNotIn("capability_id", payload)
        self.assertNotIn("admission_reason", payload)

    def test_payload_keeps_nonempty_capability_and_reason(self) -> None:
        admission = ContextAdmission(
            source_key="ghost_directive",
            source_ref=context_source_ref("ghost_directive"),
            digest="sha256:" + "b" * 64,
            chars=10,
            budget=100,
            capability_id="local_context",
            admission_reason=PROVIDER_TURN_ADMISSION,
        )

        payload = admission.to_payload()

        self.assertEqual(payload["capability_id"], "local_context")
        self.assertEqual(payload["admission_reason"], PROVIDER_TURN_ADMISSION)

    def test_payload_never_contains_raw_text(self) -> None:
        secret = "SECRET_SOURCE_BODY_SHOULD_NOT_BE_SAVED"
        admission = ContextAdmission(
            source_key="leaky",
            source_ref=context_source_ref("leaky"),
            digest="sha256:" + "c" * 64,
            chars=len(secret),
            budget=100,
        )
        serialized = repr(admission.to_payload())

        self.assertNotIn(secret, serialized)


class SnapshotProjectionTests(unittest.TestCase):
    def test_snapshot_from_rendered_sources_projects_admissions(self) -> None:
        sources = (
            _admission_input(
                "project_map",
                "alpha body",
                capability_id="agent_runner",
                admission_reason="run_start_assembly",
            ),
            _admission_input("empty", ""),
        )

        snapshot = snapshot_from_rendered_sources(
            sources,
            epoch_id=context_epoch_id("outbound prompt"),
            admission_reason=PROVIDER_TURN_ADMISSION,
        )

        self.assertIsInstance(snapshot, ContextSnapshot)
        self.assertEqual(len(snapshot.admissions), 1)
        admission = snapshot.admissions[0]
        self.assertEqual(admission.source_key, "project_map")
        self.assertEqual(admission.source_ref, "context_source:project_map")
        self.assertEqual(admission.capability_id, "agent_runner")
        self.assertEqual(admission.admission_reason, "run_start_assembly")
        self.assertEqual(admission.chars, len("alpha body"))
        self.assertFalse(admission.truncated)
        self.assertTrue(admission.digest.startswith("sha256:"))
        self.assertNotIn("alpha body", repr(snapshot.to_payload()))

    def test_snapshot_skips_empty_sources_and_caps_size(self) -> None:
        sources = [
            _admission_input(f"source_{index}", "" if index == 3 else f"body {index}")
            for index in range(MAX_SNAPSHOT_SOURCES + 8)
        ]

        snapshot = snapshot_from_rendered_sources(
            sources,
            epoch_id="ctx_epoch:" + "0" * 16,
        )

        self.assertLessEqual(len(snapshot.admissions), MAX_SNAPSHOT_SOURCES)
        self.assertTrue(all(item.source_key != "source_3" for item in snapshot.admissions))

    def test_snapshot_falls_back_to_default_admission_reason(self) -> None:
        snapshot = snapshot_from_rendered_sources(
            (_admission_input("work_checkpoint", "checkpoint body", admission_reason=""),),
            epoch_id="ctx_epoch:" + "1" * 16,
            admission_reason=PROVIDER_TURN_ADMISSION,
        )

        self.assertEqual(
            snapshot.admissions[0].admission_reason,
            PROVIDER_TURN_ADMISSION,
        )

    def test_epoch_carries_boundary_and_admissions(self) -> None:
        admission = ContextAdmission(
            source_key="verified_facts",
            source_ref=context_source_ref("verified_facts"),
        )
        epoch = ContextEpoch(
            epoch_id=context_epoch_id("text"),
            boundary=PROVIDER_TURN_BOUNDARY,
            admissions=(admission,),
        )

        payload = epoch.to_payload()

        self.assertEqual(payload["boundary"], PROVIDER_TURN_BOUNDARY)
        self.assertEqual(payload["epoch_id"], context_epoch_id("text"))
        self.assertEqual(payload["admissions"], [admission.to_payload()])


if __name__ == "__main__":
    unittest.main()
