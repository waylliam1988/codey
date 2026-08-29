"""Edit-scope classification for one run's changed paths (0.5.0).

Before any integrity judgment can be made, one local question has to be
answered: what kind of thing did this run touch? ``edit_scope`` owns that
vocabulary -- production, test, fixture, verification config, docs, and
generated/vendor paths -- so the completion layer classifies each changed
path once instead of every consumer re-guessing "is this a test".

It is a pure stdlib leaf like ``codey.utils.refs``: no I/O, no models, and
no imports from codey. The closed vocabularies below are auditable on
purpose; a path that matches none of them is production, because treating
an unknown path as production is the conservative direction for every
downstream integrity rule.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Iterable


EDIT_SCOPE_PRODUCTION = "production"
EDIT_SCOPE_TEST = "test"
EDIT_SCOPE_FIXTURE = "fixture"
EDIT_SCOPE_VERIFICATION_CONFIG = "verification_config"
EDIT_SCOPE_DOCS = "docs"
EDIT_SCOPE_GENERATED_VENDOR = "generated_vendor"
EDIT_SCOPES = frozenset(
    {
        EDIT_SCOPE_PRODUCTION,
        EDIT_SCOPE_TEST,
        EDIT_SCOPE_FIXTURE,
        EDIT_SCOPE_VERIFICATION_CONFIG,
        EDIT_SCOPE_DOCS,
        EDIT_SCOPE_GENERATED_VENDOR,
    }
)

# The closed scope vocabulary doubles as the return type of
# ``classify_edit_path``; a scope is always one of EDIT_SCOPES.
EditPathScope = str

MAX_CHANGED_PATHS = 64
MAX_PATH_CHARS = 240

_DOC_SUFFIXES = frozenset({".md", ".rst", ".txt"})

# Directories whose contents are build outputs, dependencies, or caches.
# Findings never fire on them: a lockfile bump or a regenerated client is
# not an edit-integrity signal.
_GENERATED_DIR_SEGMENTS = frozenset(
    {
        "__pycache__",
        ".next",
        ".venv",
        ".pytest_cache",
        ".mypy_cache",
        "build",
        "dist",
        "node_modules",
        "target",
        "vendor",
        "venv",
    }
)
_GENERATED_BASENAMES = frozenset(
    {
        "bun.lock",
        "bun.lockb",
        "cargo.lock",
        "go.sum",
        "package-lock.json",
        "pipfile.lock",
        "pnpm-lock.yaml",
        "poetry.lock",
        "uv.lock",
        "yarn.lock",
    }
)
_GENERATED_SUFFIXES = frozenset({".pyc", ".pyo", ".min.css", ".min.js", ".min.mjs"})

# Files whose edit can make verification easier to pass without touching
# product code. ``conftest.py`` is deliberately a fixture (a protected test
# helper), not config: its edits are judged by the fixture rules.
_VERIFICATION_CONFIG_BASENAMES = frozenset(
    {
        ".coveragerc",
        ".ruff.toml",
        "mypy.ini",
        "pyproject.toml",
        "pytest.ini",
        "ruff.toml",
        "setup.cfg",
        "tox.ini",
    }
)

_FIXTURE_DIR_SEGMENTS = frozenset(
    {
        "__snapshots__",
        "fixture",
        "fixtures",
        "golden",
        "testdata",
        "test_data",
    }
)
_FIXTURE_BASENAMES = frozenset({"conftest.py"})
_FIXTURE_BASENAME_RE = re.compile(r"_fixture", re.IGNORECASE)

_TEST_DIR_SEGMENTS = frozenset({"__tests__", "spec", "specs", "test", "tests"})
_TEST_BASENAME_RES = (
    re.compile(r"^test_.+", re.IGNORECASE),
    re.compile(r".+_test$", re.IGNORECASE),
    re.compile(r".+\.(test|spec)\.(js|jsx|mjs|cjs|ts|tsx|mts|cts)$", re.IGNORECASE),
)

# A user task that names an explicit edit verb pointed at tests authorizes
# test edits, so detected findings are recorded at low severity instead of
# reading as tampering. The match is deliberately conservative: an
# unlisted phrasing fails closed to "not authorized", which can only make
# the monitor louder, never quieter.
_TEST_EDIT_AUTHORIZATION_RES = (
    re.compile(
        r"\b(update|updating|modify|modifying|change|changing|fix|fixing|"
        r"adjust|adjusting|edit|editing|rewrite|rewriting)\b[^.\n]{0,30}\btests?\b",
        re.IGNORECASE,
    ),
    re.compile(r"\btests?\b\s+to\s+expect\b", re.IGNORECASE),
)
_TEST_EDIT_AUTHORIZATION_PHRASES_ZH = (
    "修改测试",
    "修改一下测试",
    "更新测试",
    "更新一下测试",
    "调整测试",
    "改测试",
    "改一下测试",
    "测试改成",
    "测试改一下",
)


def _posix(path: object) -> PurePosixPath:
    return PurePosixPath(str(path or "").replace("\\", "/").strip())


def is_generated_or_vendor_path(path: object) -> bool:
    """True for build outputs, dependency trees, lockfiles, and caches."""

    item = _posix(path)
    name = item.name.lower()
    if name in _GENERATED_BASENAMES:
        return True
    # ``.suffix`` only sees the last extension; minified bundles are named
    # by their full compound suffix (``.min.js``).
    joined_suffixes = "".join(item.suffixes).lower()
    if joined_suffixes in _GENERATED_SUFFIXES:
        return True
    if name.endswith(("_pb2.py", "_pb2_grpc.py")):
        return True
    return any(segment in _GENERATED_DIR_SEGMENTS for segment in item.parts[:-1])


def is_verification_config_path(path: object) -> bool:
    """True for files that carry verification tool configuration."""

    return _posix(path).name.lower() in _VERIFICATION_CONFIG_BASENAMES


def is_fixture_path(path: object) -> bool:
    """True for protected test helpers: conftest, fixtures, golden files."""

    item = _posix(path)
    if item.name.lower() in _FIXTURE_BASENAMES:
        return True
    if _FIXTURE_BASENAME_RE.search(item.name):
        return True
    return any(segment.lower() in _FIXTURE_DIR_SEGMENTS for segment in item.parts[:-1])


def is_test_path(path: object) -> bool:
    """True for test files: test directories and test-name conventions."""

    item = _posix(path)
    if any(segment.lower() in _TEST_DIR_SEGMENTS for segment in item.parts[:-1]):
        return True
    return any(pattern.fullmatch(item.name) for pattern in _TEST_BASENAME_RES)


def is_document_path(path: str) -> bool:
    """True for prose files whose change never needs code verification.

    This is the single shared definition of a document path: the completion
    proof's docs-only limitation and the edit-integrity scope projection
    must never disagree about what counts as prose.
    """

    item = _posix(path)
    name = item.name.upper()
    return (
        item.suffix.lower() in _DOC_SUFFIXES
        or name == "LICENSE"
        or name.startswith("CHANGELOG")
    )


def classify_edit_path(path: object, *, task: str = "") -> EditPathScope:
    """Project one changed path onto the closed edit-scope vocabulary."""

    del task  # Authorization is a task-level fact, not a per-path one.
    if is_generated_or_vendor_path(path):
        return EDIT_SCOPE_GENERATED_VENDOR
    if is_verification_config_path(path):
        return EDIT_SCOPE_VERIFICATION_CONFIG
    if is_fixture_path(path):
        return EDIT_SCOPE_FIXTURE
    if is_test_path(path):
        return EDIT_SCOPE_TEST
    if is_document_path(str(path or "")):
        return EDIT_SCOPE_DOCS
    return EDIT_SCOPE_PRODUCTION


def changed_paths_from_changes(changes: object) -> tuple[str, ...]:
    """Bounded, deduped changed-path tuple from a collected changes payload."""

    if not isinstance(changes, dict):
        return ()
    files = changes.get("files")
    if not isinstance(files, list):
        return ()
    paths: list[str] = []
    seen: set[str] = set()
    for item in files:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").strip()[:MAX_PATH_CHARS]
        if not path or path in seen:
            continue
        seen.add(path)
        paths.append(path)
        if len(paths) >= MAX_CHANGED_PATHS:
            break
    return tuple(paths)


def task_authorizes_test_edit(task: object) -> bool:
    """True when the user's task text explicitly asks for test edits.

    Closed, auditable phrasing only. There is no model call and no fuzzy
    matching: a task that does not clearly name editing tests leaves test
    edits unauthorized.
    """

    text = str(task or "").strip()
    if not text:
        return False
    folded = text.casefold()
    if any(pattern.search(folded) for pattern in _TEST_EDIT_AUTHORIZATION_RES):
        return True
    return any(phrase in text for phrase in _TEST_EDIT_AUTHORIZATION_PHRASES_ZH)


def scoped_paths(
    paths: Iterable[object],
    scope: EditPathScope,
    *,
    task: str = "",
) -> tuple[str, ...]:
    """The subset of paths classified into one scope, input order kept."""

    out: list[str] = []
    for path in paths or ():
        text = str(path or "").strip()
        if text and classify_edit_path(text, task=task) == scope and text not in out:
            out.append(text)
    return tuple(out)


__all__ = [
    "EDIT_SCOPES",
    "EDIT_SCOPE_DOCS",
    "EDIT_SCOPE_FIXTURE",
    "EDIT_SCOPE_GENERATED_VENDOR",
    "EDIT_SCOPE_PRODUCTION",
    "EDIT_SCOPE_TEST",
    "EDIT_SCOPE_VERIFICATION_CONFIG",
    "MAX_CHANGED_PATHS",
    "EditPathScope",
    "changed_paths_from_changes",
    "classify_edit_path",
    "is_document_path",
    "is_fixture_path",
    "is_generated_or_vendor_path",
    "is_test_path",
    "is_verification_config_path",
    "scoped_paths",
    "task_authorizes_test_edit",
]
