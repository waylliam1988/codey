"""Strict allowlist and safety scan for adapter self-repair candidates."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


PROVIDER_ADAPTER_FILES = {
    "deepseek": ("codey/deepseek.py", "codey/providers/deepseek_web.py"),
    "qwen": ("codey/qwen.py", "codey/providers/qwen_web.py"),
    "mimo": ("codey/mimo.py", "codey/providers/mimo_web.py"),
    "stepfun": ("codey/stepfun.py", "codey/providers/stepfun_web.py"),
    "glm": ("codey/glm.py", "codey/providers/glm_web.py"),
}
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


def allowed_adapter_files(provider_id: str) -> tuple[str, ...]:
    return PROVIDER_ADAPTER_FILES.get(_provider_id(provider_id), ())


def readonly_reference_files(provider_id: str) -> tuple[str, ...]:
    return PROVIDER_TEST_FILES.get(_provider_id(provider_id), ())


def validate_candidate(
    provider_id: str,
    baseline_root: str | Path,
    candidate_root: str | Path,
) -> RepairPolicyResult:
    provider_id = _provider_id(provider_id)
    baseline_root = Path(baseline_root).resolve()
    candidate_root = Path(candidate_root).resolve()
    allowed = set(PROVIDER_ADAPTER_FILES.get(provider_id, ()))
    readonly_tests = set(PROVIDER_TEST_FILES.get(provider_id, ()))
    changed = _changed_files(baseline_root, candidate_root)
    errors: list[str] = []
    for rel in changed:
        normalized = rel.replace("\\", "/")
        if normalized in readonly_tests:
            errors.append(f"test file is read-only: {normalized}")
            continue
        if normalized not in allowed:
            errors.append(f"file is not allowed for adapter repair: {normalized}")
            continue
        before_text = _read_text(baseline_root / normalized)
        after_text = _read_text(candidate_root / normalized)
        for snippet in FORBIDDEN_SNIPPETS:
            if after_text.count(snippet) > before_text.count(snippet):
                errors.append(f"forbidden snippet in {normalized}: {snippet}")
                break
    return RepairPolicyResult(
        ok=not errors,
        changed_files=tuple(sorted(changed)),
        errors=tuple(errors),
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
    for path in root.rglob("*.py"):
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


def _provider_id(value: object) -> str:
    return str(value or "").strip().lower()
