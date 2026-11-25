"""Fenced examples cannot activate source-bound copying."""

import pytest

from codey.operations.browser_copy import CopyTarget, copy_target


@pytest.mark.parametrize("fence", ["```", "~~~"])
def test_copy_target_ignores_fenced_examples(fence: str) -> None:
    request = "Copy the value from https://example.test/record into dispatch.json field carrier_label as a string."
    example = f"Explain this example.\n{fence}\n{request}\n{fence}"

    assert copy_target(example) is None
    assert copy_target(request) == CopyTarget("https://example.test/record", "dispatch.json", "carrier_label", "string")
