"""Snapshot trust requires a complete inventory and stable Git review basis."""
import subprocess
from pathlib import Path

from codey.reviews.identity import ReviewSnapshot, capture_snapshot, verify_snapshot


def test_inventory_read_failure_is_not_a_valid_snapshot(tmp_path, monkeypatch):
    (tmp_path / "app.py").write_text("x", encoding="utf-8")
    monkeypatch.setattr(Path, "iterdir", lambda _: (_ for _ in ()).throw(PermissionError("unreadable")))
    assert not capture_snapshot(tmp_path, ("app.py",)).ok


def test_snapshot_without_inventory_cannot_claim_current(tmp_path):
    assert not verify_snapshot(ReviewSnapshot(str(tmp_path), ()))


def test_git_commit_invalidates_review_even_when_worktree_content_is_identical(tmp_path):
    def git(*args):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.name", "test")
    git("config", "user.email", "test@example.invalid")
    (tmp_path / "app.py").write_text("x = 1", encoding="utf-8")
    git("add", ".")
    git("commit", "-qm", "first")
    (tmp_path / "app.py").write_text("x = 2", encoding="utf-8")
    before = capture_snapshot(tmp_path, ("app.py",))
    assert before.ok and verify_snapshot(before)
    git("add", ".")
    git("commit", "-qm", "second")
    assert not verify_snapshot(before)
