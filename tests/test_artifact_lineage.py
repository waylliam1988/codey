from __future__ import annotations

import re

from codey.research.artifact_lineage import (
    ARTIFACT_MIME_TEXT,
    artifact_ref_from_managed_output,
    is_valid_derived_ref,
)

_VERSION_RE = re.compile(r"^artifact_version:[0-9a-f]{16}$")
_ARTIFACT_RE = re.compile(r"^artifact:[0-9a-f]{16}$")


def _base_input(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "handle": "out_0001_abc123def456",
        "original_bytes": 90000,
        "stored_bytes": 40000,
        "sha256": "a" * 64,
        "stored_truncated": True,
        "origin_run_id": "run-1",
        "produced_by": "analysis_run:0123456789abcdef",
    }
    data.update(overrides)
    return data


def test_artifact_ref_from_managed_output_projects_lineage() -> None:
    artifact = artifact_ref_from_managed_output(_base_input())

    assert artifact is not None
    assert _ARTIFACT_RE.fullmatch(artifact.artifact_id)
    assert _VERSION_RE.fullmatch(artifact.version_id)
    assert artifact.artifact_kind == "managed_output"
    assert artifact.sha256 == "a" * 64
    assert artifact.size == 40000
    assert artifact.mime == ARTIFACT_MIME_TEXT
    assert artifact.origin_run_id == "run-1"
    assert artifact.produced_by == "analysis_run:0123456789abcdef"
    assert artifact.stored_truncated is True
    assert artifact.warnings == ("stored_output_truncated",)


def test_artifact_ref_is_content_stable() -> None:
    first = artifact_ref_from_managed_output(_base_input())
    second = artifact_ref_from_managed_output(_base_input(handle="out_0002_ffffff123456"))

    assert first is not None and second is not None
    # Same content digest -> same artifact identity across handles.
    assert first.artifact_id == second.artifact_id
    # Different stored handle -> distinct version identity.
    assert first.version_id != second.version_id


def test_artifact_ref_fails_open_on_missing_digest_or_handle() -> None:
    assert artifact_ref_from_managed_output(_base_input(sha256="zzz")) is None
    assert artifact_ref_from_managed_output(_base_input(handle="")) is None
    assert artifact_ref_from_managed_output("nope") is None  # type: ignore[arg-type]


def test_artifact_ref_filters_invalid_derived_refs() -> None:
    artifact = artifact_ref_from_managed_output(_base_input(
        derived_from=[
            "analysis_run:0123456789abcdef",
            "source:abcdef0123456789",
            "run:pipeline-run-1",
            "garbage-without-prefix",
            "claim:not-allowed-prefix",
            "source:https://example.com/page",
        ],
    ))

    assert artifact is not None
    assert artifact.derived_from == (
        "analysis_run:0123456789abcdef",
        "source:abcdef0123456789",
        "run:pipeline-run-1",
    )


def test_artifact_ref_only_accepts_sequence_derived_refs() -> None:
    string_ref = artifact_ref_from_managed_output(_base_input(
        derived_from="analysis_run:0123456789abcdef",
    ))
    mapping_ref = artifact_ref_from_managed_output(_base_input(
        derived_from={"ref": "analysis_run:0123456789abcdef"},
    ))
    tuple_ref = artifact_ref_from_managed_output(_base_input(
        derived_from=("analysis_run:0123456789abcdef",),
    ))

    assert string_ref is not None
    assert mapping_ref is not None
    assert tuple_ref is not None
    assert string_ref.derived_from == ()
    assert mapping_ref.derived_from == ()
    assert tuple_ref.derived_from == ("analysis_run:0123456789abcdef",)


def test_is_valid_derived_ref_requires_exact_shapes() -> None:
    # Generated research refs must be 16-hex.
    assert is_valid_derived_ref("analysis_run:0123456789abcdef")
    assert is_valid_derived_ref("evidence:fedcba9876543210")
    assert not is_valid_derived_ref("source:https://example.com/page")
    assert not is_valid_derived_ref("analysis_run:xyz")
    # Runtime run ids are bounded identifier tokens.
    assert is_valid_derived_ref("run:pipeline-run-1")
    assert not is_valid_derived_ref("run:")
    assert not is_valid_derived_ref("run:bad id with spaces")
    # Unknown prefixes and free text fail closed.
    assert not is_valid_derived_ref("claim:x")
    assert not is_valid_derived_ref("")
