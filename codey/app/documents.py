"""One read-only document contract for session files and research notes."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

from codey.workspace.bounded_scan import BoundedScanBudget, iter_bounded_files
from codey.workspace.config import load_project_config, path_matches_ignored_prefix
from codey.workspace.session import session_workspace

MAX_DOCUMENT_BYTES = 2 * 1024 * 1024


def document_response(ctx: Any, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    session_id = body.get("session_id")
    state = ctx.load_ui_state()
    session = next((s for s in state.get("sessions", []) if s.get("id") == session_id), None)
    if not session:
        return 404, {"ok": False, "error": "session_not_found"}
    project = next((p for p in state.get("projects", []) if p.get("id") == session.get("projectId")), None)
    try:
        if body.get("kind") == "research":
            return _research_document(ctx, body, str(session_id), project)
        if body.get("kind") != "workspace":
            return 400, {"ok": False, "error": "document_kind_invalid"}
        if project:
            root = Path(project["path"]).resolve()
        elif ctx.state_home is not None:
            root = session_workspace(ctx.state_home, str(session_id))
        else:
            return 404, {"ok": False, "error": "workspace_unavailable"}
        if body.get("expected_workspace") is not None and body["expected_workspace"] != str(root):
            return 409, {"ok": False, "error": "document_scope_changed"}
        raw = body.get("path")
        if not isinstance(raw, str) or not raw or "\0" in raw:
            raise ValueError("invalid path")
        raw = raw.replace("\\", "/")
        # No network shares, device paths or NTFS alternate streams.
        if raw.startswith("//") or ":" in (raw[2:] if len(raw) > 1 and raw[1] == ":" else raw):
            raise ValueError("invalid path")
        path = (root / raw).resolve()
        relative = path.relative_to(root).as_posix()
        if not path.is_file() and not Path(raw).is_absolute():
            matches, incomplete = _reference_candidates(root, relative)
            if len(matches) > 1:
                return 409, {
                    "ok": False,
                    "error": "document_ambiguous",
                    "message": "Multiple files match; use a full workspace path.",
                    "candidates": [p.relative_to(root).as_posix() for p in matches],
                }
            if incomplete:
                return 503, {
                    "ok": False,
                    "error": "document_search_incomplete",
                    "message": "File search was incomplete; use a full workspace path.",
                }
            if matches:
                path = matches[0]
                relative = path.relative_to(root).as_posix()
        if not path.is_file():
            return 404, {"ok": False, "error": "document_missing", "message": f"File not found: {relative}"}
        with path.open("rb") as handle:
            data = handle.read(MAX_DOCUMENT_BYTES + 1)
        if len(data) > MAX_DOCUMENT_BYTES:
            return 413, {"ok": False, "error": "document_too_large"}
        if b"\0" in data:
            return 415, {"ok": False, "error": "binary_document"}
        try:
            content = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            return 415, {"ok": False, "error": "document_not_utf8"}
        return 200, _document(relative, path.name, content, _format(path.suffix), str(root))
    except (ValueError, RuntimeError):
        return 400, {"ok": False, "error": "document_path_invalid"}
    except OSError:
        return 403, {"ok": False, "error": "document_unreadable"}


def _reference_candidates(root: Path, reference: str) -> tuple[list[Path], bool]:
    """Resolve a shortened reference only when the bounded workspace scan is complete."""
    config = load_project_config(root)
    if config.warnings:
        return [], True
    ignored = config.config.ignored_paths
    excluded = {".git", ".codey", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache"}
    budget = BoundedScanBudget(max_files=5_000, max_dirs=500, max_dir_entries=1_000)
    suffix = os.path.normcase("/" + reference)

    def allowed(path: Path) -> bool:
        return not path_matches_ignored_prefix(path.relative_to(root).as_posix(), ignored)

    matches = []
    for candidate in iter_bounded_files(root, excluded_dirs=excluded, budget=budget, allow_dir=allowed):
        relative = candidate.relative_to(root).as_posix()
        if os.path.normcase("/" + relative).endswith(suffix):
            resolved = candidate.resolve()
            resolved.relative_to(root)
            matches.append(resolved)
            if len(matches) > 1:
                break
    return matches, budget.limited


def _research_document(
    ctx: Any, body: dict[str, Any], session_id: str, project: dict[str, Any] | None
) -> tuple[int, dict[str, Any]]:
    note_id = body.get("note_id")
    if not isinstance(note_id, str) or not note_id:
        return 400, {"ok": False, "error": "note_id_required"}
    store = ctx.knowledge_store
    found = store.read_notes_with_rows([note_id]).get(note_id) if store is not None else None
    if found is None:
        return 404, {"ok": False, "error": "document_missing"}
    note, row = found
    same_project = bool(project and note.project and Path(note.project).resolve() == Path(project["path"]).resolve())
    if note.session_id != session_id and not same_project:
        return 403, {"ok": False, "error": "document_scope_mismatch"}
    if len(note.body.encode("utf-8")) > MAX_DOCUMENT_BYTES:
        return 413, {"ok": False, "error": "document_too_large"}
    return 200, _document(str(row["path"]), note.title or note.id, note.body, "markdown", f"research:{session_id}")


def _format(suffix: str) -> str:
    return "markdown" if suffix.lower() in {".md", ".markdown"} else "json" if suffix.lower() == ".json" else "text"


def _document(path: str, title: str, content: str, format_: str, workspace: str) -> dict[str, Any]:
    return {
        "ok": True,
        "path": path,
        "title": title,
        "content": content,
        "format": format_,
        "workspace": workspace,
        "version": hashlib.sha256(content.encode("utf-8")).hexdigest(),
    }
