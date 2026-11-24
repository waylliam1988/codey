from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from codey.operations.browser_task_handoff import _previous_page_observation
from codey.operations.edit_base import compact_symbol_retry
from codey.operations.kernel_execution import execute_turn
from codey.operations.kernel_facts import project_execution_record
from codey.operations.kernel_progress import KernelProgress
from codey.operations.kernel_protocol import build_turn_snapshot, normalize_turn
from codey.operations.progress_comparison import ProjectCheckComparison
from codey.operations.project_check_handoff import project_check_handoff
from codey.operations.provider_receipts import restore_snapshot, snapshot_description
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TASK_POLICY_FILE_SCOPE_VERSION, TaskPolicy
from codey.protocols.text_turn import decode_text_turn
from codey.providers.base import AssistantTurn, TurnFinish
from codey.runtime.core.models import ToolCall, ToolResult
from codey.runtime.core.operation_payload import RuntimeExecutionCounters, RuntimeOperationTransitionError
from codey.runtime.core.operation_reducer import admit_request_counters
from codey.runtime.core.progress import ProgressSnapshot
from codey.workspace.paths import content_hash
from codey.workspace.revision import WorkspaceRevisionStore
from tests.manual.agent_stability_cases import normalization_boundary_cases


def test_recovery_checks_committed_progress_and_preserves_global_bound() -> None:
    initial = RuntimeExecutionCounters()
    counters = initial.with_length_continuation(3)
    assert counters is not None
    assert counters.with_length_continuation(4) is None
    recovered = replace(counters, progress=ProgressSnapshot(schema_version=4, last_batch_id="write", recovery_progress_turn=5))
    second = recovered.with_length_continuation(7)
    assert second is not None and admit_request_counters(recovered, second) == second
    assert second.with_length_continuation(8) is None
    with pytest.raises(RuntimeOperationTransitionError, match="budgets"):
        admit_request_counters(counters, replace(counters, length_continuations=(3, 7)))
    recovered = replace(second, progress=replace(second.progress, last_batch_id="verify", recovery_progress_turn=8))
    third = recovered.with_length_continuation(9)
    assert third is not None
    ended = replace(third, progress=replace(third.progress, last_batch_id="fresh", recovery_progress_turn=10))
    assert ended.with_length_continuation(11) is None
    old = {key: value for key, value in initial.to_payload().items() if key != "length_continuations"}
    assert RuntimeExecutionCounters.from_payload({**old, "length_continuation_used": True}).length_continuations == (0,)


def test_web_recovery_progress_uses_new_original_passages_instead_of_repeated_reads() -> None:
    session = TaskSession(TaskPolicy(frozenset({"web.read"})), task_kind="chat")
    progress = KernelProgress()
    url = "https://example.org/library/catalog"

    def read(passage: str, *, ok: bool = True) -> ToolResult:
        return ToolResult(ToolCall("open_url", {"url": url}), "Text observation", ok=ok,
                          canonical={"research_observation": {"source": {"final_url": url}},
                                     "read_passages": [{"ref": passage}]})

    session.turn = 2
    progress.observe([read("published-hours")], session, batch_id="first")
    first = progress.snapshot()
    assert first.recovery_progress_turn == 2
    progress = KernelProgress(snapshot=ProgressSnapshot.from_payload(first.to_payload()))
    session.turn = 4
    progress.observe([read("published-hours"), read("not-acquired", ok=False)], session, batch_id="repeat")
    assert progress.snapshot().recovery_progress_turn == 2
    session.turn = 6
    progress.observe([read("holiday-exception")], session, batch_id="different-window")
    assert progress.snapshot().recovery_progress_turn == 6


def test_web_recovery_ignores_document_and_ref_churn_when_observed_text_is_identical() -> None:
    session = TaskSession(TaskPolicy(frozenset({"browser.observe"})), task_kind="chat")
    progress = KernelProgress()

    def snapshot(turn: int, text: str, *, text_truncated: bool = False) -> None:
        session.turn = turn
        page = ToolResult(ToolCall("browser_snapshot", {}), text, ok=True,
            canonical={"value": {"page_url": "https://example.org/museum", "text": text, "complete": False,
                                 "text_truncated": text_truncated, "elements_truncated": True,
                                 "elements": [{"ref": f"control-{turn}"}]}},
            audit={"browser_document": {"loader_id": str(turn)}})
        progress.observe([page], session, batch_id=str(turn))

    snapshot(2, "Opening hours")
    snapshot(3, "Opening hours")
    snapshot(4, "Unfinished preview", text_truncated=True)
    assert progress.snapshot().recovery_progress_turn == 2
    snapshot(5, "Holiday opening hours")
    assert progress.snapshot().recovery_progress_turn == 5


