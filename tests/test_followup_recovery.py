"""Fault injection uses the production durable kernel and original file receipts."""
from __future__ import annotations

from pathlib import Path

import pytest

from codey.agents.tools import AgentToolFns
from codey.codemode.host import CodeModeHost
from codey.durable import DurableSession, SQLiteDurableStore
from codey.operations.generation_boundary import durable_generation_boundary_callback
from codey.operations.kernel_contracts import (
    KernelExecutionDeps,
    KernelObservationDeps,
    KernelRunRequest,
    KernelTransportDeps,
)
from codey.operations.provider_session import adapt_provider
from codey.operations.task_effects import KernelEffectSink
from codey.operations.task_loop import run_task_kernel
from codey.providers.base import AssistantTurn, ProviderToolCall
from codey.runtime.core.models import ToolResult
from codey.runtime.write.mutation_line import RuntimeMutationLine
from codey.toolchain import runtime
from codey.toolchain.runtime import ToolOutcome
from codey.types.json_value import JsonObject
from codey.workspace.changes import ChangeTracker, SnapshotStore
from codey.workspace.task_changes import collect_task_changes
from tests.test_codemode_replay_authority import _durable, _new_session, fixture


@pytest.mark.parametrize("phase", ["before_admission", "after_admission", "after_write_before_receipt"])
def test_patch_cold_restart_preserves_settled_write_and_original_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str,
) -> None:
    class Crash(BaseException):
        pass

    project, state, _, line, managed, revisions, sink, session = fixture(tmp_path)
    for name in ("a.py", "b.py"):
        (project / name).write_bytes(b"limit = 12\r\n# keep\n")
    stores = SnapshotStore(state, task_id="r")
    tracker = ChangeTracker(project, stores)
    code = """
        await tools.read_file({path:'a.py'});
        await tools.read_file({path:'b.py'});
        return unwrap(await tools.edit({files:[
            {path:'a.py',replacements:[{old_string:'12',new_string:'37'}]},
            {path:'b.py',replacements:[{old_string:'12',new_string:'37'}]}
        ]}));
    """

    class Provider:
        native_tools = True

        def send_turn(self, *args: object) -> AssistantTurn:
            return AssistantTurn(tool_calls=(ProviderToolCall("patch", "codemode", {"code": code}),))

        def send_tool_results(self, *args: object) -> AssistantTurn:
            return AssistantTurn(tool_calls=(ProviderToolCall("done", "done", {"summary": "finished"}),))

        def acknowledge_tool_results(self, *args: object) -> AssistantTurn:
            return AssistantTurn()

    def request(active_sink: KernelEffectSink, active_tracker: ChangeTracker) -> KernelRunRequest:
        return KernelRunRequest(transport=KernelTransportDeps(provider=adapt_provider(Provider()), provider_id="local",
            run_id="r", effect_scope="outer"), execution=KernelExecutionDeps(project_path=project, tool_fns=AgentToolFns(),
            change_tracker=active_tracker, workspace_revision_store=revisions), observation=KernelObservationDeps(
                intent_sink=active_sink, on_boundary=durable_generation_boundary_callback(durable=active_sink.durable,
                    session_id="s", task_id="r", sink=active_sink)))

    original = runtime._write_text_after_final_path_check
    invoke = CodeModeHost._invoke_one
    writes: list[str] = []

    def write(root: Path, rel: str, content: str, *, tool: str, expected_version: str | None = None) -> ToolOutcome | None:
        if rel == "b.py" and not writes:
            raise AssertionError("second file dispatched before the first write")
        if phase == "after_admission" and rel == "b.py":
            raise Crash()
        writes.append(rel)
        result = original(root, rel, content, tool=tool, expected_version=expected_version)
        if phase == "after_write_before_receipt" and rel == "b.py":
            assert result is None and (project / rel).read_bytes() == b"limit = 37\r\n# keep\n"
            raise Crash()
        return result

    monkeypatch.setattr(runtime, "_write_text_after_final_path_check", write)

    def interrupt_before_admission(host: CodeModeHost, index: int, name: str, args: JsonObject, *, query: bool) -> ToolResult:
        if phase == "before_admission" and name == "edit" and args.get("path") == "b.py":
            raise Crash()
        return invoke(host, index, name, args, query=query)

    monkeypatch.setattr(CodeModeHost, "_invoke_one", interrupt_before_admission)
    try:
        with pytest.raises(Crash):
            run_task_kernel(session, request=request(sink, tracker))
        expected_writes = ["a.py", "b.py"] if phase == "after_write_before_receipt" else ["a.py"]
        expected_b = b"limit = 37\r\n# keep\n" if phase == "after_write_before_receipt" else b"limit = 12\r\n# keep\n"
        assert writes == expected_writes
        assert (project / "a.py").read_bytes() == b"limit = 37\r\n# keep\n"
        assert (project / "b.py").read_bytes() == expected_b
    finally:
        _durable(line).close()

    def resume_write(root: Path, rel: str, content: str, *, tool: str, expected_version: str | None = None) -> ToolOutcome | None:
        assert rel != "a.py", "repeated a settled patch write"
        writes.append(rel)
        return original(root, rel, content, tool=tool, expected_version=expected_version)

    monkeypatch.setattr(runtime, "_write_text_after_final_path_check", resume_write)
    monkeypatch.setattr(CodeModeHost, "_invoke_one", invoke)
    durable = DurableSession(SQLiteDurableStore(state / "durable.sqlite3"))
    durable.recover_scheduler_state()
    if durable.task("r").status == "pending":
        durable.claim_task("r")
    resumed_sink = KernelEffectSink(RuntimeMutationLine(durable=durable), session_id="s", run_id="r", provider_id="local",
                                   managed_outputs=managed, durable=durable)
    resumed_tracker = ChangeTracker(project, stores)
    try:
        fresh = _new_session(project)
        outcome = run_task_kernel(fresh, request=request(resumed_sink, resumed_tracker))
        if phase != "before_admission":
            assert not outcome.completed and "external outcome is unknown" in outcome.summary
            assert writes == expected_writes, "an unreceipted admitted write must not run again"
            assert (project / "b.py").read_bytes() == expected_b
            assert not any(result.ok and result.call.name == "edit" and result.call.args.get("path") == "b.py"
                           for result in fresh._memory_results.values())
            return
        parents = [result for result in fresh._memory_results.values() if result.call.name == "codemode"]
        assert len(parents) == 1 and parents[0].ok, (outcome, parents)
        assert writes == ["a.py", "b.py"]
        assert (project / "a.py").read_bytes() == (project / "b.py").read_bytes() == b"limit = 37\r\n# keep\n"
        changes = collect_task_changes(project, resumed_tracker)
        assert changes["ok"] is True and changes["changed_count"] == 2
        assert "-limit = 12" in str(changes["diff"]) and "+limit = 37" in str(changes["diff"])
    finally:
        durable.close()


