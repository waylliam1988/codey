from __future__ import annotations

import pytest

from codey.policies.redaction import (
    looks_high_entropy_secret,
    looks_prompt_visible_secret,
)


@pytest.mark.parametrize(
    "secret",
    [
        "api_key=sk-abcdefghijklmnop1234",
        "AWS_SECRET_ACCESS_KEY=jD2f9kQpXw7ZrNs4Tb8Vm1Ly6Hc0AgEu5Oi3SqXz",
        "AKIAIOSFODNN7EXAMPLE",
        "github_pat_AAAA0123456789bbbbbbbbbbbbCCCCCCCC",
        "sk" + "_live_" + "abcdefghijklmnop1234567890",
        "token Aa1Bb2Cc3Dd4Ee5Ff6Gg7Hh8Ii9Jj0Kk29 leaked",
    ],
)
def test_single_entry_blocks_markers_shapes_and_entropy(secret: str) -> None:
    assert looks_prompt_visible_secret(secret)


def test_single_entry_blocks_bare_random_blob_without_any_marker() -> None:
    # The entropy branch must fire on its own: callers never combine
    # predicates by hand, so marker-free random blobs stay covered.
    assert looks_prompt_visible_secret("Aa1Bb2Cc3Dd4Ee5Ff6Gg7Hh8Ii9Jj0Kk29")


@pytest.mark.parametrize(
    "text",
    [
        "refactor src/main/java/util/ArrayList.java",
        "C:/Users/alienware/.codey/state.json",
        "hebbian_node:ghn_a1b2c3d4e5f6a7b8c9d0e1f2",
        "digest sha256:" + "a" * 64,
        "secreted insulin secretion pathway",
        "read the page then finish",
        "OAuth2CallbackHandler",
        "HTTPRequest2Handler",
        "Windows10CompatibilityMode",
        "PyPI2026ReleasePlan",
    ],
)
def test_single_entry_keeps_ordinary_engineering_text_clean(text: str) -> None:
    assert not looks_prompt_visible_secret(text)


def test_entropy_keeps_camelcase_engineering_names_but_blocks_random_blobs() -> None:
    for text in (
        "OAuth2CallbackHandler",
        "HTTPRequest2Handler",
        "Windows10CompatibilityMode",
        "PyPI2026ReleasePlan",
    ):
        assert not looks_high_entropy_secret(text)

    assert looks_high_entropy_secret("Aa1Bb2Cc3Dd4Ee5Ff6Gg7Hh8Ii9Jj0Kk29")
    assert looks_high_entropy_secret("AbCdEfGhIjKlMnOp1")
    assert looks_high_entropy_secret("AbcdEfghIjkl1234X")


def test_entropy_predicate_agrees_with_the_single_entry() -> None:
    token = "Aa1Bb2Cc3Dd4Ee5Ff6Gg7Hh8Ii9Jj0Kk29"
    assert looks_high_entropy_secret(token)
    assert looks_prompt_visible_secret(token)
