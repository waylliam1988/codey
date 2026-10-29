"""Reproductions for disagreements between a sent contract and the kernel."""
import json

import pytest

from codey.codemode.catalog import build_codemode_catalog
from codey.codemode.kernel import build_execute_turn_executor
from codey.operations.kernel_execution import execute_turn
from codey.operations.kernel_preparation import prepare_kernel_turn
from codey.operations.kernel_protocol import build_turn_snapshot, normalize_turn
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.protocols.text_turn import decode_text_turn
from codey.providers.base import AssistantTurn, ProviderToolCall, tools_from_specs
from codey.runtime.core.models import ToolCall
from codey.toolchain import tool_spec


def session(*, write=False, project=""):
    grants = {"control", "project.read"} | ({"project.write"} if write else set())
    return TaskSession(policy=TaskPolicy(frozenset(grants)), project=str(project))


def test_native_visible_names_are_exactly_the_sent_wire_schema_names():
    prepared = prepare_kernel_turn(session(), user_task="Read two files", context_text="",
                                   task_guidance="", native=True)
    wire_names = {tool.name for tool in tools_from_specs(prepared.snapshot.frozen_specs)}
    assert "read_file" in wire_names
    assert set(prepared.snapshot.tool_names) == wire_names
    assert "read_files" not in prepared.prompt and "parallel" not in prepared.prompt
    # Native wrappers are not executable and must not be recommended.
    rejected = normalize_turn(AssistantTurn(tool_calls=(
        ProviderToolCall("bad-wrapper", "read_files", {"paths": ["a.py", "b.py"]}),
    )), snapshot=prepared.snapshot)
    assert rejected.protocol_error
    text = build_turn_snapshot(session())
    assert {"read_files", "parallel"}.issubset(text.tool_names)
    assert not normalize_turn(decode_text_turn(
        '{"tool":"read_files","args":{"paths":["a.py","b.py"]}}'), snapshot=text).protocol_error


def test_every_advertised_json_example_can_be_decoded_and_normalized():
    from codey.policies.capabilities import KNOWN_TASK_GRANTS

    state = TaskSession(policy=TaskPolicy(frozenset(KNOWN_TASK_GRANTS)))
    state.record_search_result("r1", "https://example.com/")
    state.record_open("https://example.com/")
    state.hit_targets["h1"] = {"url": "https://example.com/", "offset": 0, "pages": ""}
    snapshot = build_turn_snapshot(state)
    for spec in snapshot.frozen_specs:
        for example in spec.json_examples:
            assert json.loads(example), spec.name
            plan = normalize_turn(decode_text_turn(example), snapshot=snapshot)
            assert not plan.protocol_error, (spec.name, plan.protocol_error, example)


def test_frozen_contract_rendering_does_not_reenter_the_live_registry(monkeypatch):
    from codey.operations.kernel_prompt import kernel_prompt_for_session

    state = session()
    snapshot = build_turn_snapshot(state)
    frozen = snapshot.frozen_specs
    def changed_registry():
        raise AssertionError("frozen rendering reentered the live registry")
    monkeypatch.setattr(tool_spec, "tool_specs", changed_registry)
    assert tool_spec.json_contract_text(frozen) == snapshot.contract_text
    prompt = kernel_prompt_for_session(state, tool_names=snapshot.tool_names, contract_text=snapshot.contract_text)
    assert "Visible tools: " + ", ".join(snapshot.tool_names) in prompt


def test_registry_removal_cannot_erase_a_frozen_text_declaration():
    name = "frozen_contract_audit_probe"
    assert tool_spec.register_custom_tool(name, grant="control")
    try:
        snapshot = build_turn_snapshot(session())
        assert name in snapshot.contract_text
        frozen = snapshot.frozen_specs
        assert tool_spec.unregister_custom_tool(name)
        assert tool_spec.json_contract_text(frozen) == snapshot.contract_text
    finally:
        tool_spec.unregister_custom_tool(name)


def test_registry_change_during_snapshot_cannot_split_the_sent_text_contract(monkeypatch):
    from codey.operations import kernel_protocol
    from tests.support.kernel_runtime import KernelRunRequest, KernelTransportDeps, run_task_kernel

    name = "captured_contract_churn_probe"
    assert tool_spec.register_custom_tool(name, grant="control")
    expected = tool_spec.tool_specs()[name].json_examples[0]
    capture = kernel_protocol._frozen_specs_for_names
    def remove_after_capture(names):
        frozen = capture(names)
        tool_spec.unregister_custom_tool(name)
        return frozen
    monkeypatch.setattr(kernel_protocol, "_frozen_specs_for_names", remove_after_capture)
    prompts = []
    class Provider:
        def send(self, prompt, timeout=None):
            prompts.append(prompt)
            return '{"tool":"done","args":{"summary":"No action needed"}}'
    try:
        result = run_task_kernel(session(), request=KernelRunRequest(
            transport=KernelTransportDeps(provider=Provider(), user_task="No action needed")))
        assert result.completed
        visible = next(line for line in prompts[0].splitlines() if line.startswith("Visible tools:"))
        assert name in visible
        assert expected in prompts[0], "captured tool has no declaration in the actual sent contract"
    finally:
        tool_spec.unregister_custom_tool(name)


