import pytest

from codey.completion.edit_scope import EDIT_SCOPE_PRODUCTION, EDIT_SCOPE_TEST, classify_edit_path


@pytest.mark.parametrize("path", ["src/parser_test.py", "src/test_parser.py"])
def test_python_test_filename_conventions_apply_outside_test_directories(path: str) -> None:
    assert classify_edit_path(path) == EDIT_SCOPE_TEST


def test_ordinary_python_source_is_not_a_test() -> None:
    assert classify_edit_path("src/parser.py") == EDIT_SCOPE_PRODUCTION
