"""Line-ending normalization is formatting, not sensitive content removal."""
import pytest

from codey.reviews.input import prepare_review_input


@pytest.mark.parametrize("newline", ["\r\n", "\r"])
def test_windows_or_legacy_line_endings_do_not_make_review_partial(newline):
    prepared = prepare_review_input(
        project="project", task="Review changes", writer_summary="Implemented addition.",
        changes={"ok": True, "files": [{"path": "math_utils.py"}], "diff": ""},
        recent_log=newline.join(("Read math_utils.py", "Edited math_utils.py", "Tests passed")),
    )
    assert prepared.scope.content_redacted is False
    assert prepared.scope.is_complete
    assert "Tests passed" in prepared.prompt
