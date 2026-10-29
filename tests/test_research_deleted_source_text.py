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


@pytest.mark.parametrize("attribute", [
    'hidden', 'hidden="false"', 'aria-hidden="true"',
    'style="display: none"', 'style="color:red; visibility: hidden !important;"',
])
def test_hidden_navigation_and_nested_void_elements_do_not_become_source_text(attribute):
    html = (f'<div {attribute}><h2>Glossary</h2><div><img src="x"><br>'
            '<p>Unrelated hidden definition.</p></div></div>'
            '<article><h1>Trial</h1><p>Actual study population.</p></article>')
    text = extract_text(html)
    assert "Glossary" not in text and "Unrelated hidden" not in text
    assert "Trial" in text and "Actual study population." in text


def test_visible_content_and_non_hiding_styles_remain_readable():
    assert "Visible" in extract_text('<p aria-hidden="false" style="display: block">Visible</p>')
    assert "Visible" in extract_text('<p aria-hidden>Visible</p>')
