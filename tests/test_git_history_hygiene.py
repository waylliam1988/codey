"""Git history and tracked-source hygiene guards.

Commit-identity rules keep release markers canonical; the credential
scanner's own literals are assembled at runtime from fragments so this file
never contains a complete secret-like fixture -- a complete one here would
trip secret-push protection on the test itself.
"""

from __future__ import annotations

import re
import subprocess
import unittest
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


# --- tracked-source credential hygiene --------------------------------------

SCANNED_SUFFIXES = frozenset({
    ".cfg",
    ".ini",
    ".js",
    ".json",
    ".md",
    ".py",
    ".toml",
    ".ts",
    ".txt",
    ".yml",
    ".yaml",
})
FORBIDDEN_TRACKED_SUFFIXES = frozenset({".pem", ".p12", ".pfx", ".key"})
MIN_SECRET_TAIL_CHARS = 20


def _text(*parts: str) -> str:
    return "".join(parts)


# Prefix fragments only; the random-looking tail is matched by regex so no
# full fixture ever appears in this file.
_TOKEN_PREFIXES = (
    _text("gh", "p_"),
    _text("gh", "o_"),
    _text("github", "_pat_"),
    _text("sk", "-"),
    _text("AKI", "A"),
    _text("xox", "b-"),
)
_PRIVATE_KEY_HEADER = _text("-----BEGIN ", "PRIVATE KEY-----")


def _tracked_source_files() -> list[Path]:
    """Git-tracked files under production source (tests carry deliberate
    fake fixtures and are out of scope)."""

    result = _git(["ls-files", "--", "codey"])
    if result.returncode != 0:
        pytest.skip("git ls-files is unavailable")
    files: list[Path] = []
    for line in result.stdout.splitlines():
        name = line.strip().replace("\\", "/")
        if not name or ".." in name.split("/"):
            continue
        candidate = ROOT / name
        if candidate.is_file():
            files.append(candidate)
    return files


class TrackedSourceCredentialHygieneTests(unittest.TestCase):
    def test_no_credential_shaped_tokens_in_tracked_sources(self) -> None:
        tail_pattern = re.compile(
            r"[A-Za-z0-9_\-%]{" + str(MIN_SECRET_TAIL_CHARS) + r",}"
        )
        offenders: list[str] = []
        for path in _tracked_source_files():
            if path.suffix.lower() not in SCANNED_SUFFIXES:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for prefix in _TOKEN_PREFIXES:
                start = 0
                while True:
                    index = text.find(prefix, start)
                    if index < 0:
                        break
                    window = text[index + len(prefix): index + len(prefix) + 200]
                    match = tail_pattern.match(window)
                    if match:
                        offenders.append(
                            f"{path.relative_to(ROOT).as_posix()}: {prefix}..."
                        )
                        break
                    start = index + len(prefix)
            if _PRIVATE_KEY_HEADER in text:
                offenders.append(
                    f"{path.relative_to(ROOT).as_posix()}: private key header"
                )

        self.assertEqual(offenders, [])

    def test_no_private_key_material_is_tracked(self) -> None:
        offenders = [
            path.relative_to(ROOT).as_posix()
            for path in _tracked_source_files()
            if path.suffix.lower() in FORBIDDEN_TRACKED_SUFFIXES
        ]

        self.assertEqual(offenders, [])

    def test_scanner_literals_are_not_themselves_secrets(self) -> None:
        # Guard the guard: every prefix fragment must be too short to be a
        # credential on its own.
        self.assertTrue(all(len(prefix) < 12 for prefix in _TOKEN_PREFIXES))
        self.assertLess(len(_PRIVATE_KEY_HEADER), 30)


if __name__ == "__main__":
    unittest.main()
