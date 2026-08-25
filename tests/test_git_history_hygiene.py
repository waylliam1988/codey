from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SEMVER_RE = re.compile(r"\b\d+\.\d+\.\d+\b")
RELEASE_SUBJECT_RE = re.compile(r"^release(?:\s|-)\d+\.\d+\.\d+\b", re.IGNORECASE)
EXPECTED_COMMIT_EMAIL = "waylliam@qq.com"


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


def _unreleased_commit_rows(format_spec: str) -> list[list[str]]:
    if _git(["rev-parse", "--is-inside-work-tree"]).returncode != 0:
        pytest.skip("git worktree is unavailable")

    result = _git(["log", f"--format={format_spec}", "HEAD"])
    if result.returncode != 0:
        pytest.skip(f"git log is unavailable: {result.stderr.strip()}")

    unreleased_rows: list[list[str]] = []
    found_release_boundary = False
    for line in result.stdout.splitlines():
        row = line.split("\0")
        subject = row[-1]
        if RELEASE_SUBJECT_RE.search(subject):
            found_release_boundary = True
            break
        unreleased_rows.append(row)

    if not found_release_boundary:
        pytest.skip("no release commit boundary found in history")

    return unreleased_rows


def test_unreleased_commit_subjects_do_not_include_release_versions() -> None:
    unreleased_rows = _unreleased_commit_rows("%H%x00%s")
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


def test_unreleased_commits_keep_waylliam_identity() -> None:
    unreleased_rows = _unreleased_commit_rows("%H%x00%ae%x00%ce%x00%s")
    violations = [
        f"{sha[:7]} author={author_email} committer={committer_email} {subject}"
        for sha, author_email, committer_email, subject in unreleased_rows
        if author_email != EXPECTED_COMMIT_EMAIL
        or committer_email != EXPECTED_COMMIT_EMAIL
    ]

    assert not violations, (
        "Non-release commits after the latest release must keep the canonical "
        f"Codey git identity email {EXPECTED_COMMIT_EMAIL} for both author "
        "and committer:\n"
        + "\n".join(violations)
    )
