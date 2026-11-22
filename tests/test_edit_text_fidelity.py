"""Exercise edit assistance through the production file and kernel boundaries."""
from __future__ import annotations

from pathlib import Path

import pytest

from codey.operations.kernel_execution import execute_turn
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.runtime.core.models import ToolCall
from codey.toolchain.runtime import EDIT_FAILURE_MAX_CHARS, EditBlock, edit_file
from codey.workspace.revision import WorkspaceRevisionStore


@pytest.mark.parametrize("prefix", [b"", b"\xef\xbb\xbf"])
@pytest.mark.parametrize("requested_ending", ["\n", "\r\n"])
def test_edit_preserves_mixed_endings_and_original_bom(
    tmp_path: Path, prefix: bytes, requested_ending: str,
) -> None:
    path = tmp_path / "settings.txt"
    original = prefix + b"head\r\nold\ncontext\r\nuntouched\nlast"
    path.write_bytes(original)
    old = "old\ncontext\n".replace("\n", requested_ending)
    new = "new\ncontext\n".replace("\n", requested_ending)

    result = edit_file(tmp_path, path.name, [EditBlock(old, new)])

    assert result.ok, result.model_text
    assert path.read_bytes() == original.replace(b"old", b"new")


def test_inserted_line_uses_local_ending_and_keeps_later_context(tmp_path: Path) -> None:
    path = tmp_path / "settings.txt"
    original = b"head\r\nvalue = old\ncontext\r\nuntouched\n"
    path.write_bytes(original)

    result = edit_file(tmp_path, path.name, [EditBlock("value = old", "value = new\nextra = 1")])

    assert result.ok, result.model_text
    assert path.read_bytes() == original.replace(b"value = old", b"value = new\nextra = 1")


def test_mixed_ending_batch_keeps_matched_context_line_endings(tmp_path: Path) -> None:
    path = tmp_path / "settings.txt"
    original = b"head\r\nold\nkeep\r\nlast\n"
    path.write_bytes(original)

    result = edit_file(tmp_path, path.name, [
        EditBlock("old\nkeep\n", "new\ninserted\nkeep\n"),
        EditBlock("last", "tail"),
    ])

    assert result.ok, result.model_text
    assert path.read_bytes() == b"head\r\nnew\ninserted\nkeep\r\ntail\n"


def test_ambiguous_feedback_displays_each_candidate_context(tmp_path: Path) -> None:
    path = tmp_path / "app.py"
    source = b"def first():\n    return old_value\n\ndef second():\n    return old_value\n"
    path.write_bytes(source)

    result = edit_file(tmp_path, path.name, [EditBlock("    return old_value", "    return new_value")])

    assert not result.ok and not result.changed
    assert "def first():" in result.model_text and "def second():" in result.model_text
    assert "Exact matches start at lines: 2, 5." in result.model_text
    assert "same replacement" in result.model_text
    assert len(result.model_text) <= EDIT_FAILURE_MAX_CHARS
    assert path.read_bytes() == source


@pytest.mark.parametrize("context_width", [79, 1000])
def test_ambiguous_contexts_remain_bounded_for_three_candidates(tmp_path: Path, context_width: int) -> None:
    path = tmp_path / "settings.txt"
    source = "\n".join(("x" * context_width + "\n") * 2 + "same" for _ in range(3))
    path.write_bytes(source.encode())

    result = edit_file(tmp_path, path.name, [EditBlock("same", "changed")])

    assert not result.ok
    assert len(result.model_text) <= EDIT_FAILURE_MAX_CHARS
    for line in (3, 6, 9):
        assert f"Context for match at line {line}:" in result.model_text
    if context_width > 80:
        assert "Line 1 omitted; use read_file offset=1 limit=1." in result.model_text
        assert "x" * context_width not in result.model_text
    assert path.read_bytes() == source.encode()


def test_contextual_retry_changes_only_target_and_preserves_mixed_endings(tmp_path: Path) -> None:
    path = tmp_path / "app.py"
    source = b"def first():\n    return 10\r\n\ndef second():\r\n    return 10\n"
    path.write_bytes(source)

    ambiguous = edit_file(tmp_path, path.name, [EditBlock("    return 10", "    return 30")])
    assert not ambiguous.ok and "def second():" in ambiguous.model_text
    assert path.read_bytes() == source

    result = edit_file(tmp_path, path.name, [EditBlock(
        "def second():\n    return 10", "def second():\n    return 30",
    )])

    assert result.ok, result.model_text
    assert path.read_bytes() == source.replace(b"def second():\r\n    return 10", b"def second():\r\n    return 30")


