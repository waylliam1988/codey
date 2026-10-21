"""An attached scratch project must not inherit the parent repository's diff."""

import subprocess

from codey.workspace.changes import collect_git_changes


def test_ignored_project_is_a_snapshot_workspace(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    (tmp_path / ".gitignore").write_text("scratch/\n")
    (tmp_path / "unrelated.py").write_text("unrelated work\n")
    project = tmp_path / "scratch"
    project.mkdir()
    (project / "slugify.py").write_text("pass\n")
    result = collect_git_changes(project)
    assert result.get("reason") == "not_repo"
    assert result["files"] == []
