"""Workspace and research documents share one session-scoped reader."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from codey.types.json_value import JsonObject

if TYPE_CHECKING:
    from codey.knowledge.store import KnowledgeStore

import pytest


@dataclass
class DocumentContext:
    state_home: Path
    sessions: list[dict[str, str]]
    projects: list[dict[str, str]]
    knowledge_store: KnowledgeStore | None = None

    def load_ui_state(self) -> dict[str, object]:
        return {"sessions": self.sessions, "projects": self.projects}


DocumentFixture = tuple[Path, DocumentContext]


@pytest.fixture()
def documents(tmp_path: Path) -> DocumentFixture:
    root = tmp_path / "Project with spaces"
    root.mkdir()
    (root / "report.md").write_text("# 参数报告\n", encoding="utf-8", newline="")
    return root, DocumentContext(
        tmp_path / "state", [{"id": "chat", "projectId": "project"}], [{"id": "project", "path": str(root)}],
    )


def preview(ctx: DocumentContext, **fields: object) -> tuple[int, JsonObject]:
    from codey.app.documents import document_response

    return document_response(ctx, {"session_id": "chat", "kind": "workspace", **fields})


def test_document_content_version_changes_and_missing_is_explicit(documents: DocumentFixture) -> None:
    root, ctx = documents
    status, first = preview(ctx, path="report.md")
    assert status == 200
    assert first["content"] == "# 参数报告\n"
    assert first["format"] == "markdown"
    (root / "report.md").write_text("# Updated", encoding="utf-8")
    assert preview(ctx, path=str(root / "report.md"))[1]["version"] != first["version"]
    (root / "report.md").unlink()
    assert preview(ctx, path="report.md")[0] == 404


def test_reference_confirmation_does_not_read_or_decode_binary_files(documents: DocumentFixture) -> None:
    root, ctx = documents
    (root / "photo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\xff")
    status, data = preview(ctx, path="photo.png", representation="reference")
    assert status == 200
    assert data["path"] == "photo.png"
    assert "content" not in data
    assert preview(ctx, path="json.dumps", representation="reference")[0] == 404


def test_workspace_images_use_the_same_scope_and_a_bounded_image_contract(documents: DocumentFixture) -> None:
    import base64

    root, ctx = documents
    image = b"\x89PNG\r\n\x1a\n\x00\xff"
    (root / "photo.png").write_bytes(image)
    status, data = preview(ctx, path="photo.png", representation="image")
    assert status == 200
    assert data["format"] == "image"
    assert data["mime"] == "image/png"
    content = data["content"]
    assert isinstance(content, str)
    assert base64.b64decode(content) == image
    assert preview(ctx, path="../photo.png", representation="image")[0] == 400
    (root / "fake.png").write_text("<script>bad</script>", encoding="utf-8")
    assert preview(ctx, path="fake.png", representation="image")[0] == 415


@pytest.mark.parametrize(
    "path", ["../outside.md", "C:/Windows/win.ini", "//server/share/file", "report.md:stream", "a\0b"]
)
def test_document_paths_never_escape_session_workspace(documents: DocumentFixture, path: str) -> None:
    _, ctx = documents
    assert preview(ctx, path=path)[0] == 400


def test_unknown_session_does_not_accept_client_supplied_root(documents: DocumentFixture) -> None:
    root, ctx = documents
    assert preview(ctx, session_id="other", project=str(root), path="report.md")[0] == 404


def test_open_document_cannot_silently_switch_to_a_different_project(documents: DocumentFixture, tmp_path: Path) -> None:
    _, ctx = documents
    first = preview(ctx, path="report.md")[1]
    other = tmp_path / "other"
    other.mkdir()
    (other / "report.md").write_text("different project", encoding="utf-8")
    ctx.projects[0]["path"] = str(other)
    assert preview(ctx, path="report.md", expected_workspace=first["workspace"])[0] == 409


def test_symlink_target_outside_workspace_is_denied(documents: DocumentFixture, tmp_path: Path) -> None:
    root, ctx = documents
    outside = tmp_path / "outside.md"
    outside.write_text("private", encoding="utf-8")
    try:
        (root / "link.md").symlink_to(outside)
    except OSError:
        pytest.skip("Creating symlinks requires Windows developer mode or privileges")
    assert preview(ctx, path="link.md")[0] == 400


def test_binary_and_oversize_are_explicit_not_silently_truncated(documents: DocumentFixture) -> None:
    root, ctx = documents
    (root / "binary.dat").write_bytes(b"\x00\xff")
    assert preview(ctx, path="binary.dat")[1]["error"] == "binary_document"
    (root / "huge.md").write_bytes(b"x" * (2 * 1024 * 1024 + 1))
    assert preview(ctx, path="huge.md")[1]["error"] == "document_too_large"


def test_research_uses_same_document_contract_and_checks_owner(documents: DocumentFixture, tmp_path: Path) -> None:
    from codey.knowledge.note import KnowledgeNote
    from codey.knowledge.store import KnowledgeStore

    _, ctx = documents
    ctx.knowledge_store = KnowledgeStore(tmp_path / "vault")
    note = KnowledgeNote(id="report", type="synthesis", title="Research report", body="# Research\n", session_id="chat")
    ctx.knowledge_store.write_note(note)
    status, result = preview(ctx, kind="research", note_id="report")
    assert status == 200
    stored_note = ctx.knowledge_store.read_note("report")
    assert stored_note is not None
    assert result["content"] == stored_note.body
    assert result["format"] == "markdown"
    note.session_id = "other"
    ctx.knowledge_store.write_note(note)
    assert preview(ctx, kind="research", note_id="report")[0] == 403


def test_shortened_document_reference_resolves_only_a_unique_workspace_suffix(documents: DocumentFixture) -> None:
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
    candidates = data["candidates"]
    assert isinstance(candidates, list)
    paths: list[str] = []
    for candidate in candidates:
        assert isinstance(candidate, str)
        paths.append(candidate)
    assert sorted(paths) == ["other/beauty/cache.py", "selfie/beauty/cache.py"]
    assert preview(ctx, path="selfie/beauty/cache.py")[1]["content"] == "# cache"
