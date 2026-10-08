"""PEP source titles exclude SVG theme-switcher accessibility titles."""
import pytest

from codey.research.extract import extract_title


@pytest.mark.parametrize("label", ["Following system colour scheme", "Selected dark colour scheme", "Selected light colour scheme"])
def test_page_title_does_not_append_pep_theme_icon_title(label):
    html = (
        "<html><head><title>PEP 428 | peps.python.org</title></head><body>"
        f"<button><svg><title>{label}</title><path /></svg></button></body></html>"
    )
    assert extract_title(html) == "PEP 428 | peps.python.org"


def test_svg_title_without_document_title_cannot_become_source_title():
    assert extract_title("<html><body><svg><title>Icon label</title></svg></body></html>") == ""
