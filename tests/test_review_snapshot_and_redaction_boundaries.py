"""Review snapshots and input bounds preserve applicability and useful code."""

from codey.reviews.identity import capture_snapshot, verify_snapshot
from codey.reviews.input import prepare_review_input


def prepare(diff, path="app.py", **kwargs):
    return prepare_review_input(project="project", task="review", writer_summary="done", changes={
        "ok": True, "files": [{"path": path, "status": "M"}], "changed_count": 1, "diff": diff,
    }, **kwargs)


def test_snapshot_file_limit_does_not_silently_drop_required_files(tmp_path):
    paths = tuple(f"f{i}.py" for i in range(33))
    for name in paths:
        (tmp_path / name).write_text("x", encoding="utf-8")
    assert not capture_snapshot(tmp_path, paths).ok


def test_new_project_file_invalidates_review_snapshot(tmp_path):
    (tmp_path / "app.py").write_text("x", encoding="utf-8")
    snapshot = capture_snapshot(tmp_path, ("app.py",))
    assert verify_snapshot(snapshot)
    (tmp_path / "new.py").write_text("y", encoding="utf-8")
    assert not verify_snapshot(snapshot)


def test_private_and_auth_source_modules_remain_reviewable():
    for path in ("src/auth.py", "src/private_helpers.py", "src/credentials_parser.py"):
        prepared = prepare(f"diff --git a/{path} b/{path}\n+safe = True\n", path)
        assert prepared.scope.provided_files == (path,)
        assert prepared.scope.is_complete


def test_contextual_hex_secret_is_redacted_without_removing_diff_marker():
    secret = "0123456789abcdef0123456789abcdef"
    prepared = prepare(f'diff --git a/app.py b/app.py\n+api_key = "{secret}"\n')
    assert secret not in prepared.prompt
    assert "+api_key" in prepared.reviewer_view["diff"]


def test_long_normal_diff_line_is_preserved_when_secret_only_is_removed():
    code = "normal_code = " + "a" * 250
    secret = "sk-live-abcdefghij1234567890XYZ"
    prepared = prepare(f'diff --git a/app.py b/app.py\n+{code}; api_key="{secret}"\n')
    assert "+" + code in prepared.reviewer_view["diff"]
    assert secret not in prepared.prompt


def test_required_task_clipping_marks_review_incomplete():
    prepared = prepare_review_input(project="p", task="x" * 6001, writer_summary="done", changes={
        "ok": True, "files": [{"path": "app.py"}], "diff": "diff --git a/app.py b/app.py\n+x\n",
    })
    assert not prepared.scope.is_complete
