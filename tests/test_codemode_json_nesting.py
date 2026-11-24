import pytest

from codey.codemode.json_edit import json_source_value, replace_json_fields


def _document(depth: int) -> str:
    return '{"theme":"old","nested":' + "[" * depth + "null" + "]" * depth + "}"


def test_json_source_rejects_nesting_beyond_the_decoder_limit() -> None:
    assert json_source_value(_document(32), "/theme") == "old"
    with pytest.raises(ValueError, match="JSON nesting"):
        json_source_value(_document(5_000), "/theme")


def test_json_edit_rejects_nesting_beyond_the_decoder_limit() -> None:
    assert replace_json_fields(_document(32), {"theme": "new"}) == _document(32).replace('"old"', '"new"')
    with pytest.raises(ValueError, match="JSON nesting"):
        replace_json_fields(_document(5_000), {"theme": "new"})
