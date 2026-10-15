"""Workspace and research documents share one session-scoped reader."""

from types import SimpleNamespace

import pytest


@pytest.fixture
def documents(tmp_path):
    root = tmp_path / "Project with spaces"
    root.mkdir()
    (root / "report.md").write_text("# 参数报告\n", encoding="utf-8", newline="")
    state = {"sessions": [{"id": "chat", "projectId": "project"}], "projects": [{"id": "project", "path": str(root)}]}
    return root, SimpleNamespace(load_ui_state=lambda: state, state_home=tmp_path / "state", knowledge_store=None)


def preview(ctx, **fields):
    from codey.app.documents import document_response

    return document_response(ctx, {"session_id": "chat", "kind": "workspace", **fields})


def test_document_content_version_changes_and_missing_is_explicit(documents):
    root, ctx = documents
    status, first = preview(ctx, path="report.md")
    assert status == 200
    assert first["content"] == "# 参数报告\n"
    assert first["format"] == "markdown"
    (root / "report.md").write_text("# Updated", encoding="utf-8")
    assert preview(ctx, path=str(root / "report.md"))[1]["version"] != first["version"]
    (root / "report.md").unlink()
    assert preview(ctx, path="report.md")[0] == 404


@pytest.mark.parametrize(
    "path", ["../outside.md", "C:/Windows/win.ini", "//server/share/file", "report.md:stream", "a\0b"]
)
def test_document_paths_never_escape_session_workspace(documents, path):
    _, ctx = documents
    assert preview(ctx, path=path)[0] == 400


def test_unknown_session_does_not_accept_client_supplied_root(documents):
    root, ctx = documents
    assert preview(ctx, session_id="other", project=str(root), path="report.md")[0] == 404


def test_open_document_cannot_silently_switch_to_a_different_project(documents, tmp_path):
    _, ctx = documents
    first = preview(ctx, path="report.md")[1]
    other = tmp_path / "other"
    other.mkdir()
    (other / "report.md").write_text("different project", encoding="utf-8")
    ctx.load_ui_state()["projects"][0]["path"] = str(other)
    assert preview(ctx, path="report.md", expected_workspace=first["workspace"])[0] == 409


def test_symlink_target_outside_workspace_is_denied(documents, tmp_path):
    root, ctx = documents
    outside = tmp_path / "outside.md"
    outside.write_text("private", encoding="utf-8")
    try:
        (root / "link.md").symlink_to(outside)
    except OSError:
        pytest.skip("Creating symlinks requires Windows developer mode or privileges")
    assert preview(ctx, path="link.md")[0] == 400


def test_binary_and_oversize_are_explicit_not_silently_truncated(documents):
    root, ctx = documents
    (root / "binary.dat").write_bytes(b"\x00\xff")
    assert preview(ctx, path="binary.dat")[1]["error"] == "binary_document"
    (root / "huge.md").write_bytes(b"x" * (2 * 1024 * 1024 + 1))
    assert preview(ctx, path="huge.md")[1]["error"] == "document_too_large"


def test_research_uses_same_document_contract_and_checks_owner(documents, tmp_path):
    from codey.knowledge.note import KnowledgeNote
    from codey.knowledge.store import KnowledgeStore

    _, ctx = documents
    ctx.knowledge_store = KnowledgeStore(tmp_path / "vault")
    note = KnowledgeNote(id="report", type="synthesis", title="Research report", body="# Research\n", session_id="chat")
    ctx.knowledge_store.write_note(note)
    status, result = preview(ctx, kind="research", note_id="report")
    assert status == 200
    assert result["content"] == ctx.knowledge_store.read_note("report").body
    assert result["format"] == "markdown"
    note.session_id = "other"
    ctx.knowledge_store.write_note(note)
    assert preview(ctx, kind="research", note_id="report")[0] == 403


def test_shortened_document_reference_resolves_only_a_unique_workspace_suffix(documents):
    root, ctx = documents
    target = root / "selfie" / "beauty" / "cache.py"
    target.parent.mkdir(parents=True)
    target.write_text("# cache", encoding="utf-8")
    status, data = preview(ctx, path="beauty/cache.py")
    assert status == 200
    assert data["path"] == "selfie/beauty/cache.py"
    assert data["content"] == "# cache"
    other = root / "other" / "beauty" / "cache.py"
    other.parent.mkdir(parents=True)
    other.write_text("# different", encoding="utf-8")
    status, data = preview(ctx, path="beauty/cache.py")
    assert status == 409
    assert data["error"] == "document_ambiguous"
    assert sorted(data["candidates"]) == ["other/beauty/cache.py", "selfie/beauty/cache.py"]
    assert preview(ctx, path="selfie/beauty/cache.py")[1]["content"] == "# cache"
