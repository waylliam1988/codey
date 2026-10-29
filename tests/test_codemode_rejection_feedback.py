"""Rejected programs retain the frozen API contract without running any effects."""
import json
from dataclasses import replace

import pytest

from codey.operations.kernel_execution import execute_turn
from codey.operations.kernel_protocol import build_turn_snapshot, normalize_turn
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.protocols.text_turn import decode_text_turn
from codey.providers.base import AssistantTurn, ProviderToolCall
from codey.runtime.core.models import ToolCall
from codey.toolchain import tool_spec


@pytest.mark.parametrize("native", [False, True])
def test_host_rejection_resends_the_captured_contract_not_a_network_workaround(monkeypatch, native):
    state = TaskSession(policy=TaskPolicy(frozenset({"control", "project.read"})))
    snapshot = build_turn_snapshot(state, native=native)
    spec = next(spec for spec in snapshot.frozen_specs if spec.name == "codemode")
    # A registry change after sending cannot alter the correction contract.
    monkeypatch.setitem(tool_spec.tool_specs(), "codemode", replace(spec, description="UNSENT-CONTRACT"))
    args = {"code": "const fs = require('fs'); return fs.readFileSync('a.txt', 'utf8');"}
    reply = (AssistantTurn(tool_calls=(ProviderToolCall("rejected", "codemode", args),))
             if native else decode_text_turn(json.dumps({"tool": "codemode", "args": args})))
    plan = normalize_turn(reply, snapshot=snapshot)
    assert not plan.calls and plan.protocol_error
    assert "before execution" in plan.protocol_error
    assert "'fs'" in plan.protocol_error
    from codey.operations.kernel_prompt import _repair_prompt

    feedback = _repair_prompt(plan, snapshot=snapshot, native=native)
    if native:
        from codey.providers.base import tools_from_specs

        assert spec.description in next(tool.description for tool in tools_from_specs(snapshot.frozen_specs)
                                        if tool.name == "codemode")
        assert "required arguments: code" in feedback
    else:
        assert spec.description in feedback
    assert spec.declarations not in feedback
    assert "UNSENT-CONTRACT" not in plan.protocol_error
    assert "HTTP" not in plan.protocol_error and "if shell" not in plan.protocol_error
    assert state.executed == {}
    assert args["code"].startswith("const fs = require('fs')")


@pytest.mark.parametrize("code,kind", [
    ("return fetch('https://example.com');", "host access"),
    ("return await tools.codemode({code:'return 1'});", "recursive"),
])
def test_rejection_reports_the_actual_category_without_executing_a_tool(code, kind):
    state = TaskSession(policy=TaskPolicy(frozenset({"control", "project.read"})))
    snapshot = build_turn_snapshot(state, native=True)
    result = execute_turn(state, [ToolCall("codemode", {"code": code}, call_id="denied")],
                          snapshot=snapshot, executors={"codemode": lambda _: pytest.fail("must not run")})[0]
    assert not result.ok
    assert kind in result.model_text
    assert ("recursive" in result.model_text.split("Current codemode contract:")[0]) == (kind == "recursive")
    assert ("host access" in result.model_text.split("Current codemode contract:")[0]) == (kind == "host access")
    spec = next(spec for spec in snapshot.frozen_specs if spec.name == "codemode")
    assert spec.description in result.model_text
    assert not state.executed


def test_real_runtime_type_error_retains_the_frozen_contract_and_cannot_complete(tmp_path, monkeypatch):
    from codey.codemode.kernel import build_execute_turn_executor
    from codey.operations.completion_gate import evaluate

    (tmp_path / "a.txt").write_text("OBSERVED-729\n", encoding="utf-8")
    state = TaskSession(policy=TaskPolicy(frozenset({"control", "project.read"}),
                                        required_checks=("tool_succeeded:codemode",)), project=str(tmp_path))
    snapshot = build_turn_snapshot(state, native=True)
    spec = next(spec for spec in snapshot.frozen_specs if spec.name == "codemode")
    monkeypatch.setitem(tool_spec.tool_specs(), "codemode", replace(spec, description="UNSENT-CONTRACT"))
    executor = build_execute_turn_executor(session=state, snapshot=snapshot, run_id="r", effect_scope="task",
                                           turn=1, project_path=tmp_path)
    result = execute_turn(state, [ToolCall("codemode", {"code": (
        "const a = await tools.read_file({path:'a.txt'}); return a.split('\\n')[0];"
    )}, call_id="bad-value")], snapshot=snapshot, project_path=tmp_path,
        executors={"codemode": executor}, run_id="r", turn=1)[0]
    assert not result.ok and "a.split is not a function" in result.model_text
    assert spec.description in result.model_text
    assert "UNSENT-CONTRACT" not in result.model_text
    assert spec.declarations not in result.model_text
    assert result.model_text.count(spec.description) == 1
    assert not evaluate(state, "done").complete
    reads = [row for row in state.executed.values() if row.get("name") == "read_file"]
    assert len(reads) == 1 and reads[0]["ok"] is True
    parents = [row for row in state.executed.values() if row.get("name") == "codemode"]
    assert len(parents) == 1 and parents[0]["ok"] is False


