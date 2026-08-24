from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SEMVER_RE = re.compile(r"\b\d+\.\d+\.\d+\b")
RELEASE_SUBJECT_RE = re.compile(r"^release(?:\s|-)\d+\.\d+\.\d+\b", re.IGNORECASE)


def _git(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(ROOT), *args],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    )


def test_unreleased_commit_subjects_do_not_include_release_versions() -> None:
    if _git(["rev-parse", "--is-inside-work-tree"]).returncode != 0:
        pytest.skip("git worktree is unavailable")

    result = _git(["log", "--format=%H%x00%s", "HEAD"])
    if result.returncode != 0:
        pytest.skip(f"git log is unavailable: {result.stderr.strip()}")

    unreleased_rows: list[tuple[str, str]] = []
    found_release_boundary = False
    for line in result.stdout.splitlines():
        sha, _, subject = line.partition("\0")
        if RELEASE_SUBJECT_RE.search(subject):
            found_release_boundary = True
            break
        unreleased_rows.append((sha, subject))

    if not found_release_boundary:
        pytest.skip("no release commit boundary found in history")

    violations = [
        f"{sha[:7]} {subject}"
        for sha, subject in unreleased_rows
        if SEMVER_RE.search(subject)
    ]

    assert not violations, (
        "Non-release commit subjects after the latest release must not contain "
        "Codey release versions; keep versions only on release marker commits:\n"
        + "\n".join(violations)
    )