@pytest.mark.parametrize("name,args", [
    ("read_files", {"paths": ["a.py", "b.py"]}),
    ("parallel", {"calls": [{"tool": "read_file", "args": {"path": "a.py"}}]}),
])
def test_codemode_catalog_does_not_advertise_unexecutable_text_wrappers(tmp_path, name, args):
    (tmp_path / "a.py").write_text("a = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("b = 2\n", encoding="utf-8")
    state = session(project=tmp_path)
    snapshot = build_turn_snapshot(state)
    catalog = build_codemode_catalog(snapshot.frozen_specs)
    # Text wrapper lowering occurs before execution, not in CodeMode RPC.
    result = execute_turn(state, [ToolCall(name, args, call_id="child-wrapper")],
                          snapshot=snapshot, project_path=tmp_path, turn=1)[0]
    assert not result.ok
    assert f"unknown tool: {name}" in result.model_text
    assert name not in {entry.name for entry in catalog.entries}
    assert "read_file" in {entry.name for entry in catalog.entries}


def test_codemode_description_does_not_deny_real_authorized_edits(tmp_path):
    from codey.workspace.revision import WorkspaceRevisionStore

    project = tmp_path / "project"
    project.mkdir()
    state = session(write=True, project=project)
    snapshot = build_turn_snapshot(state)
    revisions = WorkspaceRevisionStore(tmp_path / "state")
    executor = build_execute_turn_executor(session=state, snapshot=snapshot, run_id="r", effect_scope="task",
        turn=1, project_path=project, workspace_revision_store=revisions, allow_writes=True)
    result = executor(ToolCall("codemode", {
        "code": "return await tools.edit({path:'created.txt',content:'observed edit'});",
    }))
    assert result.ok and json.loads(result.model_text)["ok"], result.model_text
    assert (project / "created.txt").read_text(encoding="utf-8") == "observed edit"
    description = next(tool.description for tool in tools_from_specs(snapshot.frozen_specs)
                       if tool.name == "codemode")
    assert "writes, approval" not in description
    assert "Only frozen read-only tools" not in description


@pytest.mark.parametrize("native", [False, True])
def test_documented_codemode_example_executes_with_real_child_tools(tmp_path, native):
    (tmp_path / "app.py").write_text("OBSERVED-CODEMODE-729\n", encoding="utf-8")
    state = session(project=tmp_path)
    snapshot = build_turn_snapshot(state, native=native)
    spec = next(spec for spec in snapshot.frozen_specs if spec.name == "codemode")
    example = json.loads(spec.json_examples[0])
    reply = (AssistantTurn(tool_calls=(ProviderToolCall("example-call", "codemode", example["args"]),))
             if native else decode_text_turn(spec.json_examples[0]))
    plan = normalize_turn(reply, snapshot=snapshot)
    assert not plan.protocol_error
    executor = build_execute_turn_executor(session=state, snapshot=snapshot, run_id="example",
                                           effect_scope="task", turn=1, project_path=tmp_path)
    result = execute_turn(state, plan.calls, snapshot=snapshot, executors={"codemode": executor},
                          run_id="example", turn=1, project_path=tmp_path)[0]
    observed = json.loads(result.model_text)
    assert result.ok
    assert observed == {"ok": True, "value": "OBSERVED-CODEMODE-729\n"}
    # Both surfaces teach the very same program, not independently escaped copies.
    assert example["args"]["code"] in spec.description
    assert "Calls return {ok:true, value}" in spec.description


@pytest.mark.parametrize("native", [False, True])
def test_project_audit_uses_only_the_kernel_protocol_contract(tmp_path, native):
    from tests.support.kernel_runtime import run_project_audit_advisor

    sent = []
    class Provider:
        native_tools = native

        def send(self, prompt, timeout=None):
            sent.append((prompt, []))
            return '{"tool":"done","args":{"summary":"No findings"}}'

        def send_turn(self, prompt, tools, timeout=None):
            sent.append((prompt, tools))
            return AssistantTurn(text="No findings")

        def send_tool_results(self, *args, **kwargs):
            raise AssertionError("no tool calls to answer")

    assert run_project_audit_advisor(Provider(), tmp_path, "Inspect this project",
        parent_policy=TaskPolicy(frozenset({"control", "project.read"}))) == "No findings"
    prompt, tools = sent[0]
    assert "Every reply MUST be exactly one JSON object" not in prompt
    assert "Available read-only tools:" not in prompt
    assert "Use only list_dir, read_file, read_files" not in prompt
    assert prompt.count("Visible tools:") == 1
    if native:
        assert '"tool":"read_files"' not in prompt and '"tool":"parallel"' not in prompt
        assert "use native tool calls, not JSON in prose" in prompt
        assert "read_file" in {tool.name for tool in tools}
    else:
        assert '"tool":"read_files"' in prompt