@pytest.mark.parametrize("native", [False, True])
def test_documented_example_propagates_a_real_child_read_failure(tmp_path, native):
    from codey.codemode.kernel import build_execute_turn_executor

    state = TaskSession(policy=TaskPolicy(frozenset({"control", "project.read"})), project=str(tmp_path))
    snapshot = build_turn_snapshot(state, native=native)
    spec = next(spec for spec in snapshot.frozen_specs if spec.name == "codemode")
    args = json.loads(spec.json_examples[0])["args"]
    executor = build_execute_turn_executor(session=state, snapshot=snapshot, run_id="missing", effect_scope="task",
                                           turn=1, project_path=tmp_path)
    result = execute_turn(state, [ToolCall("codemode", args, call_id="missing-file")], snapshot=snapshot,
                          project_path=tmp_path, executors={"codemode": executor}, run_id="missing", turn=1)[0]
    assert not result.ok
    assert spec.description in result.model_text
    assert len(state.executed) == 2
    assert all(row["ok"] is False for row in state.executed.values())


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("failure", ["host", "value", "console", "missing_return", "missing_property", "nested_property", "uncalled_main", "syntax"])
def test_rejected_then_corrected_program_uses_real_kernel_and_recovers(tmp_path, native, failure):
    from types import SimpleNamespace

    from codey.durable import DurableSession, SQLiteDurableStore
    from codey.operations.completion_gate import evaluate
    from codey.operations.recovery import record_entry_policy
    from codey.runtime.effects.effect_records import RuntimeEffectStore
    from codey.runtime.effects.tool_result_delivery import ToolResultDeliveryStore
    from codey.runtime.write.mutation_line import RuntimeMutationLine
    from tests.support.kernel_runtime import (
        KernelExecutionDeps,
        KernelObservationDeps,
        KernelRunRequest,
        KernelTransportDeps,
        run_task_kernel,
    )
    from tests.support.receipt_fixtures import inspect_receipt_facts
    from tests.support.session_projection import restore_task_session
    from tests.test_session_log_receipt_recovery_preserves_facts import _dirs, _open_runtime

    project, storage, logdir = _dirs(tmp_path)
    (project / "app.py").write_text("OBSERVED-729\n", encoding="utf-8")
    _, mutations, managed, revisions, _, sink = _open_runtime(logdir, storage, "s", "r", project)
    policy = TaskPolicy(frozenset({"control", "project.read"}), required_checks=("tool_succeeded:codemode",))
    state = TaskSession(policy=policy, project=str(project), max_turns=5, verification_forbidden=True)
    record_entry_policy(mutations, session_id="s", run_id="r", policy=policy, durable=mutations.durable)
    spec = next(spec for spec in build_turn_snapshot(state, native=native).frozen_specs if spec.name == "codemode")
    example = json.loads(spec.json_examples[0])["args"]
    bad_code = {
        "host": "return require('fs').readFileSync('app.py','utf8');",
        "value": "const a = await tools.read_file({path:'app.py'}); return a.split('\\n')[0];",
        "console": "const a = await tools.read_file({path:'app.py'}); console.log(a.value.text);",
        "missing_return": "const a = await tools.read_file({path:'app.py'});",
        "missing_property": "const a = await tools.read_file({path:'app.py'}); return a.text;",
        "nested_property": "const a = await tools.read_file({path:'app.py'}); return {payload:a.text};",
        "uncalled_main": "async function main() { const a = await tools.read_file({path:'app.py'}); return a.value.text; }",
        "syntax": "const text = 'first\nsecond'; return text;",
    }[failure]

    class Provider:
        native_tools = native
        step = 0

        def advance(self, feedback, tools=()):
            index = self.step
            self.step += 1
            if index == 0:
                name, args = "codemode", {"code": bad_code}
            elif index == 1:
                if native:
                    assert spec.description in next(tool.description for tool in tools if tool.name == "codemode")
                else:
                    assert spec.description in feedback
                if failure == "syntax":
                    assert "JavaScript compilation failed" in feedback
                    assert "No child tool was executed" in feedback and "inputs.<field>" in feedback
                assert not evaluate(state, "premature success").complete
                assert not any(row.get("name") == "codemode" and row.get("ok") is True
                               for row in state.executed.values())
                if failure == "host":
                    assert "before execution" in feedback and not state.executed
                elif failure == "value":
                    assert "a.split is not a function" in feedback
                elif failure == "console":
                    assert "Console output is unavailable" in feedback
                elif failure == "missing_return":
                    assert "CodeMode returned undefined" in feedback
                elif failure == "missing_property":
                    assert "returning a nonexistent property" in feedback and "result.value" in feedback
                elif failure == "nested_property":
                    assert '$["payload"]' in feedback and "undefined" in feedback
                    assert "tools.read_file: Result<" in feedback and "tools.grep:" not in feedback
                elif failure == "uncalled_main":
                    assert "No child tool was executed" in feedback and "return await main()" in feedback
                name, args = "codemode", example
            else:
                assert index == 2 and "OBSERVED-729" in feedback
                name, args = "done", {"summary": "OBSERVED-729"}
            return (AssistantTurn(tool_calls=(ProviderToolCall(f"call-{index}", name, args),)) if native
                    else json.dumps({"tool": name, "args": args}))

        def send(self, prompt, timeout=None):
            return self.advance(prompt)

        def send_turn(self, prompt, tools=None, timeout=None):
            return self.advance(prompt, tools)

        def send_tool_results(self, results, tools=None, timeout=None):
            assert results[0].call_id == f"call-{self.step - 1}"
            return self.advance("\n".join(part for part in (results[0].content, results[0].user_content) if part), tools)

        def acknowledge_tool_results(self, results, declared_tools, timeout=None):
            return AssistantTurn(text="OBSERVED-729")

    provider = Provider()
    try:
        result = run_task_kernel(state, request=KernelRunRequest(
            transport=KernelTransportDeps(provider=provider, run_id="r",
                provider_id="local", effect_scope="task", user_task="Read app.py using codemode."),
            execution=KernelExecutionDeps(project_path=project, managed_outputs=managed, session_id="s",
                durable=mutations.durable, workspace_revision_store=revisions, permission_profile="planning_readonly"),
            observation=KernelObservationDeps(intent_sink=sink),
        ))
        assert result.completed and result.summary == "OBSERVED-729"
        assert provider.step == 3
        before = dict(state.executed)
        mutations.durable.close()
        durable = DurableSession(SQLiteDurableStore(storage / "durable.sqlite3"))
        mutations = RuntimeMutationLine(durable=durable)
        deps = SimpleNamespace(state=SimpleNamespace(durable=durable), runtime_mutations=mutations,
            runtime_effects=RuntimeEffectStore(durable=durable),
            tool_result_delivery=ToolResultDeliveryStore(durable=durable), managed_outputs=managed,
            workspace_revisions=revisions)
        recovered = inspect_receipt_facts(deps, session_id="s", run_id="r", project=str(project),
                                               task_kind="project")
        assert recovered.ok
        fresh = TaskSession(policy=policy, project=str(project), verification_forbidden=True)
        frame = SimpleNamespace(run_id="r", recovered_tool_outcomes=recovered.recovered_tool_outcomes,
            settled_tool_outcomes=recovered.settled_tool_outcomes,
            recovered_tool_result_batch_id=recovered.recovered_tool_result_batch_id)
        restore_task_session(frame, fresh)
        assert evaluate(fresh, result.summary).complete
        recovered_rows = dict(fresh.executed)
        restore_task_session(frame, fresh)
        assert fresh.executed == recovered_rows and state.executed == before
        reads = [row for row in fresh.executed.values()
                 if tool_spec.canonical_tool_name(row.get("name")) == "read_file"]
        assert len(reads) == (1 if failure in {"host", "syntax", "uncalled_main"} else 2)
        assert all(":child:" in row["call_id"] for row in reads)
        parents = [row for row in fresh.executed.values() if row.get("name") == "codemode"]
        assert sorted(row["ok"] for row in parents) == ([True] if failure == "host" else [False, True])
    finally:
        mutations.durable.close()
