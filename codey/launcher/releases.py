"""Immutable runtime snapshots; activation is owned by a standalone process."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import tomllib
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING, Any

if os.name == "nt" or TYPE_CHECKING:
    # CREATE_NO_WINDOW is a Windows-only subprocess constant and is absent
    # from Linux typeshed.
    _windows_subprocess: Any = subprocess

from codey.control.protocol import ControlError, fingerprint
from codey.storage.file_lock import acquire_lease
from codey.storage.local_store import read_json_strict, write_json_atomic


def release_path(home: Path, identity: str) -> Path:
    if not re.fullmatch(r"[a-z0-9_-]{1,80}", identity):
        raise ControlError("INVALID_ARGUMENT", "Invalid release identity")
    return home / "releases" / identity


def runtime_files(root: Path) -> list[Path]:
    files = [root / "pyproject.toml"]
    for directory, dirs, names in os.walk(root / "codey", followlinks=False):
        for name in dirs:
            if Path(directory, name).is_symlink():
                raise ControlError("INVALID_ARGUMENT", "Runtime snapshot cannot contain symbolic links")
        dirs[:] = sorted(name for name in dirs if name != "__pycache__")
        files.extend(Path(directory, name) for name in sorted(names) if not name.endswith((".pyc", ".log")))
    if not (root / "codey" / "__main__.py").is_file() or not (root / "codey" / "web" / "index.html").is_file():
        raise ControlError("INVALID_ARGUMENT", "Release requires Codey runtime and a built desktop frontend")
    if len(files) > 10_000:
        raise ControlError("INVALID_ARGUMENT", "Runtime snapshot has too many files")
    return sorted(files)


def runtime_digest(root: Path) -> str:
    rows = []
    total = 0
    for path in runtime_files(root):
        if path.is_symlink():
            raise ControlError("INVALID_ARGUMENT", "Runtime snapshot cannot contain symbolic links")
        size = path.stat().st_size
        total += size
        if size > 32 * 1024 * 1024 or total > 128 * 1024 * 1024:
            raise ControlError("INVALID_ARGUMENT", "Runtime snapshot exceeds size limits")
        rows.append([path.relative_to(root).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest()])
    return fingerprint(rows)


def snapshot(home: Path, source: Path) -> dict[str, Any]:
    digest = runtime_digest(source)
    identity = "release_" + digest[:32]
    directory = release_path(home, identity)
    target = directory / "source"
    if not target.exists():
        target.mkdir(parents=True)
        for path in runtime_files(source):
            destination = target / path.relative_to(source)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
    if runtime_digest(target) != digest or runtime_digest(source) != digest:
        raise ControlError("REVISION_CONFLICT", "Release changed while being snapshotted; candidate was not accepted")
    return {"release_id": identity, "candidate_digest": digest, "source": str(target), "created_at": time.time()}


def ensure_frontend(source: Path) -> None:
    """Materialize the generated frontend before the runtime is snapshotted.

    Installed packages contain the already-published ``codey/web`` tree and do
    not necessarily contain Node or the frontend sources. A source checkout,
    however, must update that tree automatically when its frontend inputs
    changed. The publish script remains the single owner of the generated
    tree and its source hash.
    """
    frontend = source / "frontend"
    if not frontend.is_dir():
        return
    publish = source / "tools" / "publish_frontend.py"
    if not publish.is_file():
        raise ControlError("FRONTEND_BUILD_FAILED", "Frontend sources exist but the publisher is unavailable")

    def run(argv: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(argv, cwd=source, stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True, encoding="utf-8",
                                  timeout=timeout, check=False,
                                  creationflags=_windows_subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ControlError("FRONTEND_BUILD_FAILED", "Frontend build process could not be started or completed",
                               details={"command": argv, "error": str(exc)[:1000]}) from exc

    check = run([sys.executable, "-m", "tools.publish_frontend", "--check"], timeout=60)
    if check.returncode == 0:
        return
    build = run(["npm", "run", "build"], timeout=300)
    if build.returncode != 0:
        raise ControlError("FRONTEND_BUILD_FAILED", "Frontend build failed",
                           details={"command": ["npm", "run", "build"],
                                    "stdout": (build.stdout or "")[-4000:],
                                    "stderr": (build.stderr or "")[-4000:]})
    publish_result = run([sys.executable, "-m", "tools.publish_frontend"], timeout=60)
    if publish_result.returncode != 0:
        raise ControlError("FRONTEND_BUILD_FAILED", "Frontend publication failed",
                           details={"command": [sys.executable, "-m", "tools.publish_frontend"],
                                    "stdout": (publish_result.stdout or "")[-4000:],
                                    "stderr": (publish_result.stderr or "")[-4000:]})


def _dependencies(source: Path) -> object:
    data = tomllib.loads((source / "pyproject.toml").read_text(encoding="utf-8"))
    project = data["project"]
    return [project.get("requires-python"), project.get("dependencies"), project.get("optional-dependencies", {}).get("mcp")]


def _verification(directory: Path, manifest: dict[str, Any], commands: list[list[str]]) -> None:
    results = manifest.setdefault("verification", [])
    for argv in commands:
        result: dict[str, Any] = {"argv": argv, "state": "started", "started_at": time.time()}
        results.append(result)
        write_json_atomic(directory / "manifest.json", manifest, mode=0o600)
        with (directory / f"validation-{len(results)}.log").open("wb") as output:
            try:
                done = subprocess.run(argv, cwd=manifest["validation_source"], stdin=subprocess.DEVNULL,
                                      stdout=output, stderr=subprocess.STDOUT, timeout=60,
                                      creationflags=_windows_subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                result.update(state="settled", exit_code=done.returncode)
            except subprocess.TimeoutExpired:
                result.update(state="timeout", exit_code=None)
        write_json_atomic(directory / "manifest.json", manifest, mode=0o600)
        if result["state"] != "settled" or result["exit_code"] != 0:
            raise ControlError("VALIDATION_FAILED", "Candidate validation failed; inspect release validation logs")


def prepare(service: Any, args: dict[str, Any]) -> dict[str, Any]:
    home = service.ctx.state_home
    receipt = home / "release-requests" / (fingerprint(args["request_id"]) + ".json")
    with acquire_lease(home / "release-preparation"):
        old = read_json_strict(receipt)
        if old:
            if old["fingerprint"] != fingerprint(args):
                raise ControlError("REQUEST_CONFLICT", "Release request ID names different content")
            if old.get("error"):
                raise ControlError(old["error"]["code"], old["error"]["message"])
            if "result" not in old:
                raise ControlError("OUTCOME_UNKNOWN", "Validation already started; inspect its manifest rather than replaying commands")
            return dict(old["result"])
        manifest = snapshot(home, Path(args["source"]).expanduser().resolve())
        directory = release_path(home, manifest["release_id"])
        previous = read_json_strict(directory / "manifest.json")
        if previous and previous.get("status") == "validating":
            raise ControlError("OUTCOME_UNKNOWN", "An interrupted validation needs a new candidate; it is never silently replayed")
        current = read_json_strict(home / "active-release.json")
        if not current or _dependencies(Path(manifest["source"])) != _dependencies(Path(current["source"])):
            raise ControlError("DEPENDENCY_MISMATCH", "Automatic activation requires the same runtime dependencies")
        commands = args.get("verification", [])
        if len(commands) > 8 or any(not isinstance(row, list) or not row or not all(isinstance(word, str) and word for word in row) for row in commands):
            raise ControlError("INVALID_ARGUMENT", "verification must contain at most eight non-empty argv arrays")
        check_dir = directory / "checks" / fingerprint(args["request_id"])[:32]
        validation_source = check_dir / "source"
        manifest.update(status="validating", verification=[], validation_source=str(validation_source))
        write_json_atomic(directory / "manifest.json", manifest, mode=0o600)
        write_json_atomic(receipt, {"fingerprint": fingerprint(args), "status": "validating"}, mode=0o600)
        defaults = [[sys.executable, "-m", "compileall", "-q", "codey"], [sys.executable, "-m", "codey", "--help"]]
        try:
            validation_source.mkdir(parents=True)
            for original in runtime_files(Path(manifest["source"])):
                destination = validation_source / original.relative_to(manifest["source"])
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(original, destination)
            _verification(directory, manifest, defaults + commands)
            if runtime_digest(validation_source) != manifest["candidate_digest"]:
                raise ControlError("REVISION_CONFLICT", "Validation modified its isolated runtime copy")
            # Only SQLite's backup API is used. The original remains live; this
            # copy is evidence and preflight input, never a rollback overwrite.
            probe = check_dir / "state"
            probe.mkdir(exist_ok=True)
            for database in home.glob("*.sqlite3"):
                with closing(sqlite3.connect(database)) as source_db, closing(sqlite3.connect(probe / database.name)) as destination_db:
                    source_db.backup(destination_db)
            from codey.launcher import runner

            before = runner.schema(probe)
            _verification(directory, manifest, [[sys.executable, "-c",
                "import sys; from pathlib import Path; from codey.app.service import LocalService; "
                "s=LocalService(Path(sys.argv[1]),verify_only=True,release_id=sys.argv[2]); s.start(); s.close()",
                str(probe), manifest["release_id"]]])
            if runner.schema(probe) != before:
                raise ControlError("SCHEMA_MISMATCH", "Automatic activation requires a backward-compatible unchanged schema")
            if runtime_digest(Path(manifest["source"])) != manifest["candidate_digest"]:
                raise ControlError("REVISION_CONFLICT", "Validation modified runtime files")
            manifest.update(status="validated", schema_digest=before)
        except Exception as exc:
            manifest["status"] = "failed"
            write_json_atomic(directory / "manifest.json", manifest, mode=0o600)
            error = exc if isinstance(exc, ControlError) else ControlError("VALIDATION_FAILED", "Candidate validation did not complete; inspect its logs")
            write_json_atomic(receipt, {"fingerprint": fingerprint(args), "error": error.payload()}, mode=0o600)
            raise error from exc
        write_json_atomic(directory / "manifest.json", manifest, mode=0o600)
        result = {key: manifest[key] for key in ("release_id", "candidate_digest", "status", "verification")}
        write_json_atomic(receipt, {"fingerprint": fingerprint(args), "result": result}, mode=0o600)
        return result


def activate(service: Any, args: dict[str, Any]) -> dict[str, Any]:
    home = service.ctx.state_home
    identity = "upgrade_" + fingerprint(args["request_id"])[:32]
    path = home / "upgrades" / (identity + ".json")
    with acquire_lease(home / "release-preparation"):
        old = read_json_strict(path)
        if old:
            if old["request_fingerprint"] != fingerprint(args):
                raise ControlError("REQUEST_CONFLICT", "Upgrade request ID names different content")
            return {"upgrade_id": identity, "phase": old["phase"]}
        if args["expected_instance_id"] != service.instance_id:
            raise ControlError("RUN_MISMATCH", "Activation names an old service instance")
        directory = release_path(home, args["candidate_release"])
        manifest = read_json_strict(directory / "manifest.json")
        if not manifest or manifest.get("status") != "validated" or manifest["candidate_digest"] != args["candidate_digest"]:
            raise ControlError("VALIDATION_FAILED", "Activation requires the validated candidate digest")
        if runtime_digest(Path(manifest["source"])) != args["candidate_digest"]:
            raise ControlError("REVISION_CONFLICT", "Candidate runtime changed after validation")
        timeout = args.get("timeout_seconds", 60)
        if not 5 <= timeout <= 300:
            raise ControlError("INVALID_ARGUMENT", "Upgrade timeout must be between 5 and 300 seconds")
        from codey.launcher import runner

        launcher = home / "launcher" / "runner.py"
        launcher.parent.mkdir(parents=True, exist_ok=True)
        # Per-operation copy: an update cannot overwrite an in-flight runner.
        launcher = launcher.with_name(identity + ".py")
        launcher.write_bytes(Path(runner.__file__).read_bytes())
        journal = {"upgrade_id": identity, "request_fingerprint": fingerprint(args), "phase": "accepted",
                   "candidate": manifest, "previous": read_json_strict(home / "active-release.json"),
                   "old_instance_id": service.instance_id, "deadline": time.time() + timeout,
                   "allowed_roots": [str(root) for root in service.allowed_roots], "python": sys.executable,
                   "admission_may_have_opened": False, "attempts": 0, "events": list[dict[str, Any]]()}
        write_json_atomic(path, journal, mode=0o600)
        with (home / "launcher" / (identity + ".log")).open("ab") as output:
            subprocess.Popen([sys.executable, str(launcher), str(home), identity], cwd=home,
                             stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                             creationflags=_windows_subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        return {"upgrade_id": identity, "phase": "accepted"}


def release_command(service: Any, name: str, args: dict[str, Any]) -> dict[str, Any]:
    if name == "prepare_release":
        return prepare(service, args)
    if name == "activate_release":
        return activate(service, args)
    return read_upgrade(service.ctx.state_home, args["upgrade_id"])


def read_upgrade(home: Path, identity: str) -> dict[str, Any]:
    if not re.fullmatch(r"upgrade_[a-f0-9]{32}", identity):
        raise ControlError("INVALID_ARGUMENT", "Invalid upgrade identity")
    journal = read_json_strict(home / "upgrades" / (identity + ".json"))
    if not journal:
        raise ControlError("NOT_FOUND", "Upgrade not found")
    return {key: value for key, value in journal.items() if key not in {"python", "request_fingerprint"}}
