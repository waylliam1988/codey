"""Audit admission and shared rg search retain their separate responsibilities.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import mock

from codey.agents import project_audit_tools as audit_tools


def _make_root(files: dict[str, str | bytes]) -> tempfile.TemporaryDirectory[str]:
    td = tempfile.TemporaryDirectory()
    root = Path(td.name)
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content, encoding="utf-8")
    return td


def test_resolve_start_ok_and_missing() -> None:
    with _make_root({"a.py": "x\n"}) as td:
        root = Path(td)
        start, error = audit_tools._audit_search_resolve_start(root, ".")
        assert error is None and start is not None
        _, error2 = audit_tools._audit_search_resolve_start(root, "nope.py")
        assert error2 is not None
        assert "path not found" in error2.model_text


def test_resolve_start_blocks_excluded_and_secret() -> None:
    with _make_root({"a.py": "x\n"}) as td:
        root = Path(td)
        _, err1 = audit_tools._audit_search_resolve_start(root, "node_modules/pkg")
        assert err1 is not None
        _, err2 = audit_tools._audit_search_resolve_start(root, ".env")
        assert err2 is not None


def test_audit_and_regular_search_use_the_same_backend() -> None:
    from codey.toolchain import rg_search

    with _make_root({"a.py": "HeLLo world\n"}) as td:
        root = Path(td)
        with mock.patch.object(rg_search, "search", wraps=rg_search.search) as shared:
            result = audit_tools._audit_search_files(root, ".", "hello")
        assert result.ok and "a.py:1: HeLLo world" in result.model_text
        assert shared.call_count == 1
        assert result.canonical["value"]["matches"][0]["text"] == "HeLLo world"


def test_search_files_end_to_end_basic_and_limits() -> None:
    with _make_root({"a.py": "marker one\n", "b.py": "nothing\n"}) as td:
        root = Path(td)
        out = audit_tools._audit_search_files(root, ".", "marker")
        assert "a.py:1: marker one" in out.model_text
        assert not out.truncated
        empty = audit_tools._audit_search_files(root, ".", "   ")
        assert not empty.ok
        limited = audit_tools._audit_search_files(root, ".", "marker", max_results=1)
        assert not limited.truncated  # Exactly one match is a complete page.
        assert not limited.canonical["value"]["has_more"]


def test_search_files_skips_secret_and_reports_budgets() -> None:
    with _make_root({".env": "SUPER_SECRET=1\n", "a.py": "safe\n"}) as td:
        root = Path(td)
        out = audit_tools._audit_search_files(root, ".", "SUPER_SECRET")
        assert "(no matches" in out.model_text
        assert "SUPER_SECRET=1" not in out.model_text
    with _make_root({"a.py": "marker\n", "b.py": "marker\n"}) as td:
        root = Path(td)
        with mock.patch("codey.agents.project_audit_tools.SEARCH_MAX_FILE_BYTES", 2):
            o2 = audit_tools._audit_search_files(root, ".", "marker")
            assert "larger than" in o2.model_text and o2.truncated
        with mock.patch("codey.agents.project_audit_tools.SEARCH_MAX_SCAN_BYTES", 1):
            o3 = audit_tools._audit_search_files(root, ".", "marker")
            assert "read budget" in o3.model_text and o3.truncated


def test_search_files_resolve_contract_never_raises() -> None:
    # Deterministic contract: _audit_search_resolve_start returns
    # (None, error) on failure and (Path, None) on success. Main must
    # never raise AssertionError/TypeError even if a future helper
    # returns (None, None); it must return a clean ToolOutcome.error.
    with _make_root({"a.py": "marker\n"}) as td:
        root = Path(td)
        with mock.patch.object(
            audit_tools, "_audit_search_resolve_start", return_value=(None, None)
        ):
            out = audit_tools._audit_search_files(root, ".", "marker")
            assert not out.ok
            assert "ERROR:" in out.model_text