def test_browser_history_keeps_the_requested_entry_page_without_reusing_its_controls() -> None:
    entry = "https://example.org/museum"
    session = TaskSession(TaskPolicy(frozenset({"browser.observe"})), task_kind="chat",
                          required_source_urls=frozenset({entry}))
    for key, url in [("entry", entry), ("unrelated", "https://example.org/other")]:
        call = ToolCall("browser_snapshot", {"max_text": 100}, call_id=key)
        result = ToolResult(call, "Opening hours", ok=True,
            canonical={"value": {"page_url": url, "text": "Opening hours", "complete": True,
                                 "scope": {"root_ref": "", "selector": ""},
                                 "elements": [{"ref": "stale-control"}]}},
            audit={"browser_document": {"url": url}})
        session._memory_results[key] = result
        project_execution_record(session, key, call, result, ok=True)
    assert _previous_page_observation(session, "https://example.org/museum/admission") == {
        "result_ref": "entry", "page_url": entry, "text": "Opening hours"}
    assert _previous_page_observation(session, entry) == {}
    session.required_source_urls = frozenset()
    assert _previous_page_observation(session, "https://example.org/museum/admission")["page_url"] == entry
    assert _previous_page_observation(session, "https://foreign.example/museum") == {}


def test_truncated_edit_binds_a_single_observed_definition_through_request_restore(tmp_path: Path) -> None:
    for name in ("colors", "labels"):
        (tmp_path / f"{name}.py").write_text(f'OUTSIDE = "retained"\n\ndef {name}(): return 1\n', encoding="utf-8")
    session = TaskSession(TaskPolicy(frozenset({"control", "project.read", "project.write"})),
                          task_kind="project", project=str(tmp_path))
    assert all(row.ok for row in execute_turn(session, [ToolCall("read_file", {"path": "labels.py"}),
        ToolCall("read_file", {"path": "colors.py"})], run_id="read", turn=1, project_path=tmp_path))
    snapshot = build_turn_snapshot(session)
    partial = AssistantTurn(text='{"tool":"edit","args":{"symbol":"labels","path":"./labels.py","replacements":[',
                            finish=TurnFinish.OUTPUT_LIMIT)
    assert normalize_turn(partial, snapshot=snapshot).protocol_error
    retry = restore_snapshot(snapshot_description(compact_symbol_retry(snapshot, partial, session=session)))
    assert retry.tool_names == ("edit",)
    good = normalize_turn(decode_text_turn('{"tool":"edit","args":{"content":"def labels(): return 2"}}'), snapshot=retry)
    assert not good.protocol_error
    assert good.calls[0].args == {"content": "def labels(): return 2", "path": "labels.py", "symbol": "labels",
                                  "base_result_ref": dict(snapshot.symbol_read_refs)["labels.py"]}
    raw = AssistantTurn(text="def labels():\n    return r'[^\\w\\s]'")
    assert normalize_turn(raw, snapshot=retry).calls[0].args["content"] == raw.text
    imports = normalize_turn(AssistantTurn(text="```python\nimport re\n\ndef labels():\n    return re.ASCII\n```"), snapshot=retry)
    assert imports.calls[0].args["content"] == "def labels():\n    import re\n    return re.ASCII"
    signature = AssistantTurn(text="import re\ndef labels(value=re.ASCII):\n    return value")
    assert normalize_turn(signature, snapshot=retry).protocol_error
    assert normalize_turn(replace(raw, finish=TurnFinish.OUTPUT_LIMIT), snapshot=retry).protocol_error
    assert normalize_turn(AssistantTurn(text="I finished the change."), snapshot=retry).protocol_error
    wrong = decode_text_turn('{"tool":"edit","args":{"content":"def colors(): return 2","path":"colors.py"}}')
    assert normalize_turn(wrong, snapshot=retry).protocol_error


def test_source_only_retry_keeps_failures_without_suggesting_tool_calls(tmp_path: Path) -> None:
    definition = "def value(): return 0"
    path = tmp_path / "example.py"
    path.write_text(definition + "\n", encoding="utf-8")
    session = TaskSession(TaskPolicy(frozenset({"control", "project.read", "project.write", "project.verify"})),
                          task_kind="project", project=str(tmp_path), workspace_revision=1,
                          workspace_fingerprint="current")
    assert execute_turn(session, [ToolCall("read_file", {"path": path.name})], run_id="read", turn=1,
                        project_path=tmp_path)[0].ok
    check = ToolResult(ToolCall("run", {"command": "python -m pytest"}), "failed", ok=False)
    session._memory_results["check"] = check
    session._memory_results["no-op"] = ToolResult(ToolCall("edit", {"path": path.name, "replacements": [
        {"old_string": definition, "new_string": definition}]}), "unchanged", ok=True, audit={"changed": False})
    fact = ProjectCheckComparison("python -m pytest", ".", "current", 1, ("FAILED test_value.py::test_value",),
                                  "child", "check", frames=({"path": path.name, "line": 1, "source": definition,
                                                            "assertions": ["assert 0 == 1"]},), workspace_revision=1)

    def compare(result: ToolResult) -> ProjectCheckComparison | None:
        return fact if result is check else None

    partial = AssistantTurn(text='{"tool":"edit","args":{"path":"example.py","replacements":[',
                            finish=TurnFinish.OUTPUT_LIMIT)
    retry = compact_symbol_retry(build_turn_snapshot(session), partial, session=session)
    assert retry.tool_names == ("edit",)
    handoff = project_check_handoff(session, retry, tmp_path, compare)
    assert "assert 0 == 1" in handoff and definition in handoff
    assert '"tool":' not in handoff and '"next":' not in handoff


