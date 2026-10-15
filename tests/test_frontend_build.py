from __future__ import annotations

from pathlib import Path

import pytest

from codey.app import http_plumbing
from tools import publish_frontend


def test_candidate_frontend_root_contains_only_publishable_runtime_files(tmp_path: Path) -> None:
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text('<html><script type="module" src="/assets/app.js"></script></html>', encoding="utf-8")
    (root / "assets" / "app.js").write_text("export {}", encoding="utf-8")
    assert http_plumbing.validate_web_root(root) == root.resolve()


def test_empty_frontend_root_is_rejected_even_when_index_and_assets_exist(tmp_path: Path) -> None:
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<html></html>", encoding="utf-8")
    (root / "assets" / "app.js").write_text("export {}", encoding="utf-8")
    with pytest.raises(RuntimeError, match="module script"):
        http_plumbing.validate_web_root(root)


def test_invalid_candidate_frontend_root_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="index.html and assets"):
        http_plumbing.validate_web_root(tmp_path / "missing")


def test_publish_keeps_live_frontend_when_backup_move_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("new", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("export {}", encoding="utf-8")
    web = tmp_path / "web"
    (web / "assets").mkdir(parents=True)
    (web / "index.html").write_text("old", encoding="utf-8")
    monkeypatch.setattr(publish_frontend, "ROOT", tmp_path)
    monkeypatch.setattr(publish_frontend, "DIST", dist)
    monkeypatch.setattr(publish_frontend, "WEB", web)
    monkeypatch.setattr(publish_frontend, "_metadata", lambda files: {"schema": 1, "files": files})
    original_rename = Path.rename

    def fail_backup(source: Path, target: Path):
        if source == web:
            raise OSError("backup move failed")
        return original_rename(source, target)

    monkeypatch.setattr(Path, "rename", fail_backup)
    with pytest.raises(OSError, match="backup move failed"):
        publish_frontend._publish({"index.html": "old-hash", "assets/app.js": "new-hash"})
    assert (web / "index.html").read_text(encoding="utf-8") == "old"


def test_publish_validation_rejects_unsupported_assets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / ".vite").mkdir()
    (dist / "index.html").write_text('<script type="module" src="/assets/app.js"></script>', encoding="utf-8")
    (dist / "icon.ico").write_bytes(b"ico")
    (dist / "assets" / "app.js").write_text("export {}", encoding="utf-8")
    (dist / "assets" / "unsupported.wasm").write_bytes(b"wasm")
    (dist / ".vite" / "manifest.json").write_text('{"main":{"file":"assets/app.js"}}', encoding="utf-8")
    monkeypatch.setattr(publish_frontend, "DIST", dist)
    with pytest.raises(SystemExit, match="unsupported production asset type"):
        publish_frontend._validate_dist()


def test_publish_validation_rejects_empty_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / ".vite").mkdir()
    (dist / "index.html").write_text('<script type="module" src="/assets/app.js"></script>', encoding="utf-8")
    (dist / "icon.ico").write_bytes(b"ico")
    (dist / "assets" / "app.js").write_text("export {}", encoding="utf-8")
    (dist / ".vite" / "manifest.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(publish_frontend, "DIST", dist)
    with pytest.raises(SystemExit, match="non-empty object"):
        publish_frontend._validate_dist()



def test_packaged_svg_icon_is_served_with_svg_mime(tmp_path, monkeypatch):
    root = tmp_path / "web"
    (root / "assets").mkdir(parents=True)
    icon = root / "assets" / "new-chat.svg"
    icon.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>', encoding="utf-8")
    monkeypatch.setattr(http_plumbing, "WEB_DIR", root)
    assert http_plumbing.resolve_web_asset("/assets/new-chat.svg") == (icon.resolve(), "image/svg+xml")
    assert http_plumbing.resolve_web_asset("/assets/../new-chat.svg") is None
