from __future__ import annotations

import inspect

from codey.operations.task_loop import KernelRunRequest, run_task_kernel


def test_kernel_entry_uses_one_typed_request_boundary() -> None:
    parameters = inspect.signature(run_task_kernel).parameters.values()
    explicit = tuple(item.name for item in parameters if item.kind is not item.VAR_KEYWORD)
    assert explicit == ("session", "request")
    annotations = inspect.get_annotations(KernelRunRequest)
    assert annotations["transport"] == "KernelTransportDeps"
    assert annotations["execution"] == "KernelExecutionDeps"
    assert annotations["observation"] == "KernelObservationDeps"