@pytest.mark.parametrize("source_kind", ["unique", "ambiguous", "unobserved", "shadowed", "unread_conflict", "versioned",
                                         "versioned_crlf", "stale", "symbol", "nested_uppercase"])
def test_python_edit_uses_only_an_unambiguous_observed_import(tmp_path: Path, source_kind: str) -> None:
    if shutil.which("ruff") is None:
        pytest.skip("Ruff executable is unavailable")
    (tmp_path / "labels.py").write_text("def public_label(value): return value.casefold()\n", encoding="utf-8")
    original = '# report formatter\n"""Render the report title."""\nfrom __future__ import annotations\n\ndef title(value): return value\n'
    path = "nested/report.PY" if source_kind == "nested_uppercase" else "report.py"
    target = tmp_path / path
    target.parent.mkdir(exist_ok=True)
    stored = original.replace("\n", "\r\n") if source_kind == "versioned_crlf" else original
    target.write_bytes(stored.encode("utf-8"))
    revisions = WorkspaceRevisionStore(tmp_path.parent / (tmp_path.name + "-state"))
    observed = [path]
    if source_kind != "unobserved":
        observed.append("labels.py")
    if source_kind in {"ambiguous", "unread_conflict"}:
        (tmp_path / "alternate.py").write_text("def public_label(value): return value\n", encoding="utf-8")
    if source_kind == "ambiguous":
        observed.append("alternate.py")
    session = TaskSession(TaskPolicy(frozenset({"control", "project.read", "project.write"}),
                                     version=TASK_POLICY_FILE_SCOPE_VERSION, edit_scope=(path,)),
                          task_kind="project", project=str(tmp_path))
    calls = [ToolCall("read_file", {"path": path}) for path in observed]
    assert all(row.ok for row in execute_turn(session, calls, turn=1, project_path=tmp_path, run_id="read"))
    if source_kind == "shadowed":
        (tmp_path / "labels").mkdir()
    updated = "def title(value): return f'«{public_label(value)}»'\n"
    snapshot = build_turn_snapshot(session)
    if source_kind == "symbol":
        edit = ToolCall("edit", {"path": path, "symbol": "title", "content": updated,
                                 "base_result_ref": dict(snapshot.symbol_read_refs)[path]})
    else:
        edit = ToolCall("edit", {"path": path, "replacements": [
            {"old_string": "def title(value): return value\n", "new_string": updated}]})
        if source_kind in {"versioned", "versioned_crlf", "stale"}:
            version = content_hash("other" if source_kind == "stale" else stored)
            edit = replace(edit, args={**edit.args, "expected_version": version})
    result = execute_turn(session, [edit], turn=2, project_path=tmp_path, run_id="edit",
                          snapshot=snapshot, workspace_revision_store=revisions)[0]
    if source_kind == "stale":
        assert not result.ok and "python_diagnostics" not in result.audit
        assert target.read_text(encoding="utf-8") == original
        return
    assert result.ok and "F821" in result.model_text, result.model_text
    expected = original.replace("def title(value): return value\n", updated)
    if source_kind in {"unique", "versioned", "versioned_crlf"}:
        expected = expected.replace("from __future__ import annotations\n",
                                    "from __future__ import annotations\nfrom labels import public_label\n")
        assert result.audit.get("python_import_binding")
    else:
        assert result.audit.get("python_import_binding") is None
    assert target.read_text(encoding="utf-8") == expected


def test_normalization_oracle_exercises_all_ascii_punctuation_and_unicode() -> None:
    examples = dict(normalization_boundary_cases())
    assert examples["A_B"] == examples["A-B"] == "ab"
    assert examples[" Café, 42! "] == "café-42"
    assert examples[" A？B! "] == "a？b"
    assert examples["A→Z"] == "a→z"
    assert examples[" A\u00a0B "] == "a-b"
