"""Immutable runtime snapshots; activation is owned by a standalone process."""

from __future__ import annotations

import hashlib
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal, NotRequired, Protocol, TypedDict, cast

from codey.control.protocol import ControlError, fingerprint
from codey.launcher.runtime import dependencies, runtime_files
from codey.launcher.startup import startup_stage
from codey.launcher.validation_cache import Verification, cached_checks, save_checks, validation_key
from codey.storage.atomic_io import filesystem_path
from codey.storage.file_lock import acquire_lease
from codey.storage.local_store import read_json_strict, write_json_atomic

if sys.platform == "win32":
    from subprocess import CREATE_NO_WINDOW
else:
    CREATE_NO_WINDOW = 0


class ReleaseSnapshot(TypedDict):
    release_id: str
    candidate_digest: str
    source: str
    created_at: float


class _Manifest(ReleaseSnapshot):
    status: Literal["validating", "validated", "failed"]
    verification: list[Verification]
    validation_source: str
    static_validation_key: NotRequired[str]
    schema_digest: NotRequired[str]


class PrepareArgs(TypedDict):
    request_id: str
    source: str
    verification: NotRequired[list[list[str]]]


class ActivationTarget(TypedDict):
    candidate_release: str
    candidate_digest: str
    expected_instance_id: str


class ActivateArgs(ActivationTarget):
    request_id: str
    timeout_seconds: NotRequired[int]


class PreparedRelease(TypedDict):
    release_id: str
    candidate_digest: str
    status: Literal["validated"]
    verification: list[Verification]


class _PreparationError(TypedDict):
    code: str
    message: str


class _PreparationReceipt(TypedDict):
    fingerprint: str
    error: NotRequired[_PreparationError]
    result: NotRequired[PreparedRelease]


class Activation(TypedDict):
    upgrade_id: str
    phase: str


class _UpgradeEvent(TypedDict):
    phase: str
    time: float


class _UpgradeJournal(Activation):
    request_fingerprint: str
    candidate: _Manifest
    previous: ReleaseSnapshot | None
    old_instance_id: str
    deadline: float
    allowed_roots: list[str]
    python: str
    admission_may_have_opened: bool
    attempts: int
    events: list[_UpgradeEvent]


class _ReleaseContext(Protocol):
    @property
    def state_home(self) -> Path: ...


class _PreparationOwner(Protocol):
    @property
    def ctx(self) -> _ReleaseContext: ...


class _ActivationOwner(_PreparationOwner, Protocol):
    @property
    def instance_id(self) -> str: ...

    @property
    def allowed_roots(self) -> tuple[Path, ...]: ...


def release_path(home: Path, identity: str) -> Path:
    if not re.fullmatch(r"[a-z0-9_-]{1,80}", identity):
        raise ControlError("INVALID_ARGUMENT", "Invalid release identity")
    return home / "releases" / identity


def runtime_digest(root: Path) -> str:
    rows = []
    total = 0
    for path in runtime_files(root):
        path = filesystem_path(path)
        if path.is_symlink():
            raise ControlError("INVALID_ARGUMENT", "Runtime snapshot cannot contain symbolic links")
        size = path.stat().st_size
        total += size
        if size > 32 * 1024 * 1024 or total > 128 * 1024 * 1024:
            raise ControlError("INVALID_ARGUMENT", "Runtime snapshot exceeds size limits")
        rows.append([path.relative_to(filesystem_path(root)).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest()])
    return fingerprint(rows)


