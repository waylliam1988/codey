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
    session, result = execute(tmp_path, "return unwrap(await tools.edit({path:'result.json', transform:text => {"
                              "const data=JSON.parse(text); data.amount=173; return JSON.stringify(data); }}));")
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
    assert "transform" not in edits[0].call.args  # Callback never crosses the JSON host bridge.


def test_helper_preserves_external_change_and_propagates_edit_failure(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("before\n", encoding="utf-8")

    def concurrent_read(root: Path, path: str, *, offset: int = 1, limit: int = 300) -> ToolOutcome:
        outcome = tool_runtime.read_file(root, path, offset=offset, limit=limit)
        target.write_text("external\n", encoding="utf-8")
        return outcome

    session, result = execute(tmp_path, "await tools.edit({path:'a.txt', transform:text => text + 'change'}); return 'updated';",
                              tool_fns=AgentToolFns(read_file=concurrent_read))
    assert not result.ok
    assert "Read the current file again" in result.model_text
    assert target.read_text() == "external\n"
    edits = [receipt for receipt in session._memory_results.values() if receipt.call.name == "edit"]
    assert len(edits) == 1 and not edits[0].ok  # No implicit reread or retry.


@pytest.mark.parametrize("failure", ["missing", "partial", "transform"])
def test_helper_never_turns_read_or_transform_failure_into_a_write(tmp_path: Path, failure: str) -> None:
    target = tmp_path / "a.txt"
    if failure != "missing":
        target.write_text("line\n" * (400 if failure == "partial" else 1), encoding="utf-8")
    before = target.read_bytes() if target.exists() else None
    transform = "text => { throw Error('cannot transform'); }" if failure == "transform" else "text => text + 'change'"
    session, result = execute(tmp_path, f"return unwrap(await tools.edit({{path:'a.txt', transform:{transform}}}));")
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
    with path.open("a", encoding="utf-8") as stream:
        stream.write("\n" + json.dumps({**request, "exchange": 2}))
    metrics = provider_metrics(path)
    assert metrics["model_calls"] == 2 and metrics["usage_complete"] is False
    assert metrics["prompt_tokens"] is None and metrics["completion_tokens"] is None


def test_declarations_replace_edit_contract_instead_of_adding_an_alternative(tmp_path: Path) -> None:
    snapshot = codemode_catalog.build_catalog_snapshot([
        ToolCatalogEntry("edit", "Old content/base_result_ref/replacements contract"),
        ToolCatalogEntry("read_file", "Read file"),
    ])
    original = codemode_catalog.render_declarations(snapshot)
    with prototype_helper(tmp_path, enabled=True):
        declarations = codemode_catalog.render_declarations(snapshot)
    assert declarations.count("  edit(args:") == 1
    assert "transform: (text: string) => string" in declarations
    assert "Old content/base_result_ref/replacements contract" not in declarations
    assert "declare function update_file" not in declarations
    assert "read_file(args:" in declarations
    assert codemode_catalog.render_declarations(snapshot) == original


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
        assert "transform: (text: string) => string" in snapshot.contract_text


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
def test_probe_exposes_stale_closure_overwrite_between_manual_and_helper_read(tmp_path: Path, enabled: bool) -> None:
    """Characterize the observed model pattern, including the prototype's lost-update risk."""
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
    edit = ("return unwrap(await tools.edit({path:'a.json',transform:text=>newText}));" if enabled else
            "return unwrap(await tools.edit({path:'a.json',content:newText,base_result_ref:r.result_ref}));")
    _session, result = execute(tmp_path, common + edit, tool_fns=AgentToolFns(read_file=concurrent_read), enabled=enabled)
    actual = json.loads(target.read_text(encoding="utf-8"))
    if enabled:
        assert result.ok, result.model_text
        assert reads == 2
        assert actual == {"amount": 173, "note": "before"}  # Prototype overwrites the external change.
    else:
        assert not result.ok
        assert reads == 1
        assert actual == {"amount": 0, "note": "external"}  # Original receipt rejects the stale edit.
