"""Durable first-write baselines, manifest integrity, and project writer leases."""

from __future__ import annotations

import contextlib
import hashlib
import shutil
from collections.abc import Callable
from pathlib import Path

from codey.storage.atomic_io import write_bytes_atomic
from codey.storage.file_lock import FileLease, LockTimeout, acquire_lease, with_file_lock
from codey.storage.local_store import (
    DEFAULT_STATE_HOME,
    StoreCorruption,
    delete_file,
    project_key,
    read_json_strict,
    write_json_atomic,
)
from codey.workspace.paths import read_text_bounded as _read_text_bounded
from codey.workspace.paths import safe_join as _safe_join

MAX_SNAPSHOT_FILE_BYTES = 512 * 1024
MAX_SNAPSHOT_FILES = 200
MAX_SNAPSHOT_TOTAL_BYTES = 32 * 1024 * 1024
MAX_SNAPSHOT_MANIFEST_BYTES = 4 * 1024 * 1024
SNAPSHOT_SCHEMA_VERSION = 1
SNAPSHOT_DIR_NAME = "recovery"
BASELINE_DIR_NAME = "baselines"


class ProjectWriteBusy(RuntimeError):
    """A second task tried to persist snapshots for a project already owned."""


def _manifest_files_or_raise(payload: dict[str, object], manifest_path: Path) -> dict[str, object]:
    """Return the top-level ``files`` index or raise ``StoreCorruption``.

    Per-entry dirt is skipped by callers; a missing/broken top-level index
    blocks writes so one edit never rebuilds an empty index over good bodies.
    The corrupt file stays in place for explicit repair.
    """
    if type(payload.get("schema_version")) is not int or payload.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        raise StoreCorruption(manifest_path, "manifest schema_version")
    raw_files = payload.get("files")
    if not isinstance(raw_files, dict):
        raise StoreCorruption(manifest_path, "manifest files not a dict")
    return raw_files


