"""Validate the experiment helper against the production edit executor."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from codey.agents.tools import AgentToolFns
from codey.codemode import catalog as codemode_catalog
from codey.codemode.kernel import build_execute_turn_executor
from codey.codemode.types import ToolCatalogEntry
from codey.operations import kernel_protocol
from codey.operations.kernel_execution import execute_turn
from codey.operations.kernel_protocol import build_turn_snapshot
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.runtime.core.models import ToolCall, ToolResult
from codey.toolchain import runtime as tool_runtime
from codey.toolchain.runtime import ToolOutcome
from codey.workspace.revision import WorkspaceRevisionStore
from tools.receipt_bound_edit_ab import helper_metrics, prototype_helper, provider_metrics


def execute(root: Path, code: str, *, tool_fns: AgentToolFns | None = None,
            enabled: bool = True) -> tuple[TaskSession, ToolResult]:
    session = TaskSession(TaskPolicy(frozenset({"control", "project.read", "project.write"})), project=str(root))
    snapshot = build_turn_snapshot(session)
    revisions = WorkspaceRevisionStore(root.parent / (root.name + "-state"))
    executor = build_execute_turn_executor(
        session=session, snapshot=snapshot, run_id="helper-probe", effect_scope="task", turn=1,
        project_path=root, allow_writes=True, workspace_revision_store=revisions, tool_fns=tool_fns,
    )
    with prototype_helper(root, enabled=enabled):
        result = execute_turn(
            session, [ToolCall("codemode", {"code": code})], executors={"codemode": executor},
            run_id="helper-probe", effect_scope="task", turn=1, project_path=root, snapshot=snapshot,
            workspace_revision_store=revisions,
        )[0]
    return session, result


def test_helper_binds_the_exact_read_receipt_and_uses_one_existing_edit(tmp_path: Path) -> None:
    (tmp_path / "result.json").write_text('{"amount":0,"preserve":true}\n', encoding="utf-8")
    session, result = execute(tmp_path, "const r=await tools.read_file({path:'result.json'});"
                              "const data=JSON.parse(unwrap(r).text); data.amount=173;"
                              "return unwrap(await tools.edit({read:r,content:JSON.stringify(data)}));")
    assert result.ok, result.model_text
    assert json.loads((tmp_path / "result.json").read_text()) == {"amount": 173, "preserve": True}
    reads = [(ref, receipt) for ref, receipt in session._memory_results.items() if receipt.call.name == "read_file"]
    edits = [receipt for receipt in session._memory_results.values() if receipt.call.name == "edit"]
    assert len(reads) == len(edits) == 1
    assert edits[0].call.args["base_result_ref"] == reads[0][0]
    assert edits[0].call.args["path"] == reads[0][1].call.args["path"]
    assert helper_metrics(tmp_path) == {
        "confirmed_helper_calls": 1, "confirmed_helper_edits": 1, "untraced_failed_programs": 0,
    }
    assert "probe_" not in result.model_text
    assert "read" not in edits[0].call.args  # Guest object never crosses the JSON host bridge.


def test_helper_preserves_external_change_and_propagates_edit_failure(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("before\n", encoding="utf-8")

    def concurrent_read(root: Path, path: str, *, offset: int = 1, limit: int = 300) -> ToolOutcome:
        outcome = tool_runtime.read_file(root, path, offset=offset, limit=limit)
        target.write_text("external\n", encoding="utf-8")
        return outcome

    session, result = execute(tmp_path, "const r=await tools.read_file({path:'a.txt'});"
                              "await tools.edit({read:r,content:unwrap(r).text+'change'}); return 'updated';",
                              tool_fns=AgentToolFns(read_file=concurrent_read))
    assert not result.ok
    assert "Read the current file again" in result.model_text
    assert target.read_text() == "external\n"
    edits = [receipt for receipt in session._memory_results.values() if receipt.call.name == "edit"]
    assert len(edits) == 1 and not edits[0].ok  # No implicit reread or retry.


@pytest.mark.parametrize("failure", ["missing", "partial", "computation"])
def test_helper_never_turns_read_or_computation_failure_into_a_write(tmp_path: Path, failure: str) -> None:
    target = tmp_path / "a.txt"
    if failure != "missing":
        target.write_text("line\n" * (400 if failure == "partial" else 1), encoding="utf-8")
    before = target.read_bytes() if target.exists() else None
    computation = "throw Error('cannot compute');" if failure == "computation" else "const content=unwrap(r).text+'change';"
    session, result = execute(tmp_path, "const r=await tools.read_file({path:'a.txt'});" + computation
                              + "return unwrap(await tools.edit({read:r,content}));")
    assert not result.ok
    assert (target.read_bytes() if target.exists() else None) == before
    assert not any(receipt.ok and receipt.call.name == "edit" for receipt in session._memory_results.values())


def test_usage_counts_duplicate_exchanges_once_and_keeps_missing_usage_unknown(tmp_path: Path) -> None:
    path = tmp_path / "provider.jsonl"
    request = {"type": "request", "exchange": 1, "recorder_id": "r"}
    response = {"type": "response", "exchange": 1, "recorder_id": "r", "seconds": 1,
                "payload": {"usage": {"prompt_tokens": 10, "completion_tokens": 3}}}
    path.write_text("\n".join(json.dumps(row) for row in (request, response, response)), encoding="utf-8")
    metrics = provider_metrics(path)
    assert metrics["model_calls"] == 1 and metrics["prompt_tokens"] == 10 and metrics["completion_tokens"] == 3
    first_edit_bytes = path.stat().st_size
    with path.open("a", encoding="utf-8") as stream:
        stream.write("\n" + json.dumps({**request, "exchange": 2}))
    metrics = provider_metrics(path)
    assert metrics["model_calls"] == 2 and metrics["usage_complete"] is False
    assert metrics["prompt_tokens"] is None and metrics["completion_tokens"] is None
    edit_metrics = provider_metrics(path, through_bytes=first_edit_bytes)
    assert edit_metrics["model_calls"] == 1 and edit_metrics["prompt_tokens"] == 10
    assert edit_metrics["completion_tokens"] == 3 and edit_metrics["usage_complete"] is True


def test_declarations_replace_edit_contract_instead_of_adding_an_alternative(tmp_path: Path) -> None:
    snapshot = codemode_catalog.build_catalog_snapshot([
        ToolCatalogEntry("edit", "Old content/base_result_ref/replacements contract"),
        ToolCatalogEntry("read_file", "Read file"),
    ])
    original = codemode_catalog.render_declarations(snapshot)
    with prototype_helper(tmp_path, enabled=True):
        declarations = codemode_catalog.render_declarations(snapshot)
    assert declarations.count("  edit(args:") == 1
    assert "read: Awaited<ReturnType<typeof tools.read_file>>; content: string" in declarations
    assert "transform:" not in declarations
    assert "Old content/base_result_ref/replacements contract" not in declarations
    assert "declare function update_file" not in declarations
    assert "read_file(args:" in declarations
    assert codemode_catalog.render_declarations(snapshot) == original


def test_helper_does_not_advertise_edit_when_the_catalog_has_no_edit(tmp_path: Path) -> None:
    snapshot = codemode_catalog.build_catalog_snapshot([ToolCatalogEntry("browser_read", "Read current page")])
    original = codemode_catalog.render_declarations(snapshot)
    with prototype_helper(tmp_path, enabled=True):
        assert codemode_catalog.render_declarations(snapshot) == original
        assert "edit(args:" not in codemode_catalog.render_declarations(snapshot)


@pytest.mark.parametrize("enabled", [False, True])
def test_outer_prompt_and_native_tools_never_advertise_a_second_edit_contract(tmp_path: Path, enabled: bool) -> None:
    from codey.operations.provider_receipts import restore_snapshot, snapshot_description
    from codey.providers.base import tools_from_specs

    session = TaskSession(TaskPolicy(frozenset({"control", "project.read", "project.write"})), project=str(tmp_path))
    with prototype_helper(tmp_path, enabled=enabled):
        snapshot = kernel_protocol.build_turn_snapshot(session)
        restored = restore_snapshot(snapshot_description(snapshot))
    assert restored.inventory_names == snapshot.inventory_names
    assert restored.contract_text == snapshot.contract_text
    assert "edit" not in snapshot.tool_names
    assert "edit" in snapshot.nested_tool_names
    assert "edit" in snapshot.inventory_names
    assert "edit" not in {tool.name for tool in tools_from_specs(snapshot.model_specs)}
    assert snapshot.contract_text.count("  edit(args:") == 1
    assert '- edit:' not in snapshot.contract_text
    assert '"tool":"edit"' not in snapshot.contract_text
    if enabled:
        assert '"content"?:' not in snapshot.contract_text
        assert "read: Awaited<ReturnType<typeof tools.read_file>>; content: string" in snapshot.contract_text


def test_unused_edit_declaration_never_counts_as_executed_adoption(tmp_path: Path) -> None:
    session, result = execute(tmp_path, "async function unused() { return tools.edit({}); } return 'no edit';")
    assert result.ok, result.model_text
    assert not session._memory_results or all(
        receipt.call.name not in {"read_file", "edit"} for receipt in session._memory_results.values())
    assert helper_metrics(tmp_path)["confirmed_helper_edits"] == 0


def test_failed_guest_adoption_remains_unknown_instead_of_counting_as_zero(tmp_path: Path) -> None:
    _session, result = execute(tmp_path, "throw Error('before edit');")
    assert not result.ok
    assert helper_metrics(tmp_path)["untraced_failed_programs"] == 1


@pytest.mark.parametrize("enabled", [False, True])
def test_both_arms_reject_external_change_using_the_original_read(tmp_path: Path, enabled: bool) -> None:
    """The model pattern from the previous experiment must now preserve external changes."""
    target = tmp_path / "a.json"
    target.write_text('{"amount":0,"note":"before"}', encoding="utf-8")
    reads = 0

    def concurrent_read(root: Path, path: str, *, offset: int = 1, limit: int = 300) -> ToolOutcome:
        nonlocal reads
        reads += 1
        outcome = tool_runtime.read_file(root, path, offset=offset, limit=limit)
        if reads == 1:
            target.write_text('{"amount":0,"note":"external"}', encoding="utf-8")
        return outcome

    common = ("const r=await tools.read_file({path:'a.json'});"
              "const data=JSON.parse(unwrap(r).text); data.amount=173;"
              "const newText=JSON.stringify(data);")
    edit = ("return unwrap(await tools.edit({read:r,content:newText}));" if enabled else
            "return unwrap(await tools.edit({path:'a.json',content:newText,base_result_ref:r.result_ref}));")
    _session, result = execute(tmp_path, common + edit, tool_fns=AgentToolFns(read_file=concurrent_read), enabled=enabled)
    actual = json.loads(target.read_text(encoding="utf-8"))
    assert not result.ok
    assert reads == 1
    assert actual == {"amount": 0, "note": "external"}


def test_read_object_binds_its_path_even_after_another_file_is_read(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("b\n", encoding="utf-8")
    session, result = execute(tmp_path,
        "const r=await tools.read_file({path:'a.txt'});"
        "await tools.read_file({path:'b.txt'});"
        "return unwrap(await tools.edit({read:r,content:unwrap(r).text+'changed'}));")
    assert result.ok, result.model_text
    assert (tmp_path / "a.txt").read_text() == "a\nchanged"
    assert (tmp_path / "b.txt").read_text() == "b\n"
    reads = [(ref, receipt) for ref, receipt in session._memory_results.items() if receipt.call.name == "read_file"]
    edit = next(receipt for receipt in session._memory_results.values() if receipt.call.name == "edit")
    assert edit.call.args["base_result_ref"] == reads[0][0]
    assert edit.call.args["path"] == "a.txt"


def test_a_newer_read_of_the_same_path_never_replaces_the_bound_receipt(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("before\n", encoding="utf-8")
    reads = 0

    def concurrent_read(root: Path, path: str, *, offset: int = 1, limit: int = 300) -> ToolOutcome:
        nonlocal reads
        reads += 1
        outcome = tool_runtime.read_file(root, path, offset=offset, limit=limit)
        if reads == 1:
            target.write_text("external\n", encoding="utf-8")
        return outcome

    session, result = execute(tmp_path,
        "const original=await tools.read_file({path:'a.txt'});"
        "await tools.read_file({path:'a.txt'});"
        "return unwrap(await tools.edit({read:original,content:'stale'}));",
        tool_fns=AgentToolFns(read_file=concurrent_read))
    assert not result.ok
    assert reads == 2
    assert target.read_text() == "external\n"
    receipts = [(ref, receipt) for ref, receipt in session._memory_results.items() if receipt.call.name == "read_file"]
    edit = next(receipt for receipt in session._memory_results.values() if receipt.call.name == "edit")
    assert edit.call.args["base_result_ref"] == receipts[0][0]


@pytest.mark.parametrize("expression", ["{...r}", "JSON.parse(JSON.stringify(r))", "unwrap(r)"])
def test_copied_or_unwrapped_read_objects_never_reach_the_host_edit(tmp_path: Path, expression: str) -> None:
    target = tmp_path / "a.txt"
    target.write_text("before\n", encoding="utf-8")
    session, result = execute(tmp_path,
        "const r=await tools.read_file({path:'a.txt'});"
        f"return unwrap(await tools.edit({{read:{expression},content:'changed'}}));")
    assert not result.ok
    assert "original tools.read_file result" in result.model_text
    assert target.read_text() == "before\n"
    assert not any(receipt.call.name == "edit" for receipt in session._memory_results.values())


def test_mutating_read_arguments_or_receipt_metadata_cannot_retarget_the_edit(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("b\n", encoding="utf-8")
    session, result = execute(tmp_path,
        "const args={path:'a.txt'}; const pending=tools.read_file(args); args.path='b.txt';"
        "const r=await pending; r.result_ref='forged';"
        "return unwrap(await tools.edit({read:r,content:'changed'}));")
    assert result.ok, result.model_text
    assert (tmp_path / "a.txt").read_text() == "changed"
    assert (tmp_path / "b.txt").read_text() == "b\n"
    ref = next(ref for ref, receipt in session._memory_results.items() if receipt.call.name == "read_file")
    edit = next(receipt for receipt in session._memory_results.values() if receipt.call.name == "edit")
    assert edit.call.args["base_result_ref"] == ref
