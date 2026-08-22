from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.manual.ab_journal import (
    TRANSCRIPT_MODE_ARCHIVE,
    TRANSCRIPT_MODE_DIGEST_ONLY,
    TranscriptReplayCache,
)


def test_digest_only_mode_never_writes_content(tmp_path: Path) -> None:
    cache = TranscriptReplayCache(tmp_path, mode=TRANSCRIPT_MODE_DIGEST_ONLY)
    ref = cache.ref_for(prompt="secret prompt", reply="secret reply", archive=True)

    assert ref.mode == TRANSCRIPT_MODE_DIGEST_ONLY
    assert ref.path == ""
    assert ref.content_digest.startswith("sha256:")
    assert list(tmp_path.iterdir()) == []


def test_archive_mode_stores_content_addressed_transcript(tmp_path: Path) -> None:
    cache = TranscriptReplayCache(tmp_path, mode=TRANSCRIPT_MODE_ARCHIVE)
    ref = cache.ref_for(prompt="question one", reply="answer one", archive=True)

    assert ref.mode == TRANSCRIPT_MODE_ARCHIVE
    assert ref.path == f"transcripts/{ref.content_digest.removeprefix('sha256:')}.json"
    stored = tmp_path / ref.path
    assert stored.is_file()
    payload = json.loads(stored.read_text(encoding="utf-8"))
    assert payload["prompt"] == "question one"
    assert payload["reply"] == "answer one"
    assert payload["content_digest"] == ref.content_digest
    # Only the bounded schema fields exist: no DOM/cookie/webpage surfaces.
    assert set(payload) == {
        "schema_version",
        "kind",
        "content_digest",
        "prompt",
        "reply",
        "created_at",
    }


def test_archive_mode_is_idempotent_per_content(tmp_path: Path) -> None:
    cache = TranscriptReplayCache(tmp_path, mode=TRANSCRIPT_MODE_ARCHIVE)
    first = cache.ref_for(prompt="p", reply="r", archive=True)
    second = cache.ref_for(prompt="p", reply="r", archive=True)
    third = cache.ref_for(prompt="other", reply="r", archive=True)

    assert first.path == second.path
    assert first.path != third.path
    transcripts = list((tmp_path / "transcripts").glob("*.json"))
    assert len(transcripts) == 2


def test_oversized_transcript_is_rejected(tmp_path: Path) -> None:
    cache = TranscriptReplayCache(
        tmp_path,
        mode=TRANSCRIPT_MODE_ARCHIVE,
        max_bytes=64,
    )
    with pytest.raises(ValueError, match="bounded size"):
        cache.ref_for(prompt="x" * 200, reply="y" * 200, archive=True)


def test_unknown_mode_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown transcript mode"):
        TranscriptReplayCache(tmp_path, mode="raw_everything")