class SnapshotStore:
    """Persist bounded first-write baselines for one workspace/task scope.

    The store is two-layered so one edit no longer rewrites the whole
    baseline set:

    - ``recovery/baselines/<rel-digest>.txt`` holds one file's baseline body;
    - ``recovery/manifest.json`` is the small index (path -> baseline ref,
      after hash).

    ``capture_after`` therefore touches only the manifest, and a new
    baseline writes one bounded body file plus the manifest instead of
    re-serializing up to 64MB of JSON on every edit.
    """

    def __init__(self, state_home: str | Path = DEFAULT_STATE_HOME, *, task_id: str = "") -> None:
        self.state_home: Path = Path(state_home)
        self.task_id = task_id

    def _project_dir(self, root: str | Path) -> Path:
        return (
            self.state_home
            / "projects"
            / str(project_key(root))
            / str(SNAPSHOT_DIR_NAME)
        )

    def dir_for(self, root: str | Path) -> Path:
        from codey.storage.atomic_io import filesystem_path

        snapshot_dir = self._project_dir(root)
        return filesystem_path(snapshot_dir / "tasks" / hashlib.sha256(self.task_id.encode()).hexdigest()
                               if self.task_id else snapshot_dir)

    def path_for(self, root: str | Path) -> Path:
        return self.dir_for(root) / "manifest.json"

    def _lock_target(self, root: str | Path) -> Path:
        resolved_root = Path(root).expanduser().resolve()
        return self.dir_for(resolved_root)

    def _baseline_path(self, root: str | Path, rel: str) -> Path:
        digest = hashlib.sha256(rel.encode("utf-8")).hexdigest()[:32]
        return self.dir_for(root) / BASELINE_DIR_NAME / f"{digest}.txt"

    def load(self, root: str | Path) -> tuple[dict[str, str | None], dict[str, str]]:
        resolved_root = Path(root).expanduser().resolve()
        manifest_path = self.path_for(resolved_root)
        if not manifest_path.exists():
            return {}, {}
        with with_file_lock(self._lock_target(resolved_root)):
            # Missing -> empty. Present-but-unreadable or top-level-invalid
            # -> raise and keep the file in place for explicit repair.
            payload = read_json_strict(
                manifest_path,
                max_bytes=MAX_SNAPSHOT_MANIFEST_BYTES,
            )
            if payload is None:
                return {}, {}
            raw_files = _manifest_files_or_raise(payload, manifest_path)

            before: dict[str, str | None] = {}
            hashes: dict[str, str] = {}
            total = 0
            for rel, entry in raw_files.items():
                if len(before) >= MAX_SNAPSHOT_FILES or not isinstance(rel, str):
                    raise StoreCorruption(manifest_path, "baseline entry limit or path type")
                if not isinstance(entry, dict):
                    raise StoreCorruption(manifest_path, "baseline entry must be an object")
                if set(entry) - {"baseline", "after_hash"}:
                    raise StoreCorruption(manifest_path, "unknown baseline entry fields")
                if "baseline" not in entry:
                    raise StoreCorruption(manifest_path, "missing baseline reference")
                try:
                    path = _safe_join(resolved_root, rel)
                    canonical = path.relative_to(resolved_root).as_posix()
                except (ValueError, OSError) as exc:
                    raise StoreCorruption(manifest_path, "invalid baseline path") from exc
                if canonical != rel:
                    raise StoreCorruption(manifest_path, "noncanonical baseline path")
                content: str | None
                if entry.get("baseline") is None:
                    content = None
                else:
                    expected_body = self._baseline_path(resolved_root, rel).name
                    if entry.get("baseline") != expected_body:
                        raise StoreCorruption(manifest_path, "baseline reference mismatch")
                    try:
                        body = _read_text_bounded(
                            self._baseline_path(resolved_root, rel),
                            max_bytes=MAX_SNAPSHOT_FILE_BYTES,
                        )
                    except (OSError, UnicodeDecodeError, ValueError) as exc:
                        raise StoreCorruption(manifest_path, "baseline body unavailable") from exc
                    if len(body.encode("utf-8")) > MAX_SNAPSHOT_FILE_BYTES:
                        raise StoreCorruption(manifest_path, "baseline body exceeds budget")
                    content = body
                digest = entry.get("after_hash")
                if digest is not None and not (
                    isinstance(digest, str)
                    and (digest == "missing" or digest.startswith("sha256:"))
                ):
                    raise StoreCorruption(manifest_path, "invalid produced version")
                content_bytes = len((content or "").encode("utf-8"))
                if total + content_bytes > MAX_SNAPSHOT_TOTAL_BYTES:
                    raise StoreCorruption(manifest_path, "baseline total budget exceeded")
                total += content_bytes
                before[rel] = content
                if isinstance(digest, str):
                    hashes[rel] = digest
            return before, hashes

    def writer_lock_path(self, root: str | Path) -> Path:
        """Per-project persistent-writer ownership lock (held for a task)."""
        return self._project_dir(root) / ".writer.lock"

    def acquire_writer(self, root: str | Path, *, timeout_seconds: float = 0.0) -> FileLease:
        """Claim the single persistent writer for ``root`` or raise busy.

        Non-blocking by default so a second write task fails fast with
        ``ProjectWriteBusy`` instead of queueing behind the first.
        """
        self.dir_for(root).mkdir(parents=True, exist_ok=True)
        try:
            return acquire_lease(
                self.writer_lock_path(root), timeout_seconds=timeout_seconds
            )
        except LockTimeout as exc:
            raise ProjectWriteBusy(
                f"another task is writing this project: {root}"
            ) from exc

    def put_baseline(
        self,
        root: str | Path,
        rel: str,
        content: str | None,
    ) -> str | None:
        """First-write baseline; returns the persisted baseline.

        The on-disk entry always wins: a second tracker adopting a newer
        file read converges back to the first persisted value instead of
        forking memory. A damaged existing entry (bad schema, wrong body
        name, missing/unreadable body) raises and blocks further edits
        rather than forking or silently overwriting.
        """

        resolved_root = Path(root).expanduser().resolve()
        try:
            probe = _safe_join(resolved_root, rel)
            canonical = probe.relative_to(resolved_root).as_posix()
        except (ValueError, OSError) as exc:
            raise ValueError(f"unsafe snapshot path: {rel!r}") from exc
        if canonical != rel:
            raise ValueError(f"unsafe snapshot path: {rel!r}")
        if content is not None and len(content.encode("utf-8")) > MAX_SNAPSHOT_FILE_BYTES:
            raise ValueError(f"snapshot too large: {rel!r}")
        body_path = self._baseline_path(resolved_root, rel)
        manifest_path = self.path_for(resolved_root)

        with with_file_lock(self._lock_target(resolved_root)):
            payload = read_json_strict(manifest_path, max_bytes=MAX_SNAPSHOT_MANIFEST_BYTES)
            if payload is None:
                files: dict[str, object] = {}
            else:
                files = _manifest_files_or_raise(payload, manifest_path)
            if rel in files:
                persisted = self._persisted_baseline_locked(
                    resolved_root, rel, files[rel], body_path
                )
                return persisted
            if len(files) >= MAX_SNAPSHOT_FILES:
                raise ValueError("snapshot file limit reached")
            if content is not None:
                new_bytes = len(content.encode("utf-8"))
                if self._disk_total_bytes_locked(resolved_root, files) + new_bytes > MAX_SNAPSHOT_TOTAL_BYTES:
                    raise ValueError("snapshot size limit reached")
            written_body = False
            try:
                if content is None:
                    _remove_file(body_path)
                else:
                    _write_bytes_atomic(body_path, content.encode("utf-8"))
                    written_body = True
                files[rel] = {
                    "baseline": None if content is None else body_path.name,
                }
                write_json_atomic(
                    manifest_path,
                    {"schema_version": SNAPSHOT_SCHEMA_VERSION, "files": files},
                    max_bytes=MAX_SNAPSHOT_MANIFEST_BYTES,
                )
                return content
            except Exception:
                if written_body:
                    self._discard_unpublished_baseline_locked(manifest_path, rel, body_path)
                raise

    def _discard_unpublished_baseline_locked(self, manifest_path: Path, rel: str, body_path: Path) -> None:
        # Atomic replacement may succeed before directory fsync raises. Never
        # delete a referenced body, or guess when publication cannot be read.
        try:
            payload = read_json_strict(manifest_path, max_bytes=MAX_SNAPSHOT_MANIFEST_BYTES)
            files = _manifest_files_or_raise(payload, manifest_path) if payload is not None else dict[str, object]()
        except (OSError, ValueError):
            return
        if rel not in files:
            _remove_file(body_path)

    def require_baseline(self, root: str | Path, rel: str) -> str | None:
        """Read-only recovery basis; never writes.

        The manifest and the entry must both exist. Missing or damaged
        state raises ``StoreCorruption`` so a lost manifest can never be
        rebuilt from the current working file.
        """
        resolved_root = Path(root).expanduser().resolve()
        try:
            probe = _safe_join(resolved_root, rel)
            canonical = probe.relative_to(resolved_root).as_posix()
        except (ValueError, OSError) as exc:
            raise ValueError(f"unsafe snapshot path: {rel!r}") from exc
        if canonical != rel:
            raise ValueError(f"unsafe snapshot path: {rel!r}")
        body_path = self._baseline_path(resolved_root, rel)
        manifest_path = self.path_for(resolved_root)
        with with_file_lock(self._lock_target(resolved_root)):
            payload = read_json_strict(manifest_path, max_bytes=MAX_SNAPSHOT_MANIFEST_BYTES)
            if payload is None:
                raise StoreCorruption(manifest_path, f"baseline manifest missing: {rel!r}")
            files = _manifest_files_or_raise(payload, manifest_path)
            if rel not in files:
                raise StoreCorruption(manifest_path, f"baseline entry missing: {rel!r}")
            return self._persisted_baseline_locked(resolved_root, rel, files[rel], body_path)

    def _persisted_baseline_locked(
        self,
        resolved_root: Path,
        rel: str,
        existing: object,
        body_path: Path,
    ) -> str | None:
        """Validate an existing entry inside the manifest lock and read it."""
        if not isinstance(existing, dict):
            raise StoreCorruption(
                self.path_for(resolved_root), f"baseline entry not an object: {rel!r}"
            )
        if set(existing) - {"baseline", "after_hash"} or "baseline" not in existing:
            raise StoreCorruption(
                self.path_for(resolved_root), f"baseline entry schema: {rel!r}"
            )
        baseline_ref = existing.get("baseline")
        if baseline_ref is None:
            digest = existing.get("after_hash")
            if digest is not None and not (
                isinstance(digest, str)
                and (digest == "missing" or digest.startswith("sha256:"))
            ):
                raise StoreCorruption(
                    self.path_for(resolved_root), f"baseline after_hash: {rel!r}"
                )
            return None
        if not isinstance(baseline_ref, str) or baseline_ref != body_path.name:
            raise StoreCorruption(
                self.path_for(resolved_root), f"baseline body name: {rel!r}"
            )
        digest = existing.get("after_hash")
        if digest is not None and not (
            isinstance(digest, str)
            and (digest == "missing" or digest.startswith("sha256:"))
        ):
            raise StoreCorruption(
                self.path_for(resolved_root), f"baseline after_hash: {rel!r}"
            )
        try:
            body = _read_text_bounded(body_path, max_bytes=MAX_SNAPSHOT_FILE_BYTES)
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise StoreCorruption(
                self.path_for(resolved_root), f"baseline body unreadable: {rel!r}"
            ) from exc
        if len(body.encode("utf-8")) > MAX_SNAPSHOT_FILE_BYTES:
            raise StoreCorruption(
                self.path_for(resolved_root), f"baseline body too large: {rel!r}"
            )
        result: str | None = body
        return result

    def _disk_total_bytes_locked(self, resolved_root: Path, files: dict[str, object]) -> int:
        # Disk-view size guard from manifest refs + stat sizes only.
        # Missing, renamed, linked, or non-regular bodies are corruption:
        # never silently skipped, never re-decoded here.
        import stat as _stat

        total = 0
        for rel, entry in files.items():
            if not isinstance(entry, dict) or entry.get("baseline") is None:
                continue
            if not isinstance(rel, str):
                raise StoreCorruption(self.path_for(resolved_root), "baseline entry path")
            baseline_ref = entry.get("baseline")
            if not isinstance(baseline_ref, str):
                raise StoreCorruption(self.path_for(resolved_root), f"baseline body name: {rel!r}")
            if baseline_ref != self._baseline_path(resolved_root, rel).name:
                raise StoreCorruption(self.path_for(resolved_root), f"baseline body name: {rel!r}")
            body_path = self._baseline_path(resolved_root, rel)
            try:
                if body_path.is_symlink():
                    raise StoreCorruption(self.path_for(resolved_root), f"baseline body link: {rel!r}")
                file_stat = body_path.stat()
            except OSError as exc:
                raise StoreCorruption(
                    self.path_for(resolved_root), f"baseline body missing: {rel!r}"
                ) from exc
            if not _stat.S_ISREG(file_stat.st_mode):
                raise StoreCorruption(self.path_for(resolved_root), f"baseline body type: {rel!r}")
            total += file_stat.st_size
            if total > MAX_SNAPSHOT_TOTAL_BYTES:
                break
        return total

    def set_after_hash(self, root: str | Path, rel: str, digest: str) -> None:
        resolved_root = Path(root).expanduser().resolve()
        with with_file_lock(self._lock_target(resolved_root)):
            self._update_manifest_locked(
                resolved_root,
                rel,
                lambda entry: {**entry, "after_hash": digest},
            )

    def remove(self, root: str | Path, rel: str) -> None:
        """Drop one entry; manifest first, body last (failures preserve)."""

        resolved_root = Path(root).expanduser().resolve()
        snapshot_dir = self.dir_for(resolved_root)
        try:
            if not snapshot_dir.exists():
                return
        except OSError:
            return
        body_path = self._baseline_path(resolved_root, rel)
        manifest_path = self.path_for(resolved_root)

        with with_file_lock(self._lock_target(resolved_root)):
            payload = read_json_strict(manifest_path, max_bytes=MAX_SNAPSHOT_MANIFEST_BYTES)
            if payload is None:
                _remove_file(body_path)
                _remove_dir_if_empty(body_path.parent)
                return
            files = _manifest_files_or_raise(payload, manifest_path)
            if rel not in files:
                _remove_file(body_path)
                _remove_dir_if_empty(body_path.parent)
                return
            del files[rel]
            if files:
                write_json_atomic(
                    manifest_path,
                    {"schema_version": SNAPSHOT_SCHEMA_VERSION, "files": files},
                    max_bytes=MAX_SNAPSHOT_MANIFEST_BYTES,
                )
            else:
                delete_file(manifest_path)
                _remove_dir_if_empty(self.dir_for(resolved_root))
            _remove_file(body_path)
            _remove_dir_if_empty(body_path.parent)

    def delete(self, root: str | Path) -> None:
        resolved_root = Path(root).expanduser().resolve()
        snapshot_dir = self.dir_for(resolved_root)
        try:
            if not snapshot_dir.exists():
                return
        except OSError:
            return
        with with_file_lock(self._lock_target(resolved_root)):
            try:
                shutil.rmtree(snapshot_dir)
            except FileNotFoundError:
                return
            except OSError:
                return

    def _update_manifest_locked(
        self,
        resolved_root: Path,
        rel: str,
        mutate: Callable[[dict[str, object]], dict[str, object]],
    ) -> None:
        manifest_path = self.path_for(resolved_root)
        payload = read_json_strict(manifest_path, max_bytes=MAX_SNAPSHOT_MANIFEST_BYTES)
        if payload is None:
            raise StoreCorruption(manifest_path, "missing manifest for update")
        files = _manifest_files_or_raise(payload, manifest_path)
        if rel not in files:
            raise StoreCorruption(manifest_path, f"missing baseline entry: {rel!r}")
        entry = files.get(rel)
        if not isinstance(entry, dict) or "baseline" not in entry:
            raise StoreCorruption(manifest_path, f"baseline entry schema: {rel!r}")
        updated = mutate(dict(entry))
        if not isinstance(updated, dict) or "baseline" not in updated:
            raise StoreCorruption(manifest_path, f"baseline entry schema: {rel!r}")
        files[rel] = updated
        write_json_atomic(
            manifest_path,
            {"schema_version": SNAPSHOT_SCHEMA_VERSION, "files": files},
            max_bytes=MAX_SNAPSHOT_MANIFEST_BYTES,
        )


def _write_bytes_atomic(path: Path, data: bytes) -> None:
    write_bytes_atomic(path, data)


def _remove_file(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)


def _remove_dir_if_empty(directory: Path) -> None:
    with contextlib.suppress(OSError):
        directory.rmdir()
