from __future__ import annotations

from pathlib import Path

import pytest

from codey.operations.kernel_execution import execute_turn
from codey.operations.kernel_protocol import build_turn_snapshot
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.runtime.core.models import ToolCall
from codey.toolchain.runtime import EditBlock, RunCommandRawResult, edit_file, project_run_command_result
from codey.types.json_value import decode_json_object
from codey.utils.failure_output import failure_excerpt
from codey.workspace.revision import WorkspaceRevisionStore

SOURCE = ("class Store:\n    def order(self, stock, quantity):\n        with self.connect():\n"
          "            if stock is None or stock['units'] < quantity:\n"
          "                raise ValueError('insufficient stock')\n"
          "            return quantity\n")
OLD = "    if stock is None or stock['units'] < quantity:\n        raise ValueError('insufficient stock')\n"
NEW = "    if quantity <= 0:\n        raise ValueError('invalid quantity')\n"


@pytest.mark.parametrize("shift", [-8, 8])
@pytest.mark.parametrize("newline", [b"\n", b"\r\n"])
def test_uniform_outer_indent_is_automatic_with_original_replacements(
    tmp_path: Path, shift: int, newline: bytes,
) -> None:
    path = tmp_path / "checkout.py"
    path.write_bytes(SOURCE.encode().replace(b"\n", newline))
    old, new = OLD, NEW
    if shift < 0:
        old = "".join(" " * 16 + line for line in OLD.splitlines(keepends=True))
        new = "".join(" " * 16 + line for line in NEW.splitlines(keepends=True))
    result = edit_file(tmp_path, "checkout.py", [EditBlock(old, new)])
    assert result.ok, result.model_text
    expected = SOURCE.replace(" " * 8 + OLD.splitlines(keepends=True)[0]
                              + " " * 8 + OLD.splitlines(keepends=True)[1],
                              " " * 8 + NEW.splitlines(keepends=True)[0]
                              + " " * 8 + NEW.splitlines(keepends=True)[1])
    assert path.read_bytes() == expected.encode().replace(b"\n", newline)
    assert result.audit["indentation_adjusted"] == 1


@pytest.mark.parametrize("case", ["missing_line", "wrong_identifier", "internal_indent", "duplicate",
                                  "old_string_literal", "new_string_literal", "tabs", "invalid_syntax",
                                  "non_python", "partial_last_line", "new_outer_indent"])
def test_unproven_or_unsafe_indent_recovery_leaves_file_untouched(tmp_path: Path, case: str) -> None:
    source, old, new, name = SOURCE, OLD, NEW, "checkout.py"
    if case == "missing_line":
        old = old.replace("        raise", "    missing = True\n        raise")
    elif case == "wrong_identifier":
        old = old.replace("quantity", "other_quantity")
    elif case == "internal_indent":
        old = old.replace("        raise", "            raise")
    elif case == "duplicate":
        source += SOURCE.replace("Store", "Other")
    elif case == "old_string_literal":
        source = 'message = """\n' + " " * 8 + OLD.splitlines(keepends=True)[0] + " " * 8 + OLD.splitlines(keepends=True)[1] + '"""\n'
    elif case == "new_string_literal":
        new = '    message = """first\n    significant spaces\n    end"""\n'
    elif case == "tabs":
        new = NEW.replace("    ", "\t")
    elif case == "invalid_syntax":
        new = NEW.replace("quantity <= 0:", "quantity <= 0")
    elif case == "non_python":
        name = "checkout.txt"
    elif case == "partial_last_line":
        old = OLD.rstrip("\n")[:-2]
    elif case == "new_outer_indent":
        new = "\n".join(line[4:] for line in NEW.split("\n"))
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    before = path.read_bytes()
    result = edit_file(tmp_path, name, [EditBlock(old, new)])
    assert not result.ok, result.model_text
    assert path.read_bytes() == before


def test_failed_second_replacement_does_not_write_first_recovered_edit(tmp_path: Path) -> None:
    path = tmp_path / "checkout.py"
    path.write_text(SOURCE, encoding="utf-8")
    result = edit_file(tmp_path, "checkout.py", [EditBlock(OLD, NEW), EditBlock("missing", "new")])
    assert not result.ok and path.read_text() == SOURCE


def test_exact_change_later_in_recovered_batch_cannot_bypass_final_syntax_check(tmp_path: Path) -> None:
    path = tmp_path / "checkout.py"
    path.write_text(SOURCE, encoding="utf-8")
    result = edit_file(tmp_path, "checkout.py", [EditBlock(OLD, NEW), EditBlock("return quantity", "return (")])
    assert not result.ok and "no replacements written" in result.model_text
    assert path.read_text() == SOURCE