def snapshot(home: Path, source: Path, *, identity_suffix: str = "") -> ReleaseSnapshot:
    conflict = "Release changed while being snapshotted; candidate was not accepted"
    try:
        digest = runtime_digest(source)
        suffix = ("_" + identity_suffix) if identity_suffix else ""
        identity = "release_" + digest[:32] + suffix
        directory = release_path(home, identity)
        target = directory / "source"
        if target.exists():
            if runtime_digest(target) != digest or runtime_digest(source) != digest:
                raise ControlError("REVISION_CONFLICT", conflict)
        else:
            releases = filesystem_path(directory.parent)
            releases.mkdir(parents=True, exist_ok=True)
            # Publish the directory only after both sides match the initial digest.
            with TemporaryDirectory(prefix=".snapshot-", dir=releases) as temporary:
                staged = Path(temporary)
                for path in runtime_files(source):
                    destination = staged / path.relative_to(source)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(filesystem_path(path), destination)
                if runtime_digest(staged) != digest or runtime_digest(source) != digest:
                    raise ControlError("REVISION_CONFLICT", conflict)
                filesystem_path(directory).mkdir(exist_ok=True)
                staged.rename(filesystem_path(target))
    except FileNotFoundError as exc:
        raise ControlError("REVISION_CONFLICT", conflict) from exc
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

    def run(argv: list[str], *, timeout: int, cwd: Path = source) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(argv, cwd=cwd, stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True, encoding="utf-8",
                                  timeout=timeout, check=False,
                                  creationflags=CREATE_NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ControlError("FRONTEND_BUILD_FAILED", "Frontend build process could not be started or completed",
                               details={"command": argv, "error": str(exc)[:1000]}) from exc

    check = run([sys.executable, "-m", "tools.publish_frontend", "--check"], timeout=60)
    if check.returncode == 0:
        return
    build_command = ["npm.cmd" if sys.platform == "win32" else "npm", "run", "build"]
    build = run(build_command, timeout=300, cwd=frontend)
    if build.returncode != 0:
        raise ControlError("FRONTEND_BUILD_FAILED", "Frontend build failed",
                           details={"command": build_command,
                                    "stdout": (build.stdout or "")[-4000:],
                                    "stderr": (build.stderr or "")[-4000:]})
    publish_result = run([sys.executable, "-m", "tools.publish_frontend"], timeout=60)
    if publish_result.returncode != 0:
        raise ControlError("FRONTEND_BUILD_FAILED", "Frontend publication failed",
                           details={"command": [sys.executable, "-m", "tools.publish_frontend"],
                                    "stdout": (publish_result.stdout or "")[-4000:],
                                    "stderr": (publish_result.stderr or "")[-4000:]})


def _verification(directory: Path, manifest: _Manifest, commands: list[list[str]]) -> None:
    results = manifest.setdefault("verification", [])
    for argv in commands:
        result: Verification = {"argv": argv, "state": "started", "started_at": time.time()}
        results.append(result)
        write_json_atomic(directory / "manifest.json", dict(manifest), mode=0o600)
        with (directory / f"validation-{len(results)}.log").open("wb") as output:
            try:
                started = time.perf_counter()
                with startup_stage("validation.command"):
                    # Python imports must retain the Windows extended namespace too;
                    # copying long module paths successfully is not sufficient.
                    done = subprocess.run(argv, cwd=filesystem_path(Path(manifest["validation_source"])), stdin=subprocess.DEVNULL,
                                          stdout=output, stderr=subprocess.STDOUT, timeout=60,
                                          creationflags=CREATE_NO_WINDOW)
                result["state"] = "settled"
                result["exit_code"] = done.returncode
            except subprocess.TimeoutExpired:
                result["state"] = "timeout"
                result["exit_code"] = None
            finally:
                result["finished_at"] = time.time()
                result["elapsed_seconds"] = time.perf_counter() - started
        write_json_atomic(directory / "manifest.json", dict(manifest), mode=0o600)
        if result["state"] != "settled" or result["exit_code"] != 0:
            raise ControlError("VALIDATION_FAILED", "Candidate validation failed; inspect release validation logs")


def _static_commands() -> list[list[str]]:
    # compileall's temporary .pyc names can exceed MAX_PATH in an installed
    # release snapshot. Resolve the candidate root in the child process and
    # keep its extended filesystem namespace through compilation.
    compile_candidate = (
        "import compileall,sys; from pathlib import Path; "
        "from codey.storage.atomic_io import filesystem_path; "
        "sys.exit(not compileall.compile_dir(str(filesystem_path(Path('codey').resolve())), quiet=1))"
    )
    return [[sys.executable, "-c", compile_candidate], [sys.executable, "-m", "codey", "--help"]]


def _static_verification(home: Path, directory: Path, manifest: _Manifest) -> None:
    commands = _static_commands()
    with startup_stage("validation.static"):
        key = validation_key(manifest["candidate_digest"], commands)
        rows = cached_checks(home, key, commands)
        if rows is None:
            _verification(directory, manifest, commands)
        else:
            manifest["verification"].extend(rows)
        if runtime_digest(Path(manifest["validation_source"])) != manifest["candidate_digest"]:
            raise ControlError("REVISION_CONFLICT", "Validation modified its isolated runtime copy")
        if validation_key(manifest["candidate_digest"], commands) != key:
            raise ControlError("DEPENDENCY_MISMATCH", "Validation environment changed during the check")
        if rows is None:
            try:
                with startup_stage("validation.cache_write"):
                    save_checks(home, key, commands, manifest["verification"][:len(commands)])
            except OSError:
                pass  # Optional evidence cache; the checks above have still succeeded.
        manifest["static_validation_key"] = key


def prepare(service: _PreparationOwner, args: PrepareArgs) -> PreparedRelease:
    home = service.ctx.state_home
    receipt = home / "release-requests" / (fingerprint(args["request_id"]) + ".json")
    with acquire_lease(home / "release-preparation"):
        old = cast(_PreparationReceipt | None, read_json_strict(receipt))
        if old:
            if old["fingerprint"] != fingerprint(args):
                raise ControlError("REQUEST_CONFLICT", "Release request ID names different content")
            if error := old.get("error"):
                raise ControlError(error["code"], error["message"])
            if "result" not in old:
                raise ControlError("OUTCOME_UNKNOWN", "Validation already started; inspect its manifest rather than replaying commands")
            return old["result"]
        source = Path(args["source"]).expanduser().resolve()
        candidate = snapshot(home, source)
        directory = release_path(home, candidate["release_id"])
        previous = read_json_strict(directory / "manifest.json")
        if previous and previous.get("status") == "validating":
            # A digest normally names one immutable candidate. If validation
            # was interrupted, derive a fresh identity so recovery never
            # replays the old request or mutates its evidence directory.
            recovery_suffix = fingerprint(args["request_id"] + ":recovery")[:16]
            candidate = snapshot(home, source, identity_suffix=recovery_suffix)
            directory = release_path(home, candidate["release_id"])
            previous = read_json_strict(directory / "manifest.json")
            if previous and previous.get("status") == "validating":
                raise ControlError("OUTCOME_UNKNOWN", "A recovery validation is already in progress; inspect its manifest")
        current = cast(ReleaseSnapshot | None, read_json_strict(home / "active-release.json"))
        if not current or dependencies(Path(candidate["source"])) != dependencies(Path(current["source"])):
            raise ControlError("DEPENDENCY_MISMATCH", "Automatic activation requires the same runtime dependencies")
        commands = args.get("verification", [])
        if len(commands) > 8 or any(not isinstance(row, list) or not row or not all(isinstance(word, str) and word for word in row) for row in commands):
            raise ControlError("INVALID_ARGUMENT", "verification must contain at most eight non-empty argv arrays")
        check_dir = directory / "checks" / fingerprint(args["request_id"])[:32]
        validation_source = check_dir / "source"
        manifest: _Manifest = {
            "release_id": candidate["release_id"],
            "candidate_digest": candidate["candidate_digest"],
            "source": candidate["source"],
            "created_at": candidate["created_at"],
            "status": "validating",
            "verification": [],
            "validation_source": str(validation_source),
        }
        write_json_atomic(directory / "manifest.json", dict(manifest), mode=0o600)
        write_json_atomic(receipt, {"fingerprint": fingerprint(args), "status": "validating"}, mode=0o600)
        try:
            validation_source.mkdir(parents=True)
            for original in runtime_files(Path(manifest["source"])):
                destination = filesystem_path(validation_source / original.relative_to(manifest["source"]))
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(original, destination)
            _static_verification(home, directory, manifest)
            _verification(directory, manifest, commands)
            if runtime_digest(validation_source) != manifest["candidate_digest"]:
                raise ControlError("REVISION_CONFLICT", "Validation modified its isolated runtime copy")
            # Only SQLite's backup API is used. The original remains live; this
            # copy is evidence and preflight input, never a rollback overwrite.
            probe = check_dir / "state"
            probe.mkdir(exist_ok=True)
            with startup_stage("validation.database_backup"):
                for database in home.glob("*.sqlite3"):
                    with closing(sqlite3.connect(database)) as source_db, closing(sqlite3.connect(probe / database.name)) as destination_db:
                        source_db.backup(destination_db)
            from codey.launcher import runner

            before = runner.schema(probe)
            with startup_stage("validation.database_preflight"):
                _verification(directory, manifest, [[sys.executable, "-c",
                    "import sys; from pathlib import Path; from codey.app.service import LocalService; "
                    "s=LocalService(Path(sys.argv[1]),verify_only=True,release_id=sys.argv[2]); s.start(); s.close()",
                    str(probe), manifest["release_id"]]])
            if runner.schema(probe) != before:
                raise ControlError("SCHEMA_MISMATCH", "Automatic activation requires a backward-compatible unchanged schema")
            if runtime_digest(Path(manifest["source"])) != manifest["candidate_digest"]:
                raise ControlError("REVISION_CONFLICT", "Validation modified runtime files")
            manifest["status"] = "validated"
            manifest["schema_digest"] = before
        except Exception as exc:
            manifest["status"] = "failed"
            write_json_atomic(directory / "manifest.json", dict(manifest), mode=0o600)
            failure = exc if isinstance(exc, ControlError) else ControlError("VALIDATION_FAILED", "Candidate validation did not complete; inspect its logs")
            write_json_atomic(receipt, {"fingerprint": fingerprint(args), "error": failure.payload()}, mode=0o600)
            raise failure from exc
        write_json_atomic(directory / "manifest.json", dict(manifest), mode=0o600)
        result: PreparedRelease = {"release_id": manifest["release_id"], "candidate_digest": manifest["candidate_digest"],
                                   "status": "validated", "verification": manifest["verification"]}
        write_json_atomic(receipt, {"fingerprint": fingerprint(args), "result": result}, mode=0o600)
        return result


def activate(service: _ActivationOwner, args: ActivateArgs) -> Activation:
    home = service.ctx.state_home
    identity = "upgrade_" + fingerprint(args["request_id"])[:32]
    path = home / "upgrades" / (identity + ".json")
    with acquire_lease(home / "release-preparation"):
        old = cast(_UpgradeJournal | None, read_json_strict(path))
        if old:
            if old["request_fingerprint"] != fingerprint(args):
                raise ControlError("REQUEST_CONFLICT", "Upgrade request ID names different content")
            return {"upgrade_id": identity, "phase": old["phase"]}
        if args["expected_instance_id"] != service.instance_id:
            raise ControlError("RUN_MISMATCH", "Activation names an old service instance")
        directory = release_path(home, args["candidate_release"])
        manifest = cast(_Manifest | None, read_json_strict(directory / "manifest.json"))
        if not manifest or manifest.get("status") != "validated" or manifest["candidate_digest"] != args["candidate_digest"]:
            raise ControlError("VALIDATION_FAILED", "Activation requires the validated candidate digest")
        if runtime_digest(Path(manifest["source"])) != args["candidate_digest"]:
            raise ControlError("REVISION_CONFLICT", "Candidate runtime changed after validation")
        if manifest.get("static_validation_key") != validation_key(args["candidate_digest"], _static_commands()):
            raise ControlError("DEPENDENCY_MISMATCH", "Validation environment changed; prepare the candidate again")
        timeout = args.get("timeout_seconds", 60)
        if not 5 <= timeout <= 300:
            raise ControlError("INVALID_ARGUMENT", "Upgrade timeout must be between 5 and 300 seconds")
        from codey.launcher import runner

        launcher = home / "launcher" / "runner.py"
        launcher.parent.mkdir(parents=True, exist_ok=True)
        # Per-operation copy: an update cannot overwrite an in-flight runner.
        launcher = launcher.with_name(identity + ".py")
        launcher.write_bytes(Path(runner.__file__).read_bytes())
        journal: _UpgradeJournal = {
            "upgrade_id": identity, "request_fingerprint": fingerprint(args), "phase": "accepted",
            "candidate": manifest, "previous": cast(ReleaseSnapshot | None, read_json_strict(home / "active-release.json")),
            "old_instance_id": service.instance_id, "deadline": time.time() + timeout,
            "allowed_roots": [str(root) for root in service.allowed_roots], "python": sys.executable,
            "admission_may_have_opened": False, "attempts": 0, "events": [],
        }
        write_json_atomic(path, dict(journal), mode=0o600)
        try:
            with (home / "launcher" / (identity + ".log")).open("ab") as output:
                subprocess.Popen([sys.executable, str(launcher), str(home), identity], cwd=home,
                                 stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                 creationflags=CREATE_NO_WINDOW)
        except OSError:
            record: dict[str, object] = dict(journal)
            runner.mark(home, record, "aborted", error="Release coordinator could not be started")
            return {"upgrade_id": identity, "phase": "aborted"}
        return {"upgrade_id": identity, "phase": "accepted"}


def release_command(service: _ActivationOwner, name: str, args: dict[str, object]) -> dict[str, object]:
    # The shared command catalogue validates the arguments before dispatch.
    if name == "prepare_release":
        return dict(prepare(service, cast(PrepareArgs, args)))
    if name == "activate_release":
        return dict(activate(service, cast(ActivateArgs, args)))
    return read_upgrade(service.ctx.state_home, cast(str, args["upgrade_id"]))


def read_upgrade(home: Path, identity: str) -> dict[str, object]:
    if not re.fullmatch(r"upgrade_[a-f0-9]{32}", identity):
        raise ControlError("INVALID_ARGUMENT", "Invalid upgrade identity")
    journal: dict[str, object] | None = read_json_strict(home / "upgrades" / (identity + ".json"))
    if not journal:
        raise ControlError("NOT_FOUND", "Upgrade not found")
    return {key: value for key, value in journal.items() if key not in {"python", "request_fingerprint"}}
