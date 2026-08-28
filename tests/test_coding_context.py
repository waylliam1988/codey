from codey.workspace.coding_context import CodingContext, render_coding_context
from codey.completion.verification_policy import VerificationCandidate


def test_render_coding_context_omits_empty_block() -> None:
    assert render_coding_context(CodingContext()) == ""


def test_render_coding_context_lists_current_local_facts() -> None:
    prompt = render_coding_context(
        CodingContext(
            read_files=("app.py",),
            edit_eligible_files=("app.py", "tests/test_app.py"),
            changed_files=("app.py",),
            selected_verification=VerificationCandidate("python -m pytest", "."),
            verification_fresh=False,
        )
    )

    assert prompt.startswith("Coding current local context:")
    assert "- Files read this run: app.py" in prompt
    assert "- Existing files eligible for exact edit: app.py, tests/test_app.py" in prompt
    assert "- Changed files needing verification: app.py" in prompt
    assert '{"tool":"run","args":{"command":"python -m pytest","path":"."}}' in prompt
    assert "not yet passed after the latest edit" in prompt
    assert "not a fixed tool order" in prompt
    assert prompt.endswith("Reply with exactly one JSON object and no other text.")


def test_render_coding_context_marks_fresh_verification() -> None:
    prompt = render_coding_context(
        CodingContext(
            changed_files=("src/app.py",),
            selected_verification=VerificationCandidate("npm test", "frontend"),
            verification_fresh=True,
        )
    )

    assert "- Changed files covered by verification: src/app.py" in prompt
    assert "- Changed files needing verification" not in prompt
    assert "- Verification covering current changes: npm test (path: frontend)" in prompt
    assert "- Suggested verification for current changes:" not in prompt
    assert '{"tool":"run"' not in prompt
    assert "passed after the latest edit" in prompt
    assert "not yet passed" not in prompt


def test_render_coding_context_suppresses_suggested_check_when_verification_forbidden() -> None:
    prompt = render_coding_context(
        CodingContext(
            changed_files=("src/app.py",),
            selected_verification=VerificationCandidate("npm test", "frontend"),
            verification_fresh=False,
            verification_forbidden=True,
        )
    )

    assert "- Changed files not locally verified by request: src/app.py" in prompt
    assert "- Changed files needing verification" not in prompt
    assert "- Suggested verification for current changes:" not in prompt
    assert '{"tool":"run"' not in prompt
    assert "task forbids local checks" in prompt


def test_render_coding_context_sanitizes_and_truncates_paths() -> None:
    paths = tuple(f"src\\file_{index}.py\nnoise" for index in range(10))

    prompt = render_coding_context(CodingContext(read_files=paths))

    assert "\nnoise" not in prompt
    assert "src/file_0.py noise" in prompt
    assert "... (+2 more)" in prompt
