from __future__ import annotations

import json
import os
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


def test_archive_mode_can_delete_one_transcript_by_ref_or_digest(tmp_path: Path) -> None:
    cache = TranscriptReplayCache(tmp_path, mode=TRANSCRIPT_MODE_ARCHIVE)
    first = cache.ref_for(prompt="p1", reply="r1", archive=True)
    second = cache.ref_for(prompt="p2", reply="r2", archive=True)

    assert cache.delete_transcript(first)
    assert not (tmp_path / first.path).exists()
    assert (tmp_path / second.path).is_file()
    assert not cache.delete_transcript(first.content_digest)


def test_archive_mode_rejects_invalid_delete_digest(tmp_path: Path) -> None:
    cache = TranscriptReplayCache(tmp_path, mode=TRANSCRIPT_MODE_ARCHIVE)

    with pytest.raises(ValueError, match="invalid transcript digest"):
        cache.delete_transcript("transcripts/not-a-digest.json")


def test_archive_mode_prunes_oldest_transcripts_by_count(tmp_path: Path) -> None:
    cache = TranscriptReplayCache(tmp_path, mode=TRANSCRIPT_MODE_ARCHIVE)
    refs = [
        cache.ref_for(prompt=f"p{index}", reply=f"r{index}", archive=True)
        for index in range(3)
    ]
    for index, ref in enumerate(refs):
        os.utime(tmp_path / ref.path, (100 + index, 100 + index))

    assert cache.prune_transcripts(max_files=1) == 2

    assert not (tmp_path / refs[0].path).exists()
    assert not (tmp_path / refs[1].path).exists()
    assert (tmp_path / refs[2].path).is_file()


def test_archive_mode_prunes_to_total_byte_budget(tmp_path: Path) -> None:
    cache = TranscriptReplayCache(tmp_path, mode=TRANSCRIPT_MODE_ARCHIVE)
    first = cache.ref_for(prompt="p1", reply="r1", archive=True)
    second = cache.ref_for(prompt="p2", reply="r2", archive=True)
    os.utime(tmp_path / first.path, (100, 100))
    os.utime(tmp_path / second.path, (200, 200))
    newest_size = (tmp_path / second.path).stat().st_size

    assert cache.prune_transcripts(max_total_bytes=newest_size) == 1

    assert not (tmp_path / first.path).exists()
    assert (tmp_path / second.path).is_file()


def test_archive_prune_requires_a_retention_budget(tmp_path: Path) -> None:
    cache = TranscriptReplayCache(tmp_path, mode=TRANSCRIPT_MODE_ARCHIVE)

    with pytest.raises(ValueError, match="max_files or max_total_bytes"):
        cache.prune_transcripts()


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
