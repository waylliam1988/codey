"""Bounded reference text must fit the caller's total character budget."""

import pytest

from codey.utils.refs import clip, identifier


@pytest.mark.parametrize("value", ["abcdef", "中文测试😀"])
@pytest.mark.parametrize("limit", [-1, 0, 1, 2, 3, 4, 8])
def test_reference_text_and_identifiers_fit_the_total_budget(value: str, limit: int) -> None:
    assert len(clip(value, limit)) <= max(0, limit)
    assert len(identifier(value, limit)) <= max(0, limit)


def test_reference_text_preserves_normalization_and_unclipped_content() -> None:
    assert clip("  abc\r\ndef\rghi  ", 16) == "abc\ndef\nghi"
    assert clip("", 1) == ""
    assert clip("x", 1) == "x"
    assert identifier("x", 1) == "x"