def test_new_text_presence_is_only_a_hint_and_never_a_success(tmp_path: Path) -> None:
    path = tmp_path / "app.py"
    new = "    return request_timeout_seconds + 30\n"
    source = ("def unrelated():\n" + new).encode()
    path.write_bytes(source)

    result = edit_file(tmp_path, path.name, [EditBlock("    return request_timeout_seconds + 10\n", new)])

    assert not result.ok and not result.changed
    assert "may already be applied" in result.model_text
    assert path.read_bytes() == source


@pytest.mark.parametrize("new", ["", " ", "return", "\n"])
def test_trivial_new_text_does_not_suggest_an_applied_edit(tmp_path: Path, new: str) -> None:
    path = tmp_path / "app.py"
    path.write_text("def current():\n    return 1\n", encoding="utf-8")

    result = edit_file(tmp_path, path.name, [EditBlock("missing_request_timeout", new)])

    assert not result.ok
    assert "may already be applied" not in result.model_text


def test_partial_batch_hint_uses_disk_content_not_unwritten_intermediate_text(tmp_path: Path) -> None:
    path = tmp_path / "settings.txt"
    source = b"first\nlast\n"
    path.write_bytes(source)
    intermediate = "already_inserted_but_not_committed"

    result = edit_file(tmp_path, path.name, [
        EditBlock("first", intermediate), EditBlock("missing_second", intermediate),
    ])

    assert not result.ok
    assert "No replacements were written" in result.model_text
    assert "may already be applied" not in result.model_text
    assert path.read_bytes() == source


@pytest.mark.parametrize("suffix", ["\n        ", "\n"])
def test_python_boundary_padding_or_missing_eof_newline_can_be_recovered(tmp_path: Path, suffix: str) -> None:
    path = tmp_path / "app.py"
    source = "def process():\n    return 10" + ("\n" if suffix.endswith(" ") else "")
    path.write_bytes(source.replace("\n", "\r\n").encode())

    result = edit_file(tmp_path, path.name, [EditBlock(
        "def process():\n    return 10" + suffix,
        "def process():\n    return 30" + suffix,
    )])

    assert result.ok, result.model_text
    assert path.read_bytes() == source.replace("return 10", "return 30").replace("\n", "\r\n").encode()
    assert "boundary" in result.model_text.lower()


@pytest.mark.parametrize("case", ["string", "duplicate", "non_python", "internal", "new_padding", "invalid"])
def test_boundary_recovery_refuses_uncertain_or_semantic_differences(tmp_path: Path, case: str) -> None:
    name = "app.txt" if case == "non_python" else "app.py"
    source = "def process():\n    return 10\n"
    old, new = source + "        ", source.replace("10", "30") + "        "
    if case == "string":
        source = 'message = """\n' + source + '"""\n'
    elif case == "duplicate":
        source += source
    elif case == "internal":
        old = old.replace("    return", "        return")
    elif case == "new_padding":
        new = new[:-8] + "    "
    elif case == "invalid":
        new = new.replace("return 30", "return (")
    path = tmp_path / name
    path.write_bytes(source.encode())

    result = edit_file(tmp_path, name, [EditBlock(old, new)])

    assert not result.ok, result.model_text
    assert path.read_bytes() == source.encode()


def test_failed_assisted_batch_never_writes_earlier_edits(tmp_path: Path) -> None:
    path = tmp_path / "app.py"
    source = b"def process():\r\n    return 10\n"
    path.write_bytes(source)

    result = edit_file(tmp_path, path.name, [
        EditBlock("def process():\n    return 10\n        ", "def process():\n    return 30\n        "),
        EditBlock("missing_second", "new_second"),
    ])

    assert not result.ok
    assert "Replacement 2 of 2 failed" in result.model_text
    assert path.read_bytes() == source


@pytest.mark.parametrize("final_newline", ["", "\n"])
def test_only_one_extra_final_newline_is_ignored_at_actual_eof(tmp_path: Path, final_newline: str) -> None:
    path = tmp_path / "app.py"
    source = "def process():\n    return 10" + final_newline
    path.write_bytes(source.encode())

    result = edit_file(tmp_path, path.name, [EditBlock(source + "\n", source.replace("10", "30") + "\n")])

    assert result.ok, result.model_text
    assert path.read_bytes() == source.replace("10", "30").encode()


def test_missing_newline_inside_file_is_not_repaired(tmp_path: Path) -> None:
    path = tmp_path / "app.py"
    source = b"value = 10; other = 20\n"
    path.write_bytes(source)

    result = edit_file(tmp_path, path.name, [EditBlock("value = 10\n", "value = 30\n")])

    assert not result.ok
    assert path.read_bytes() == source


