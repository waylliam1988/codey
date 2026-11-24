from __future__ import annotations

from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest

from codey.app import task_submit
from codey.app.context import AppContext
from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.operations.kernel_protocol import build_turn_snapshot
from codey.operations.model_handoff import authorize_model_handoff, render_model_handoff
from codey.operations.task_effects import KernelEffectSink
from codey.operations.task_session import TaskSession
from codey.operations.tool_result_reader import ReceiptReader, read_tool_result
from codey.policies.task_policy import TaskPolicy
from codey.providers.snapshot import provider_snapshot
from codey.runtime.core.api_selection import ApiRunSelection
from codey.runtime.core.models import ToolCall
from codey.task.model import HandoffRun, ModelHandoffRejected
from codey.types.json_value import JsonObject
from tests.test_research_submission_handoff_production import install_http, requests
from tools.completion_artifact_audit import tool_receipts
from tools.completion_explanation_ab import ExperimentProvider, Target


@pytest.mark.usefixtures("scripted_local_api_connection", "no_external_advisor_models")
def test_model_handoffs_forward_original_receipts_read_by_the_intermediate_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    contents = b"# Original source context\n" * 44 + b"VALUE = 23\n"
    (project / "old.py").write_bytes(contents)
    (project / "unused.py").write_bytes(b"UNUSED = 9\n")
    state_home = tmp_path / "state"
    (tmp_path / "old").mkdir()
    (tmp_path / "new").mkdir()
    (tmp_path / "third").mkdir()
    session_id = "handoff-session"
    url = "http://scripted.test/v1"

    old_read = ""
    unused_read = ""

    def replies() -> Iterator[JsonObject]:
        yield {"tool": "read_file", "args": {"path": "old.py"}}
        yield {"tool": "read_file", "args": {"path": "unused.py"}}
        yield {"tool": "done", "args": {"summary": "Read the source value."}}
        yield {"tool": "read_tool_result", "args": {"result_ref": old_read, "query": "VALUE = 23"}}
        assert "VALUE = 23" in str(requests(tmp_path / "new")[-1]["messages"])
        yield {"tool": "done", "args": {"summary": "The existing result is available."}}
        assert old_read in str(requests(tmp_path / "third")[0]["messages"])
        yield {"tool": "read_tool_result", "args": {"result_ref": unused_read}}
        assert "stored result unavailable in this task" in str(requests(tmp_path / "third")[-1]["messages"])
        yield {"tool": "read_tool_result", "args": {"result_ref": old_read, "query": "VALUE = 23"}}
        assert "VALUE = 23" in str(requests(tmp_path / "third")[-1]["messages"])
        yield {"tool": "done", "args": {"summary": "The original value is 23."}}

    install_http(monkeypatch, replies(), native=False)
    monkeypatch.setattr("codey.app.provider_services.provider_availability", lambda: {"local": True})
    monkeypatch.setattr("codey.app.provider_services.provider_tab_availability", lambda: {})
    task = "Inspect the source value. Do not change files or run tests."
    old_provider = ExperimentProvider(Target(url, "old-model", 123, 10), tmp_path / "old", fixed_acquisition=False)
    try:
        old = run_headless(HeadlessRequest(project, task, provider_id="local", session_id=session_id,
            state_home=state_home, model_selection={"model": "old-model", "base_url": url},
            project_changes_required=False), emit_jsonl=lambda _row: None, connect_provider=lambda _name, *, port: old_provider)
    finally:
        old_provider.close()
    assert old.stop_reason == "done"
    reads = {row.result.call.args["path"]: row.identity
             for row in tool_receipts(state_home) if row.result.call.name == "read_file"}
    old_read, unused_read = reads["old.py"], reads["unused.py"]
    new_provider = ExperimentProvider(Target(url, "new-model", 456, 10), tmp_path / "new", fixed_acquisition=False)
    try:
        new = run_headless(HeadlessRequest(project, task, provider_id="local", session_id=session_id,
            state_home=state_home, model_selection={"model": "new-model", "base_url": url},
            project_changes_required=False, input=HandoffRun(old.run_id, task)),
            emit_jsonl=lambda _row: None, connect_provider=lambda _name, *, port: new_provider)
    finally:
        new_provider.close()
    assert new.stop_reason == "done" and new.run_id != old.run_id
    payload = requests(tmp_path / "new")[0]
    assert payload["model"] == "new-model"
    assert "User-authorized model handoff" in str(payload["messages"]) and old_read in str(payload["messages"])
    assert "VALUE = 23" not in str(payload["messages"])
    provider = ExperimentProvider(Target(url, "third-model", 789, 10), tmp_path / "third", fixed_acquisition=False)
    try:
        third = run_headless(HeadlessRequest(project, task, provider_id="local", session_id=session_id,
            state_home=state_home, model_selection={"model": "third-model", "base_url": url},
            project_changes_required=False, input=HandoffRun(new.run_id, task)),
            emit_jsonl=lambda _row: None, connect_provider=lambda _name, *, port: provider)
    finally:
        provider.close()
    assert third.stop_reason == "done"
    assert unused_read not in str(requests(tmp_path / "third")[0]["messages"])
    with closing(AppContext(state_home, start_scheduler=False)) as state:
        first = state.durable.task(old.run_id)
        second = state.durable.task(new.run_id)
        assert first.provider_snapshot["model_id"] == "old-model"
        assert second.provider_snapshot["model_id"] == "new-model"
        assert second.state.get("previous_run_id") in ("", None)
        assert second.state["input"]["source_run_id"] == old.run_id
        authorized = ReceiptReader(TaskSession(TaskPolicy(frozenset({"control"}))),
            state.task_submission_stores.managed_outputs, session_id, new.run_id)
        lookup = ToolCall("read_tool_result", {"result_ref": old_read, "query": "VALUE = 23"})
        assert read_tool_result(authorized, lookup, durable=state.durable).ok
        assert not authorized.session._memory_results
        unauthorized = ReceiptReader(authorized.session, authorized.managed_outputs, "other-conversation", new.run_id)
        assert not read_tool_result(unauthorized, lookup, durable=state.durable).ok
        unrelated = ReceiptReader(authorized.session, authorized.managed_outputs, session_id, old.run_id)
        assert not read_tool_result(unrelated, lookup, durable=state.durable).ok
        assert (project / "old.py").read_bytes() == contents
        assert len([row for row in tool_receipts(state_home) if row.result.call.name == "read_file"]) == 2


