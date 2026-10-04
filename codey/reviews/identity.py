"""Review input identity and workspace snapshot for stale detection."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from codey.reviews.core import REVIEW_CONTRACT_VERSION
from codey.reviews.input import ReviewInput, ReviewScope

MAX_SNAPSHOT_FILES = 32
MAX_SNAPSHOT_FILE_BYTES = 512 * 1024


@dataclass(frozen=True)
class ReviewSnapshot:
    root: str
    files: tuple[tuple[str, str], ...]
    ok: bool = True
    reason: str = ""

    def is_current(self, root: str | Path | None = None) -> bool:
        return verify_snapshot(self, root=root)


@dataclass(frozen=True)
class ReviewIdentity:
    scope_digest: str
    prompt_digest: str
    snapshot_digest: str
    project: str
    reviewer_id: str
    model_id: str
    contract_version: int
    policy: str
    self_review: bool
    snapshot_root: str = ""
    snapshot_files: tuple[tuple[str, str], ...] = ()
    attempt_id: str = ""
    artifact_sha256: str = ""


def scope_digest_for(scope: ReviewScope) -> str:
    payload = "|".join([
        str(scope.total_changed_files),
        ",".join(sorted(scope.provided_files)),
        ",".join(sorted(scope.excluded_files)),
        "1" if scope.diff_truncated else "0",
        "1" if scope.file_list_truncated else "0",
        "1" if scope.collection_incomplete else "0",
        ",".join(sorted(scope.exclusion_reasons)),
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def prompt_digest_for(prompt: str) -> str:
    return hashlib.sha256((prompt or "").encode("utf-8")).hexdigest()


def snapshot_digest_for(snapshot: ReviewSnapshot) -> str:
    payload = "|".join([f"{path}={digest}" for path, digest in sorted(snapshot.files)])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def capture_snapshot(project_root: str | Path, provided_files: tuple[str, ...] | list[str]) -> ReviewSnapshot:
    try:
        root = Path(project_root).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return ReviewSnapshot(root=str(project_root), files=(), ok=False, reason="bad_root")
    files: list[tuple[str, str]] = []
    for rel in list(provided_files)[:MAX_SNAPSHOT_FILES]:
        if not isinstance(rel, str) or not rel.strip():
            return ReviewSnapshot(root=str(root), files=(), ok=False, reason="bad_path")
        try:
            from codey.workspace.paths import safe_join as _safe_join

            path = _safe_join(root, rel)
            canonical = path.relative_to(root).as_posix()
        except (ValueError, OSError):
            return ReviewSnapshot(root=str(root), files=(), ok=False, reason="unsafe_path")
        if canonical != rel:
            return ReviewSnapshot(root=str(root), files=(), ok=False, reason="non_canonical")
        try:
            if path.is_symlink():
                digest = "symlink"
            elif not path.exists():
                digest = "missing"
            elif not path.is_file():
                return ReviewSnapshot(root=str(root), files=(), ok=False, reason="not_file")
            else:
                try:
                    size = path.stat().st_size
                except OSError:
                    return ReviewSnapshot(root=str(root), files=(), ok=False, reason="stat_failed")
                if size > MAX_SNAPSHOT_FILE_BYTES:
                    return ReviewSnapshot(root=str(root), files=(), ok=False, reason="too_large")
                try:
                    data = path.read_bytes()
                except OSError:
                    return ReviewSnapshot(root=str(root), files=(), ok=False, reason="read_failed")
                digest = "sha256:" + hashlib.sha256(data).hexdigest()
        except OSError:
            return ReviewSnapshot(root=str(root), files=(), ok=False, reason="snapshot_failed")
        files.append((canonical, digest))
    return ReviewSnapshot(root=str(root), files=tuple(files), ok=True)


def verify_snapshot(snapshot: ReviewSnapshot, root: str | Path | None = None) -> bool:
    if not isinstance(snapshot, ReviewSnapshot) or not snapshot.ok:
        return False
    try:
        expected_root = Path(snapshot.root).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    if root is not None:
        try:
            given = Path(root).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            return False
        if given != expected_root:
            return False
    for rel, digest in snapshot.files:
        try:
            from codey.workspace.paths import safe_join as _safe_join

            path = _safe_join(expected_root, rel)
            if path.relative_to(expected_root).as_posix() != rel:
                return False
        except (ValueError, OSError):
            return False
        try:
            if path.is_symlink():
                current = "symlink"
            elif not path.exists():
                current = "missing"
            elif not path.is_file():
                return False
            else:
                try:
                    if path.stat().st_size > MAX_SNAPSHOT_FILE_BYTES:
                        return False
                    current = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
                except OSError:
                    return False
        except OSError:
            return False
        if current != digest:
            return False
    return True


def build_identity(
    review_input: ReviewInput,
    *,
    reviewer_id: str,
    policy: str,
    model_id: str = "",
    self_review: bool = False,
    project: str = "",
    snapshot: ReviewSnapshot | None = None,
    attempt_id: str = "",
    artifact_sha256: str = "",
) -> ReviewIdentity:
    scope_digest = scope_digest_for(review_input.scope)
    prompt_digest = prompt_digest_for(review_input.prompt)
    if snapshot is None:
        snapshot_digest = ""
        snapshot_root = ""
        snapshot_files: tuple[tuple[str, str], ...] = ()
    else:
        snapshot_digest = snapshot_digest_for(snapshot) if snapshot.ok else ""
        snapshot_root = snapshot.root
        snapshot_files = snapshot.files
    return ReviewIdentity(
        scope_digest=scope_digest,
        prompt_digest=prompt_digest,
        snapshot_digest=snapshot_digest,
        project=project or "",
        reviewer_id=str(reviewer_id or ""),
        model_id=str(model_id or ""),
        contract_version=REVIEW_CONTRACT_VERSION,
        policy=str(policy or ""),
        self_review=bool(self_review),
        snapshot_root=snapshot_root,
        snapshot_files=snapshot_files,
        attempt_id=str(attempt_id or ""),
        artifact_sha256=str(artifact_sha256 or ""),
    )


def identities_match(first: ReviewIdentity, second: ReviewIdentity) -> bool:
    if not isinstance(first, ReviewIdentity) or not isinstance(second, ReviewIdentity):
        return False
    if first.contract_version != second.contract_version:
        return False
    if first.policy != second.policy:
        return False
    if first.self_review != second.self_review:
        return False
    if (first.reviewer_id or "") != (second.reviewer_id or ""):
        return False
    if first.scope_digest != second.scope_digest:
        return False
    if first.prompt_digest != second.prompt_digest:
        return False
    if first.snapshot_digest != second.snapshot_digest:
        return False
    if not first.model_id or not second.model_id:
        return False
    return first.model_id == second.model_id
