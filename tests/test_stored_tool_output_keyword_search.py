"""Literal receipt search locates bounded excerpts without executing commands."""
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from codey.operations.task_execution import ExecutionDelegate
from codey.operations.task_session import TaskSession
from codey.runtime.core.models import ToolCall, ToolResult
from codey.storage.managed_outputs import ManagedOutputStore
from codey.toolchain.tool_spec import _all_specs


def make_delegate(text, tmp_path=None):
    session = TaskSession(policy=SimpleNamespace(allows=lambda _: True))
    audit = {"exit_code": 1}
    store = None
    if tmp_path is not None:
        store = ManagedOutputStore(tmp_path)
        ref = store.write_tool_output(session_id="s", run_id="r", tool_id="execution-1",
            permission_profile="coding_writer", tool_name="run", display_ref="tests", text=text)
        assert ref is not None
        audit["managed_output"] = {"handle": ref.handle, "sha256": ref.sha256}
    session._memory_results["execution-1"] = ToolResult(ToolCall("run", {}),
        "bounded preview" if store else text, ok=False, audit=audit)
    return ExecutionDelegate(session=session, managed_outputs=store, session_id="s", run_id="r")


def read(delegate, **args):
    result, ok, _ = delegate.execute(ToolCall("read_tool_result", {"result_ref": "execution-1", **args}))
    return result, ok


def test_one_search_recovers_middle_of_managed_output_with_context(tmp_path):
    text = "ordinary line\n" * 12000 + "unique evidence: expected 17\n" + "trailing line\n" * 12000
    delegate = make_delegate(text, tmp_path)
    result, ok = read(delegate, query="unique evidence", limit=800)
    data = json.loads(result.model_text)
    assert ok
    assert data["match_offset"] == text.index("unique evidence")
    assert "unique evidence: expected 17" in data["text"]
    assert len(data["text"]) <= 800
    assert data["text"] == text[data["offset"]:data["offset"] + 800]
    assert data["exit_code"] == 1 and data["ok"] is False
    assert len(delegate.session._memory_results) == 1


def test_search_cursor_finds_next_literal_match_and_does_not_interpret_regex():
    delegate = make_delegate("start a.*b first\nnext a.*b second\n")
    result, ok = read(delegate, query="a.*b", offset=16, limit=12)
    data = json.loads(result.model_text)
    assert ok and data["match_offset"] == 22
    assert "a.*b" in data["text"]


def test_not_found_is_explicit_and_preserves_incomplete_capture_fact():
    delegate = make_delegate("stored portion")
    delegate.session._memory_results["execution-1"] = replace(
        delegate.session._memory_results["execution-1"], truncated=True)
    result, ok = read(delegate, query="missing")
    data = json.loads(result.model_text)
    assert ok and data["match_offset"] is None
    assert data["text"] == "" and data["next_offset"] is None
    assert data["original_output_incomplete"] is True


@pytest.mark.parametrize("query", ["", None, 17, "x" * 513])
def test_invalid_search_does_not_return_receipt_bytes(query):
    result, ok = read(make_delegate("sensitive evidence"), query=query)
    assert not ok and "sensitive evidence" not in result.model_text


def test_search_is_advertised_by_the_same_tool_registry_as_paging():
    spec = _all_specs()["read_tool_result"]
    assert dict(spec.parameters)["query"] == {"type": "string"}
    assert "literal" in spec.description and "query" in spec.description
