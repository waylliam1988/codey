"""Official pathlib constructor examples are file paths, not source domains."""
import pytest

from codey.research.provenance import provenance_problem


@pytest.mark.parametrize("example", [
    "PosixPath('pathlib.py')", "PosixPath('setup.py')", "PosixPath('docs/conf.py')",
    "WindowsPath('setup.py')", 'Path("setup.py")', "PurePath('docs/conf.py')",
])
def test_documented_path_constructor_argument_is_not_a_source_domain(example):
    assert provenance_problem("Official example: " + example,
        opened_sources={"https://docs.python.org/3/library/pathlib.html"},
        search_result_urls=set()) is None


@pytest.mark.parametrize("source", [
    "setup.py", "`setup.py`", "https://setup.py/page", "Path('https://setup.py/page')",
])
def test_real_or_ambiguous_domain_mentions_still_need_opened_sources(source):
    assert provenance_problem("Source: " + source,
        opened_sources={"https://docs.python.org/3/library/pathlib.html"},
        search_result_urls=set()) is not None
