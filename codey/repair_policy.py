"""Strict allowlist, impact classification, and safety scan for repair candidates.

The repair surface is the full web adapter layer (see
:mod:`codey.adapter_surface`): one provider's driver plus the shared web
files every provider depends on. Changing more of that surface is not a
policy violation -- it widens the *impact*, and the repair runner answers
with stronger validation before installing:

- ``provider_local``: only this provider's driver changed;
- ``shared_web_surface``: shared wrapper / send plumbing / browser helpers;
- ``profile_data``: ``provider_profiles.json`` (data, so no snippet scan).

Codey core runtime and tests stay forbidden regardless of impact.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from codey.adapter_surface import (
    PROVIDER_DRIVER_FILES,
    SHARED_WEB_ADAPTER_FILES,
    adapter_repair_surface,
)
from codey.provider_ids import normalize_provider_id


IMPACT_PROVIDER_LOCAL = "provider_local"
IMPACT_SHARED_WEB_SURFACE = "shared_web_surface"
IMPACT_PROFILE_DATA = "profile_data"

PROVIDER_TEST_FILES = {
    "deepseek": ("tests/test_deepseek.py",),
    "qwen": ("tests/test_qwen.py",),
    "mimo": ("tests/test_mimo.py",),
    "stepfun": ("tests/test_stepfun.py",),
    "glm": ("tests/test_glm.py",),
}
FORBIDDEN_SNIPPETS = (
    "eval(",
    "exec(",
    "compile(",
    "__import__(",
    "subprocess",
    "os.system",
    "socket",
    "requests",
    "urllib",
    "http.client",
    ".write_text(",
    ".write_bytes(",
    "open(",
    "shutil.rmtree",
    "Remove-Item",
    "pip install",
)


@dataclass(frozen=True)
class RepairPolicyResult:
    ok: bool
    changed_files: tuple[str, ...]
    errors: tuple[str, ...] = ()
    impact: tuple[str, ...] = ()


def allowed_adapter_files(provider_id: str) -> tuple[str, ...]:
    return adapter_repair_surface(normalize_provider_id(provider_id))


def readonly_reference_files(provider_id: str) -> tuple[str, ...]:
    return PROVIDER_TEST_FILES.get(normalize_provider_id(provider_id), ())


def classify_impact(changed_files: list[str] | set[str] | tuple[str, ...]) -> tuple[str, ...]:
    """Map changed files onto the escalation levels the runner validates."""

    shared = set(SHARED_WEB_ADAPTER_FILES)
    drivers = {rel for files in PROVIDER_DRIVER_FILES.values() for rel in files}
    impact: set[str] = set()
    for rel in changed_files:
        normalized = rel.replace("\\", "/")
        if normalized == "codey/provider_profiles.json":
            impact.add(IMPACT_PROFILE_DATA)
        elif normalized in drivers:
            impact.add(IMPACT_PROVIDER_LOCAL)
        elif normalized in shared:
            impact.add(IMPACT_SHARED_WEB_SURFACE)
    ordered = (
        IMPACT_PROVIDER_LOCAL,
        IMPACT_SHARED_WEB_SURFACE,
        IMPACT_PROFILE_DATA,
    )
    return tuple(level for level in ordered if level in impact)


def validate_candidate(
    provider_id: str,
    baseline_root: str | Path,
    candidate_root: str | Path,
) -> RepairPolicyResult:
    provider_id = normalize_provider_id(provider_id)
    baseline_root = Path(baseline_root).resolve()
    candidate_root = Path(candidate_root).resolve()
    allowed = set(adapter_repair_surface(provider_id))
    readonly_tests = set(PROVIDER_TEST_FILES.get(provider_id, ()))
    changed = _changed_files(baseline_root, candidate_root)
    errors: list[str] = []
    for rel in sorted(changed):
        if rel in readonly_tests:
            errors.append(f"test file is read-only: {rel}")
            continue
        if rel not in allowed:
            errors.append(f"file is not allowed for adapter repair: {rel}")
            continue
        if not rel.endswith(".py"):
            continue
        before_text = _read_text(baseline_root / rel)
        after_text = _read_text(candidate_root / rel)
        for snippet in FORBIDDEN_SNIPPETS:
            if after_text.count(snippet) > before_text.count(snippet):
                errors.append(f"forbidden snippet in {rel}: {snippet}")
                break
    return RepairPolicyResult(
        ok=not errors,
        changed_files=tuple(sorted(changed)),
        errors=tuple(errors),
        impact=classify_impact(changed),
    )


def _changed_files(baseline_root: Path, candidate_root: Path) -> set[str]:
    baseline_files = _text_files(baseline_root)
    candidate_files = _text_files(candidate_root)
    changed: set[str] = set()
    for rel in baseline_files.union(candidate_files):
        before = _read_text(baseline_root / rel) if rel in baseline_files else None
        after = _read_text(candidate_root / rel) if rel in candidate_files else None
        if before != after:
            changed.add(rel)
    return changed


def _text_files(root: Path) -> set[str]:
    files: set[str] = set()
    for pattern in ("*.py", "*.json"):
        for path in root.rglob(pattern):
            if "__pycache__" in path.parts:
                continue
            try:
                files.add(path.relative_to(root).as_posix())
            except ValueError:
                continue
    return files


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
