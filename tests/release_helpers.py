from __future__ import annotations

from pathlib import Path
from typing import Any

from codey.app.service import LocalService
from codey.launcher.releases import snapshot
from codey.storage.local_store import write_json_atomic

ROOT = Path(__file__).resolve().parents[1]


def start_local_service(home: Path, **kwargs: Any) -> LocalService:
    manifest = snapshot(home, ROOT)
    write_json_atomic(home / "active-release.json", manifest, mode=0o600)
    return LocalService(home, release_id=manifest["release_id"], **kwargs).start()


def release_manifest(home: Path) -> dict[str, Any]:
    manifest = snapshot(home, ROOT)
    write_json_atomic(home / "active-release.json", manifest, mode=0o600)
    return manifest