def test_automatic_indentation_preserves_unrelated_mixed_endings(tmp_path: Path) -> None:
    path = tmp_path / "app.py"
    source = b"def process():\r\n    if True:\n        value = 10\r\n        return value\n\nother = 1\r\n"
    path.write_bytes(source)

    result = edit_file(tmp_path, path.name, [EditBlock(
        "    value = 10\n    return value\n", "    value = 30\n    return value\n",
    )])

    assert result.ok, result.model_text
    assert path.read_bytes() == source.replace(b"value = 10", b"value = 30")


def test_numbered_fragment_preserves_mixed_endings(tmp_path: Path) -> None:
    path = tmp_path / "app.txt"
    source = b"head\r\nvalue = 10\nkeep\r\nlast\n"
    path.write_bytes(source)

    result = edit_file(tmp_path, path.name, [EditBlock("2 | value = 10\n3 | keep", "2 | value = 30\n3 | keep")])

    assert result.ok, result.model_text
    assert path.read_bytes() == source.replace(b"value = 10", b"value = 30")


@pytest.mark.parametrize("external", [b"old\r\nexternal\n", b"old\nkeep\n"])
def test_exact_edit_does_not_overwrite_a_concurrent_content_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, external: bytes,
) -> None:
    from codey.toolchain import runtime

    path = tmp_path / "app.txt"
    path.write_bytes(b"old\r\nkeep\n")
    original_write = runtime._write_text_after_final_path_check

    def concurrent_write(root: Path, rel: str, content: str, *, tool: str,
                         expected_version: str | None = None) -> runtime.ToolOutcome | None:
        path.write_bytes(external)
        return original_write(root, rel, content, tool=tool, expected_version=expected_version)

    monkeypatch.setattr(runtime, "_write_text_after_final_path_check", concurrent_write)

    result = edit_file(tmp_path, path.name, [EditBlock("old", "new")])

    assert not result.ok and result.error_code == "stale_read_reference"
    assert path.read_bytes() == external


def test_edit_line_end_space_assistance_preserves_untouched_bytes(tmp_path: Path) -> None:
    path = tmp_path / "settings.txt"
    path.write_bytes(b"head\r\nvalue = old \t\ncontext\r\nlast")

    result = edit_file(tmp_path, path.name, [EditBlock("value = old\ncontext", "value = new\ncontext")])

    assert result.ok, result.model_text
    assert result.audit["line_end_space_adjusted"] == 1
    assert path.read_bytes() == b"head\r\nvalue = new\ncontext\r\nlast"


def test_edit_line_end_space_ambiguity_lists_real_lines_and_writes_nothing(tmp_path: Path) -> None:
    path = tmp_path / "settings.txt"
    original = b"value = old \r\ncontext\nseparator\nvalue = old\t\ncontext"
    path.write_bytes(original)

    result = edit_file(tmp_path, path.name, [EditBlock("value = old\ncontext", "value = new\ncontext")])

    assert not result.ok
    assert "Matches after ignoring line-end space start at lines: 1, 4." in result.model_text
    assert path.read_bytes() == original


def test_hint_remains_a_failed_kernel_receipt(tmp_path: Path) -> None:
    path = tmp_path / "app.py"
    path.write_text("def process():\n    return request_timeout_seconds + 30\n", encoding="utf-8")
    session = TaskSession(TaskPolicy(frozenset({"project.read", "project.write"})))
    versions = WorkspaceRevisionStore(tmp_path.parent / (tmp_path.name + "-state"))
    call = ToolCall("edit", {"path": path.name, "replacements": [{
        "old_string": "    return request_timeout_seconds + 10\n",
        "new_string": "    return request_timeout_seconds + 30\n",
    }]})

    read = execute_turn(session, [ToolCall("read_file", {"path": path.name})], run_id="edit-hints", turn=0,
                        project_path=tmp_path, workspace_revision_store=versions)[0]
    assert read.ok

    result = execute_turn(session, [call], run_id="edit-hints", turn=1, project_path=tmp_path,
                          workspace_revision_store=versions)[0]

    assert not result.ok
    assert "may already be applied" in result.model_text
    assert result.audit.get("changed") is not True
    assert result.call == call


def test_overlapping_matches_are_ambiguous_and_leave_bytes_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "settings.txt"
    path.write_bytes(b"ababa\n")

    result = edit_file(tmp_path, path.name, [EditBlock("aba", "changed")])

    assert not result.ok
    assert "matched 2 times" in result.model_text
    assert path.read_bytes() == b"ababa\n"
