"""Native calls use declared wire schemas; text mode keeps its JSON contract."""
import pytest

from codey.operations.kernel_prompt import _repair_prompt, kernel_prompt_for_session
from codey.operations.task_session import TaskSession

CONTRACT = '- {"tool":"edit","args":{"path":"app.py","content":"body"}}'


@pytest.mark.parametrize("native", [False, True])
def test_initial_prompt_uses_exactly_the_selected_protocol_contract(native):
    prompt = kernel_prompt_for_session(TaskSession(policy=None), native=native,
        tool_names=("edit",), contract_text=CONTRACT)
    assert (CONTRACT in prompt) is not native
    if native:
        assert "native tool schemas" in prompt


@pytest.mark.parametrize("native", [False, True])
def test_repair_prompt_does_not_teach_a_text_wrapper_to_native_models(native):
    prompt = _repair_prompt("invalid arguments", native=native, contract_text=CONTRACT)
    assert (CONTRACT in prompt) is not native
    if native:
        assert "provided native" in prompt


def test_native_final_answer_has_one_declared_transport_in_initial_and_repair():
    initial = kernel_prompt_for_session(TaskSession(policy=None), native=True,
        tool_names=("read_file", "done"), contract_text=CONTRACT)
    repair = _repair_prompt("invalid arguments", native=True, contract_text=CONTRACT)
    for prompt in (initial, repair):
        assert "summary string" in prompt
        assert "not the outer tool-call object" in prompt
        assert "normal text or call done" not in prompt


def test_nested_argument_error_uses_frozen_schema_without_unwrapping():
    from codey.operations.kernel_protocol import build_turn_snapshot, normalize_turn
    from codey.policies.task_policy import TaskPolicy
    from codey.providers.base import AssistantTurn, ProviderToolCall
    from codey.toolchain.tool_spec import ToolSpec, validate_args_against_spec, validate_args_with_spec

    args = {"args": {"query": "needle", "path": "lines.txt"}}
    state = TaskSession(TaskPolicy(frozenset({"control", "project.read"})))
    plan = normalize_turn(AssistantTurn(tool_calls=(ProviderToolCall("bad", "grep", args),)),
                          snapshot=build_turn_snapshot(state, native=True))
    assert not plan.calls
    assert "undeclared args wrapper" in plan.protocol_error
    assert "Required top-level fields: query" in plan.protocol_error
    assert args == {"args": {"query": "needle", "path": "lines.txt"}}
    assert "undeclared args wrapper" in validate_args_against_spec("grep", args)
    # A tool whose actual schema declares args must not inherit the rejection.
    custom = ToolSpec(name="custom", parameters=(("args", {"type": "object"}),), required=("args",))
    assert not validate_args_with_spec(custom, args)


def test_glob_type_error_delivers_its_canonical_parameter_guidance_without_coercion():
    from codey.operations.kernel_protocol import build_turn_snapshot, normalize_turn
    from codey.policies.task_policy import TaskPolicy
    from codey.providers.base import AssistantTurn, ProviderToolCall

    args = {"query": "needle", "include": ["*.py", "*.ts"]}
    state = TaskSession(TaskPolicy(frozenset({"control", "project.read"})))
    plan = normalize_turn(AssistantTurn(tool_calls=(ProviderToolCall("bad", "grep", args),)),
                          snapshot=build_turn_snapshot(state, native=True))
    assert not plan.calls
    assert "not an array" in plan.protocol_error and "*.{py,ts}" in plan.protocol_error
    assert args["include"] == ["*.py", "*.ts"]