@pytest.mark.usefixtures("scripted_local_api_connection", "no_external_advisor_models")
def test_handoff_after_a_failed_check_verifies_the_repaired_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "value.py").write_text("VALUE = 23\n", encoding="utf-8")
    tests = "from value import VALUE\n\ndef test_current_value():\n    assert VALUE == 29\n"
    (project / "test_value.py").write_text(tests, encoding="utf-8")
    (project / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    state_home = tmp_path / "state"
    url = "http://scripted.test/v1"
    install_http(monkeypatch, iter((
        {"tool": "run", "args": {"command": "python -m pytest"}},
        {"tool": "done", "args": {"summary": "The service check failed."}},
        {"tool": "edit", "args": {"path": "test_value.py", "replacements": [{"old_string": "29", "new_string": "23"}]}},
        {"tool": "read_file", "args": {"path": "value.py"}},
        {"tool": "edit", "args": {"path": "value.py", "replacements": [{"old_string": "23", "new_string": "29"}]}},
        {"tool": "run", "args": {"command": "python -m pytest"}},
        {"tool": "done", "args": {"summary": "Updated the service value. The existing tests now pass."}},
    )), native=False)
    monkeypatch.setattr("codey.app.provider_services.provider_availability", lambda: {"local": True})
    monkeypatch.setattr("codey.app.provider_services.provider_tab_availability", lambda: {})

    def run(model: str, task: str, *, previous: str = "") -> str:
        output = tmp_path / model
        output.mkdir()
        provider = ExperimentProvider(Target(url, model, 123, 15), output, fixed_acquisition=False)
        try:
            result = run_headless(HeadlessRequest(project, task, provider_id="local", session_id="failed-check",
                state_home=state_home, model_selection={"model": model, "base_url": url},
                project_changes_required=bool(previous), input=HandoffRun(previous, task) if previous else None),
                emit_jsonl=lambda _row: None, connect_provider=lambda _name, *, port: provider)
            assert result.stop_reason == "done", result
            return result.run_id
        finally:
            provider.close()

    original = run("before", "Run the existing project tests and report the result. Do not change files.")
    with closing(AppContext(state_home, start_scheduler=False)) as state:
        workers = state.durable.owned_tasks(original)
        failed, = {effect.effect_id for worker in workers for effect in state.durable.settled_effects(worker.task_id)
                   if effect.intent.get("tool_name") == "run" and effect.status == "error"}
    revised = run("after", "Correct the service value so the existing tests pass. Only edit value.py.", previous=original)
    assert revised != original
    first = requests(tmp_path / "after")[0]
    assert first["model"] == "after" and failed in str(first["messages"]) and '"exit_code":1' in str(first["messages"])
    assert (project / "value.py").read_text(encoding="utf-8") == "VALUE = 29\n"
    assert (project / "test_value.py").read_text(encoding="utf-8") == tests
    receipts = tool_receipts(state_home)
    assert len({row.identity for row in receipts if row.result.call.name == "edit" and row.result.ok}) == 1
    assert any(row.result.call.name == "edit" and not row.result.ok and "authorized editing only value.py" in row.result.model_text
               for row in receipts)
    assert any(row.result.call.name == "run" and row.result.ok for row in receipts)


@pytest.mark.parametrize("name,args", [("browser_click", {"ref": "button"}), ("run", {"command": "pytest"})])
def test_model_handoff_rejects_active_work_and_only_exposes_uncertain_actions_for_observation(
    tmp_path: Path, name: str, args: JsonObject,
) -> None:
    with closing(AppContext(tmp_path / "state", start_scheduler=False)) as state:
        durable = state.durable
        source = ApiRunSelection("local", "rev1", "old", "openai-completions", False)
        target = ApiRunSelection("local", "rev1", "new", "openai-completions", False)
        durable.admit_input(conversation_id="session", request_id="old", task_id="old", text="old",
            provider_snapshot=provider_snapshot("local", source), task_state={"project": str(tmp_path)})
        handoff = HandoffRun("old", "Continue useful work")
        with pytest.raises(ModelHandoffRejected, match="settled boundary"):
            authorize_model_handoff(durable, request=handoff, target=target,
                                    session_id="session", run_id="new", project=str(tmp_path))
        durable.start_task("old", state={})
        mutations = state.task_submission_stores.runtime_mutations
        mutations.accept_operation(session_id="session", run_id="old", project=str(tmp_path), provider_id="local",
                                   turn_budget=3, max_repair_rounds=1, task_kind="project")
        mutations.mark_writer_running("session", "old", provider_id="local")
        sink = KernelEffectSink(mutations, session_id="session", run_id="old", provider_id="local",
                                durable=durable, managed_outputs=state.task_submission_stores.managed_outputs)
        snapshot = build_turn_snapshot(TaskSession(TaskPolicy(frozenset({"control", "project.verify", "web.read",
            "browser.observe", "browser.interact", "browser.effect"})), project=str(tmp_path)))
        specs = {spec.name: spec for spec in snapshot.frozen_specs}
        sink.begin_turn([("unknown-op", ToolCall(name, args, call_id="tool-id"), 0)], turn=0, specs=specs, scope="outer")
        durable.fail_task("old", "Interrupted", state={})
        with pytest.raises(ModelHandoffRejected, match="uncertain action"):
            authorize_model_handoff(durable, request=handoff, target=target,
                                    session_id="session", run_id="new", project=str(tmp_path))
        observed = HandoffRun("old", "Observe the current state", observe_only=True)
        assert task_submit._task_input_from_payload(task_submit._task_input_payload(observed)) == observed
        task, effects = authorize_model_handoff(durable, request=observed, target=target,
                                                session_id="session", run_id="new", project=str(tmp_path))
        text = render_model_handoff(task, effects, state.task_submission_stores.managed_outputs, durable=durable)
        assert "unknown-op" in text and '"unknown_actions":[{' in text
        assert len(durable.effects("old")) == 1
        with pytest.raises(ModelHandoffRejected, match="another|unavailable"):
            authorize_model_handoff(durable, request=observed, target=target, session_id="other",
                                    run_id="new", project=str(tmp_path))