def test_selective_restore_rechecks_files_after_read_only_preview(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    tracker = ChangeTracker(project, SnapshotStore(tmp_path / "state", task_id="r"))
    for name in ("ready.py", "changed.py", "undone.py"):
        path = project / name
        path.write_text("before\n", encoding="utf-8")
        tracker.capture_before(name)
        path.write_text("after\n", encoding="utf-8")
        tracker.capture_after(name)
    (project / "undone.py").write_text("before\n", encoding="utf-8")

    assert dict(tracker.preview_restore(["ready.py", "changed.py", "undone.py", "missing.py"])) == {
        "ready.py": "restorable", "changed.py": "restorable", "undone.py": "already_restored",
        "missing.py": "unavailable",
    }
    assert (project / "ready.py").read_text(encoding="utf-8") == "after\n"
    (project / "changed.py").write_text("user edit\n", encoding="utf-8")
    assert tracker.restore([]).restored == []
    assert (project / "ready.py").read_text(encoding="utf-8") == "after\n"
    result = tracker.restore(["ready.py", "changed.py"])
    assert result.restored == ["ready.py"] and result.conflicts == ["changed.py"]
    assert (project / "ready.py").read_text(encoding="utf-8") == "before\n"
    assert (project / "changed.py").read_text(encoding="utf-8") == "user edit\n"
    assert tracker.preview_restore(["undone.py"]) == (("undone.py", "already_restored"),)
