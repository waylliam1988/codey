"""Split regression guard for _audit_search_files helpers (pure extraction).

Covers path admission, per-file budget/read, line matching, footers and
final output. No behavior change vs pre-split implementation.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import mock

from codey.agents import consensus


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
        start, error = consensus._audit_search_resolve_start(root, ".")
        assert error is None and start is not None
        _, error2 = consensus._audit_search_resolve_start(root, "nope.py")
        assert error2 is not None
        assert "path not found" in error2.model_text


def test_resolve_start_blocks_excluded_and_secret() -> None:
    with _make_root({"a.py": "x\n"}) as td:
        root = Path(td)
        _, err1 = consensus._audit_search_resolve_start(root, "node_modules/pkg")
        assert err1 is not None
        _, err2 = consensus._audit_search_resolve_start(root, ".env")
        assert err2 is not None


def test_scan_one_file_ok_oversized_byte_and_unreadable() -> None:
    with _make_root({"a.py": "hello\n"}) as td:
        root = Path(td)
        path = root / "a.py"
        text, new_br, over, byte_hit = consensus._audit_search_scan_one_file(path, 0)
        assert text == "hello\n" and new_br == path.stat().st_size
        assert not over and not byte_hit
        with mock.patch("codey.agents.consensus.SEARCH_MAX_FILE_BYTES", 2):
            t2, br2, over2, _ = consensus._audit_search_scan_one_file(path, 0)
            assert t2 is None and over2 and br2 == 0
        with mock.patch("codey.agents.consensus.SEARCH_MAX_SCAN_BYTES", 1):
            t3, br3, _, byte3 = consensus._audit_search_scan_one_file(path, 0)
            assert t3 is None and byte3 and br3 == 0
        missing = root / "gone.py"
        t4, br4, over4, byte4 = consensus._audit_search_scan_one_file(missing, 7)
        assert t4 is None and br4 == 7 and not over4 and not byte4


def test_scan_one_file_decode_failure_preserves_bytes_read() -> None:
    # Pre-split semantics: bytes_read += size before read_text, so a
    # UnicodeDecodeError still consumes budget. Helper must preserve it.
    with _make_root({"bad.py": b"marker \xff\xfe\n"}) as td:
        root = Path(td)
        bad = root / "bad.py"
        size = bad.stat().st_size
        text, new_br, over, byte_hit = consensus._audit_search_scan_one_file(bad, 100)
        assert text is None and not over and not byte_hit
        assert new_br == 100 + size


def test_collect_file_matches_case_and_truncation() -> None:
    with _make_root({}) as td:
        root = Path(td)
        path = root / "a.py"
        matches: list[str] = []
        limited = consensus._audit_search_collect_file_matches(
            path, root, "HeLLo world\nsecond\n", "hello", matches, 80
        )
        assert not limited and matches == ["a.py:1: HeLLo world"]
        long_line = "x" * 300
        m2: list[str] = []
        consensus._audit_search_collect_file_matches(
            path, root, long_line + "\n", "x", m2, 80
        )
        assert len(m2[0]) <= len("a.py:1: ") + 240
        assert m2[0].endswith("...")
        m3: list[str] = []
        limited3 = consensus._audit_search_collect_file_matches(
            path, root, "m\nm\nm\n", "m", m3, 2
        )
        assert limited3 and len(m3) == 2


def test_append_and_build_outcome_footers() -> None:
    budget = consensus._audit_scan_budget()
    matches: list[str] = []
    consensus._audit_search_append_limit_notes(
        matches,
        max_results=80,
        result_limited=False,
        oversized_files=0,
        byte_limited=False,
        budget=budget,
    )
    assert matches == ["(no literal matches; regex is not supported)"]
    out = consensus._audit_search_build_outcome(
        matches,
        result_limited=False,
        oversized_files=0,
        byte_limited=False,
        budget=budget,
    )
    assert not out.truncated


def test_search_files_end_to_end_basic_and_limits() -> None:
    with _make_root({"a.py": "marker one\n", "b.py": "nothing\n"}) as td:
        root = Path(td)
        out = consensus._audit_search_files(root, ".", "marker")
        assert "a.py:1: marker one" in out.model_text
        assert not out.truncated
        empty = consensus._audit_search_files(root, ".", "   ")
        assert not empty.ok
        limited = consensus._audit_search_files(root, ".", "marker", max_results=1)
        assert limited.truncated
        assert "truncated after 1 matches" in limited.model_text


def test_search_files_skips_secret_and_reports_budgets() -> None:
    with _make_root({".env": "SUPER_SECRET=1\n", "a.py": "safe\n"}) as td:
        root = Path(td)
        out = consensus._audit_search_files(root, ".", "SUPER_SECRET")
        assert "(no literal matches" in out.model_text
        assert "SUPER_SECRET=1" not in out.model_text
    with _make_root({"a.py": "marker\n", "b.py": "marker\n"}) as td:
        root = Path(td)
        with mock.patch("codey.agents.consensus.SEARCH_MAX_FILE_BYTES", 2):
            o2 = consensus._audit_search_files(root, ".", "marker")
            assert "oversized" in o2.model_text and o2.truncated
        with mock.patch("codey.agents.consensus.SEARCH_MAX_SCAN_BYTES", 1):
            o3 = consensus._audit_search_files(root, ".", "marker")
            assert "read budget" in o3.model_text and o3.truncated


def test_search_files_resolve_contract_never_raises() -> None:
    # Deterministic contract: _audit_search_resolve_start returns
    # (None, error) on failure and (Path, None) on success. Main must
    # never raise AssertionError/TypeError even if a future helper
    # returns (None, None); it must return a clean ToolOutcome.error.
    with _make_root({"a.py": "marker\n"}) as td:
        root = Path(td)
        with mock.patch.object(
            consensus, "_audit_search_resolve_start", return_value=(None, None)
        ):
            out = consensus._audit_search_files(root, ".", "marker")
            assert not out.ok
            assert "ERROR:" in out.model_text
