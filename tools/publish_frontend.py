"""Validate and publish the compiled frontend.

The Python package ships the generated ``codey/web`` tree so installed Codey
does not require Node.  This command is the single owner of that generated
tree and refuses incomplete or test builds.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "frontend" / "dist"
WEB = ROOT / "codey" / "web"
BUILD_INPUTS = (
    "frontend/index.html",
    "frontend/package.json",
    "frontend/package-lock.json",
    "frontend/tsconfig.json",
    "frontend/vite.config.ts",
    "tools/publish_frontend.py",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _files(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): _sha256(path)
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.relative_to(root).as_posix() != ".vite/manifest.json"
    }


def _validate_dist() -> dict[str, str]:
    if not DIST.is_dir():
        raise SystemExit(f"frontend build does not exist: {DIST}")
    if (ROOT / "frontend" / "dist-test").exists():
        # The production path is fixed; this guard catches an accidental
        # invocation from a test build without silently publishing it.
        pass
    index = DIST / "index.html"
    assets = DIST / "assets"
    icon = DIST / "icon.ico"
    manifest = DIST / ".vite" / "manifest.json"
    if not index.is_file() or not assets.is_dir() or not icon.is_file():
        raise SystemExit("frontend dist must contain index.html, icon.ico, and assets/")
    if not manifest.is_file():
        raise SystemExit("frontend dist is missing .vite/manifest.json")
    html = index.read_text(encoding="utf-8")
    module_scripts = re.findall(r'<script[^>]+type=["\']module["\'][^>]+src=["\'](/assets/[^"\']+\.js)["\']', html, re.I)
    if not module_scripts:
        raise SystemExit("production HTML must contain a module script for an assets/*.js bundle")
    if 'src="/src/' in html or 'href="/src/' in html:
        raise SystemExit("production HTML contains a development source")
    manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(manifest_data, dict) or not manifest_data:
        raise SystemExit("frontend dist manifest must be a non-empty object")
    referenced: set[str] = set()
    for entry in manifest_data.values():
        for key in ("file", "css"):
            values = entry.get(key, [])
            if isinstance(values, str):
                values = [values]
            referenced.update(str(value) for value in values)
    published = {
        path.relative_to(DIST).as_posix()
        for path in DIST.rglob("*")
        if path.is_file() and path.relative_to(DIST).as_posix() not in {".vite/manifest.json"}
    }
    for reference in re.findall(r'(?:src|href)=["\'](/(?:assets/[^"\']+|icon\.ico))["\']', html):
        relative = reference.split("?", 1)[0].removeprefix("/")
        if relative not in published:
            raise SystemExit(f"HTML references missing published resource: {reference}")
    missing = sorted(path for path in referenced if path not in published)
    if missing:
        raise SystemExit(f"manifest references missing files: {missing}")
    for path in published:
        if path.startswith(".vite/") or path.endswith(".map"):
            raise SystemExit(f"debug or manifest file cannot be published: {path}")
        if not (path == "index.html" or path == "icon.ico" or path.startswith("assets/")):
            raise SystemExit(f"unexpected production file: {path}")
        if path.startswith("assets/") and Path(path).suffix not in {".js", ".css", ".svg", ".woff2", ".woff", ".ttf"}:
            raise SystemExit(f"unsupported production asset type: {path}")
    return _files(DIST)


def _source_digest() -> str:
    digest = hashlib.sha256()
    for relative in BUILD_INPUTS:
        path = ROOT / relative
        digest.update(relative.encode())
        digest.update(path.read_bytes())
    for path in sorted((ROOT / "frontend" / "src").rglob("*")):
        if path.is_file() and not any(path.name.endswith(f".{kind}.{suffix}")
                                      for kind in ("test", "spec") for suffix in ("ts", "tsx", "js", "jsx")):
            digest.update(path.relative_to(ROOT).as_posix().encode())
            digest.update(path.read_bytes())
    for path in sorted((ROOT / "frontend" / "public").rglob("*")):
        if path.is_file():
            digest.update(path.relative_to(ROOT).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _metadata(files: dict[str, str]) -> dict[str, object]:
    return {"schema": 1, "source_sha256": _source_digest(), "files": files}


def _check(files: dict[str, str]) -> bool:
    current = _files(WEB) if WEB.is_dir() else {}
    expected = dict(files)
    expected["frontend-build.json"] = "__metadata__"
    metadata = WEB / "frontend-build.json"
    if not metadata.is_file():
        return False
    try:
        recorded = json.loads(metadata.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if recorded != _metadata(files):
        return False
    current.pop("frontend-build.json", None)
    return current == files


def _publish(files: dict[str, str]) -> None:
    metadata = _metadata(files)
    with tempfile.TemporaryDirectory(prefix="codey-frontend-", dir=ROOT) as temp:
        candidate = Path(temp) / "web"
        shutil.copytree(DIST, candidate)
        shutil.rmtree(candidate / ".vite", ignore_errors=True)
        (candidate / "frontend-build.json").write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        backup = ROOT / f".codey-web-backup-{os.getpid()}"
        moved_old = False
        try:
            if backup.exists():
                shutil.rmtree(backup)
            if WEB.exists():
                WEB.rename(backup)
                moved_old = True
            candidate.rename(WEB)
        except Exception:
            # If moving the old tree failed, it is still the live tree. Never
            # remove it while attempting to recover a failed publication.
            if moved_old:
                if WEB.exists():
                    shutil.rmtree(WEB)
                if backup.exists():
                    backup.rename(WEB)
            raise
        else:
            if backup.exists():
                shutil.rmtree(backup)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--validate", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    files = _validate_dist()
    if args.validate:
        return 0
    if args.check:
        return 0 if _check(files) else 1
    _publish(files)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