def test_exact_edit_still_allows_intended_indent_changes(tmp_path: Path) -> None:
    path = tmp_path / "a.py"
    source = "if True:\n    value = 1\n    other = 2\n"
    path.write_text(source, encoding="utf-8")
    result = edit_file(tmp_path, "a.py", [EditBlock("    value = 1\n    other = 2\n", "value = 1\nother = 2\n")])
    assert result.ok and "indentation_adjusted" not in result.audit
    assert path.read_text() == "if True:\nvalue = 1\nother = 2\n"


def test_original_kernel_call_and_schema_need_no_extra_fields(tmp_path: Path) -> None:
    (tmp_path / "checkout.py").write_text(SOURCE, encoding="utf-8")
    session = TaskSession(TaskPolicy(frozenset({"project.read", "project.write"})))
    snapshot = build_turn_snapshot(session, native=True)
    edit = next(spec for spec in snapshot.frozen_specs if spec.name == "edit")
    assert {name for name, _ in edit.parameters} == {"path", "content", "replacements", "base_result_ref"}
    revisions = WorkspaceRevisionStore(tmp_path.parent / (tmp_path.name + "-state"))
    execute_turn(session, [ToolCall("read_file", {"path": "checkout.py"})], run_id="assistance", turn=0,
                 project_path=tmp_path, workspace_revision_store=revisions)
    call = ToolCall("edit", {"path": "checkout.py", "replacements": [{"old_string": OLD, "new_string": NEW}]})
    result = execute_turn(session, [call], run_id="assistance", turn=1, project_path=tmp_path,
                          workspace_revision_store=revisions)[0]
    assert result.ok, result.model_text
    assert result.call == call and set(call.args) == {"path", "replacements"}
    assert result.audit["workspace_revision"] == session.workspace_revision


def test_test_failure_highlights_are_visible_before_large_output_without_changing_facts(tmp_path: Path) -> None:
    output = ("setup output\n" * 5000 + "E       assert 16 == 18\n"
              "FAILED test_checkout.py::test_retry - assert 16 == 18\n"
              "FAILED test_checkout.py::test_bool - Failed: DID NOT RAISE\n"
              "2 failed, 1 passed in 0.12s\n")
    raw = RunCommandRawResult("python -m pytest -q", output, False, 1, "", "", None, len(output), 0, False)
    result = project_run_command_result(tmp_path, raw)
    assert result.model_text.index("2 failed, 1 passed") < result.model_text.index("setup output")
    assert "Failure excerpts" in result.model_text[:1600]
    assert result.audit["exit_code"] == 1 and not result.ok
    assert decode_json_object(result.canonical["execution"])["exit_code"] == 1
    assert raw.output == output


def test_mismatch_feedback_shows_requested_and_current_difference(tmp_path: Path) -> None:
    path = tmp_path / "a.py"
    path.write_text("def process():\n    first = 1\n    unique_marker = 2\n    return first\n", encoding="utf-8")
    result = edit_file(tmp_path, "a.py", [EditBlock("def process():\n    first = 1\n    return first\n", "def process():\n    return 3\n")])
    assert not result.ok and "unique_marker = 2" in result.model_text
    assert "Requested old text" in result.model_text and "Current file" in result.model_text


def test_recovered_edit_does_not_overwrite_concurrent_file_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from codey.toolchain import runtime

    path = tmp_path / "checkout.py"
    path.write_text(SOURCE, encoding="utf-8")
    original_write = runtime._write_text_after_final_path_check
    external = SOURCE + "external_change = True\n"

    def change_before_write(root: Path, rel: str, content: str, *, tool: str,
                            expected_version: str | None = None) -> runtime.ToolOutcome | None:
        path.write_text(external, encoding="utf-8")
        return original_write(root, rel, content, tool=tool, expected_version=expected_version)

    monkeypatch.setattr(runtime, "_write_text_after_final_path_check", change_before_write)
    result = edit_file(tmp_path, "checkout.py", [EditBlock(OLD, NEW)])
    assert not result.ok and result.error_code == "stale_read_reference"
    assert path.read_text() == external


def test_failure_excerpts_are_bounded_verbatim_and_do_not_guess_a_cause() -> None:
    output = "\x1b[31mFAILED test_a.py::test_value - assert 1 == 2\x1b[0m\n" * 100
    excerpt = failure_excerpt(output)
    assert excerpt.count("FAILED test_a.py::test_value") == 1
    assert "\x1b" not in excerpt
    assert not failure_excerpt("unknown application output")
    many = "\n".join(f"FAILED test_a.py::test_value_{index} - assert 1 == 2" for index in range(100))
    assert len(failure_excerpt(many)) <= 1600


def test_successful_command_output_is_not_reclassified_by_failure_words(tmp_path: Path) -> None:
    output = "FAILED is just a word in this successful program\n"
    raw = RunCommandRawResult("python example.py", output, True, 0, "", "", None, len(output), 0, False)
    result = project_run_command_result(tmp_path, raw)
    assert result.ok and result.audit["exit_code"] == 0
    assert "Failure excerpts" not in result.model_text
