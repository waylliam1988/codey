"""A terminal receipt closes followup calls through the sole driver."""
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from tests.support.kernel_runtime import KernelRunRequest, KernelTransportDeps, run_task_kernel
from tests.test_native_done_full_kernel_receipt import _SeqNativeProvider


def test_done_receipt_followup_is_closed_without_execution():
    provider = _SeqNativeProvider()
    result = run_task_kernel(TaskSession(TaskPolicy(frozenset({"control", "project.read"}))),
        request=KernelRunRequest(transport=KernelTransportDeps(provider=provider, provider_id="local")))
    assert result.completed, result
    assert provider.answered == [["done-1"], ["followup-1"]]
