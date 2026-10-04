"""Review input identity and workspace snapshot for stale detection."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
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
    inventory_digest: str = ""

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
    snapshot_inventory_digest: str = ""


def scope_digest_for(scope: ReviewScope) -> str:
    fields = asdict(scope)
    for key in ("provided_files", "excluded_files", "exclusion_reasons"):
        fields[key] = sorted(fields[key])
    payload = json.dumps(fields, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def prompt_digest_for(prompt: str) -> str:
    return hashlib.sha256((prompt or "").encode("utf-8")).hexdigest()


def snapshot_digest_for(snapshot: ReviewSnapshot) -> str:
    payload = json.dumps([sorted(snapshot.files), snapshot.inventory_digest], separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def capture_snapshot(project_root: str | Path, provided_files: tuple[str, ...] | list[str]) -> ReviewSnapshot:
    try:
        root = Path(project_root).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return ReviewSnapshot(root=str(project_root), files=(), ok=False, reason="bad_root")
    if len(provided_files) > MAX_SNAPSHOT_FILES:
        return ReviewSnapshot(root=str(root), files=(), ok=False, reason="file_limit")
    inventory = _inventory_digest(root)
    if not inventory:
        return ReviewSnapshot(root=str(root), files=(), ok=False, reason="inventory_unavailable")
    try:
        files = [_snapshot_row(root, rel) for rel in provided_files]
    except (OSError, RuntimeError, ValueError):
        return ReviewSnapshot(root=str(root), files=(), ok=False, reason="snapshot_failed")
    return ReviewSnapshot(root=str(root), files=tuple(files), ok=True, inventory_digest=inventory)


def _snapshot_row(root: Path, rel: str) -> tuple[str, str]:
    """The sole bounded file identity reader for capture and later validation."""
    from codey.workspace.paths import safe_join

    if not isinstance(rel, str) or not rel.strip():
        raise ValueError("bad_path")
    path = safe_join(root, rel)
    if path.relative_to(root).as_posix() != rel:
        raise ValueError("non_canonical")
    if not path.exists():
        return rel, "missing"
    if not path.is_file():
        raise ValueError("not_file")
    with path.open("rb") as stream:
        data = stream.read(MAX_SNAPSHOT_FILE_BYTES + 1)
    if len(data) > MAX_SNAPSHOT_FILE_BYTES:
        raise ValueError("too_large")
    return rel, "sha256:" + hashlib.sha256(data).hexdigest()


def verify_snapshot(snapshot: ReviewSnapshot, root: str | Path | None = None) -> bool:
    if not isinstance(snapshot, ReviewSnapshot) or not snapshot.ok or not snapshot.inventory_digest:
        return False
    try:
        expected_root = Path(snapshot.root).expanduser().resolve()
        if root is not None and Path(root).expanduser().resolve() != expected_root:
            return False
        if _inventory_digest(expected_root) != snapshot.inventory_digest:
            return False
        return all(_snapshot_row(expected_root, rel) == (rel, digest) for rel, digest in snapshot.files)
    except (OSError, RuntimeError, ValueError):
        return False


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
        snapshot_inventory_digest=snapshot.inventory_digest if snapshot is not None else "",
    )


def _inventory_digest(root: Path) -> str:
    """Bounded path inventory detects additions without hashing the whole project."""
    from codey.workspace.bounded_scan import BoundedScanBudget, iter_bounded_files
    from codey.workspace.revision import FINGERPRINT_EXCLUDED_DIRS

    if not root.is_dir():
        return ""
    budget = BoundedScanBudget(max_files=5000, max_dirs=500, max_dir_entries=1000)
    try:
        names = sorted(path.relative_to(root).as_posix() for path in iter_bounded_files(
            root, excluded_dirs=set(FINGERPRINT_EXCLUDED_DIRS), budget=budget,
        ))
    except (OSError, ValueError):
        return ""
    if budget.limited or budget.read_failed:
        return ""
    git_basis = _git_basis(root)
    if git_basis is None:
        return ""
    return hashlib.sha256(json.dumps([names, git_basis], separators=(",", ":")).encode("utf-8")).hexdigest()


def _git_basis(root: Path) -> str | None:
    """Git HEAD and index affect the diff even when worktree bytes do not."""
    from codey.workspace.changes import _run_git

    if not any((parent / ".git").exists() for parent in (root, *root.parents)):
        return "not_git"
    hashes = []
    for args in (["rev-parse", "--verify", "HEAD"], ["diff", "--cached", "--raw", "--no-abbrev", "-z", "--"]):
        observed = _run_git(root, args)
        if observed.returncode != 0 or observed.stdout_truncated:
            return None
        hashes.append(hashlib.sha256(observed.stdout.encode("utf-8")).hexdigest())
    return ":".join(hashes)


def review_model_identity(provider: object) -> str:
    """Hash known local target/settings; a web provider name is not a model ID."""
    from codey.providers.local_openai import LocalOpenAIProvider

    if not isinstance(provider, LocalOpenAIProvider):
        return ""
    settings = {name: getattr(provider, name) for name in (
        "base_url", "model", "temperature", "system_prompt", "context_window_tokens",
        "context_reserve_tokens", "context_keep_recent_tokens",
    )}
    return hashlib.sha256(json.dumps(settings, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def identities_match(first: ReviewIdentity, second: ReviewIdentity) -> bool:
    if not isinstance(first, ReviewIdentity) or not isinstance(second, ReviewIdentity):
        return False
    if first.contract_version != second.contract_version:
        return False
    if first.policy != second.policy:
        return False
    if first.self_review != second.self_review:
        return False
    if first.project != second.project:
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
