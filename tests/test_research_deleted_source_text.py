"""HTML revisions must survive extraction so old claims retain their identity."""

import pytest

from codey.research.extract import extract_text


@pytest.mark.parametrize("tag", ["s", "del", "strike"])
def test_deleted_guidance_is_marked_and_adjacent_version_exception_is_retained(tag):
    text = extract_text(f"<p><{tag}>Large transactions fail.</{tag}> Since version 3.11 this is fixed.</p>")
    assert "[deleted text: Large transactions fail.]" in text
    assert "Since version 3.11 this is fixed." in text


def test_hidden_deleted_text_does_not_create_visible_claims():
    assert extract_text("<script><s>obsolete</s></script><p>Current guidance.</p>") == "Current guidance."
