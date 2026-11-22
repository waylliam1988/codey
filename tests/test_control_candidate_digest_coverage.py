"""Candidate identities must refuse missing, linked or unreadable coverage."""

import hashlib
import os
from collections.abc import Iterator
from contextlib import AbstractContextManager
from pathlib import Path

import pytest

from codey.app.control_queries import project_digest
from codey.control.protocol import ControlError


@pytest.mark.parametrize("regular_file", [False, True])
def test_candidate_root_must_be_an_existing_directory(tmp_path: Path, regular_file: bool) -> None:
    root = tmp_path / "candidate"
    if regular_file:
        root.write_text("not a workspace", encoding="utf-8")
    with pytest.raises(ControlError) as failure:
        project_digest(str(root))
    assert failure.value.code == "EVIDENCE_UNAVAILABLE"


def test_a_linked_directory_is_not_silently_omitted_from_the_candidate(tmp_path: Path) -> None:
    root = tmp_path / "candidate"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "evidence.txt").write_text("Evidence", encoding="utf-8")
    (root / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ControlError) as failure:
        project_digest(str(root))
    assert failure.value.code == "EVIDENCE_UNAVAILABLE"


def test_directory_read_failure_cannot_produce_a_complete_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocked = tmp_path / "unreadable"
    blocked.mkdir()
    (blocked / "evidence.txt").write_text("Evidence", encoding="utf-8")
    original = os.scandir

    def scandir(path: str) -> AbstractContextManager[Iterator[os.DirEntry[str]]]:
        if Path(path) == blocked:
            raise PermissionError("candidate directory cannot be read")
        return original(path)

    monkeypatch.setattr(os, "scandir", scandir)
    with pytest.raises(ControlError) as failure:
        project_digest(str(tmp_path))
    assert failure.value.code == "EVIDENCE_UNAVAILABLE"


def test_an_existing_empty_directory_remains_a_valid_candidate(tmp_path: Path) -> None:
    assert project_digest(str(tmp_path)) == hashlib.sha256().hexdigest()


def test_covered_content_changes_the_digest_but_an_ignored_tree_does_not(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("First", encoding="utf-8")
    first = project_digest(str(tmp_path))
    ignored = tmp_path / "node_modules"
    ignored.mkdir()
    (ignored / "dependency.txt").write_text("Dependency", encoding="utf-8")
    assert project_digest(str(tmp_path)) == first
    source.write_text("Changed", encoding="utf-8")
    assert project_digest(str(tmp_path)) != first


def test_a_linked_file_is_still_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("Evidence", encoding="utf-8")
    root = tmp_path / "candidate"
    root.mkdir()
    (root / "linked.txt").symlink_to(outside)
    with pytest.raises(ControlError) as failure:
        project_digest(str(root))
    assert failure.value.code == "EVIDENCE_UNAVAILABLE"
