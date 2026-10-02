"""Edit rejects legacy aliases before execution; canonical old/new works.

Old top-level old_string/new_string and replacement search/replace
must be rejected by validation/execution before touching files. Canonical
replacements with old_string/new_string still edit, and no-change edits report
changed=False.
"""
from __future__ import annotations


def _delegate(tmp_path):
    from pathlib import Path

    from codey.agents.tools import DEFAULT_TOOL_FNS
    from codey.operations.task_execution import ExecutionDelegate
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy

    (tmp_path / "a.py").write_text("hello\n", encoding="utf-8")
    session = TaskSession(
        policy=TaskPolicy(grants=frozenset({"control", "project.read", "project.write"})),
        task_kind="project",
        project=str(tmp_path),
        max_turns=3,
    )
    from codey.agents.protocol import canonical_project_path

    session.read_files.add(canonical_project_path(Path(str(tmp_path)), "a.py"))
    delegate = ExecutionDelegate(session=session, project_path=str(tmp_path), tool_fns=DEFAULT_TOOL_FNS)
    return session, delegate


def test_legacy_top_level_alias_rejected(tmp_path):
    from codey.runtime.core.models import ToolCall

    _session, delegate = _delegate(tmp_path)
    call = ToolCall("edit", {"path": "a.py", "old_string": "hello", "new_string": "hi"})
    result, ok, _x = delegate.execute(call)
    assert ok is False
    assert str(result.model_text).startswith("ERROR:")


def test_legacy_replacement_alias_rejected(tmp_path):
    from codey.runtime.core.models import ToolCall

    _session, delegate = _delegate(tmp_path)
    call = ToolCall("edit", {"path": "a.py", "replacements": [{"search": "hello", "replace": "hi"}]})
    result, ok, _x = delegate.execute(call)
    assert ok is False
    assert str(result.model_text).startswith("ERROR:")


def test_canonical_replacement_edits(tmp_path):
    from codey.runtime.core.models import ToolCall

    _session, delegate = _delegate(tmp_path)
    call = ToolCall("edit", {"path": "a.py", "replacements": [{"old_string": "hello", "new_string": "hi"}]})
    result, ok, _x = delegate.execute(call)
    assert ok is True
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "hi\n"


def test_no_change_reports_unchanged(tmp_path):
    from codey.runtime.core.models import ToolCall

    _session, delegate = _delegate(tmp_path)
    call = ToolCall("edit", {"path": "a.py", "replacements": [{"old_string": "missing", "new_string": "hi"}]})
    result, ok, _x = delegate.execute(call)
    # Missing search either errors or reports unchanged without writing.
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "hello\n"


def test_validation_rejects_legacy_before_execution():
    from codey.operations.kernel_protocol import _validate_tool_args

    for args in (
        {"path": "a.py", "old_string": "x", "new_string": "y"},
        {"path": "a.py", "replacements": [{"search": "x", "replace": "y"}]},
        {"path": "a.py", "search": "x", "replace": "y"},
    ):
        _clean, error = _validate_tool_args("edit", args)
        assert error, f"validation must reject {args!r}"


def test_native_validation_rejects_legacy_before_execution():
    from codey.operations.kernel_protocol import _validate_tool_args
    from codey.toolchain.tool_spec import spec_for_tool

    spec = spec_for_tool("edit")
    frozen = {"edit": spec}
    _clean, error = _validate_tool_args(
        "edit", {"path": "a.py", "old_string": "x", "new_string": "y"}, frozen_specs=frozen,
    )
    assert error
