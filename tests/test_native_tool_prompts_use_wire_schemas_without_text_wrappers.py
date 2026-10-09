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
