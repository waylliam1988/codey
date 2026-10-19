from __future__ import annotations

import pytest


def test_parse_codemode_args_rejects_recursive_and_oversized_program() -> None:
    from codey.codemode.protocol import parse_codemode_args

    with pytest.raises(ValueError, match="code"):
        parse_codemode_args({"code": ""})

    with pytest.raises(ValueError, match="language"):
        parse_codemode_args({"code": "return 1", "language": "python"})

    with pytest.raises(ValueError, match="recursive"):
        parse_codemode_args({"code": "await tools.codemode({code: 'x'})"})

    with pytest.raises(ValueError, match="source"):
        parse_codemode_args({"code": "x" * 100_001})


def test_catalog_is_frozen_and_never_advertises_codemode_itself() -> None:
    from codey.codemode.catalog import build_catalog_snapshot

    snapshot = build_catalog_snapshot(
        (
            {"name": "read_file", "description": "read", "parameters": {"type": "object"}},
            {"name": "codemode", "description": "recursive", "parameters": {"type": "object"}},
        )
    )

    assert snapshot.digest.startswith("sha256:")
    assert [entry.name for entry in snapshot.entries] == ["read_file"]
    with pytest.raises(AttributeError):
        snapshot.entries[0].name = "edit"  # type: ignore[misc]


def test_text_protocol_accepts_only_one_codemode_object() -> None:
    from codey.codemode.protocol import parse_codemode_text

    program = parse_codemode_text('{"tool":"codemode","args":{"code":"return 1"}}')
    assert program.code == "return 1"

    with pytest.raises(ValueError, match="exactly one"):
        parse_codemode_text('{"tool":"codemode","args":{"code":"return 1"}} trailing')
