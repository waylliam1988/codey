"""Public controls over existing owned durable tasks."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from codey.toolchain.tool_spec import ToolSpec


def task_control_specs() -> dict[str, ToolSpec]:
    from codey.toolchain.tool_spec import ToolSpec

    common: tuple[tuple[str, object], ...] = (
        ("task_ref", {"type": "string", "minLength": 1}),
        ("wait_ms", {"type": "integer", "minimum": 0, "maximum": 30000}),
    )
    output = ToolSpec(name="task_output", grant="control", executor="task_control", replay_class="safe",
        parallel_safe=True, required=("task_ref",), parameters=(*common,
            ("stdout_cursor", {"type": "integer", "minimum": 0}),
            ("stderr_cursor", {"type": "integer", "minimum": 0}),
            ("limit", {"type": "integer", "minimum": 1, "maximum": 16000})),
        description="Read owned background work and incremental output. wait_ms waits within a bound; "
        "a wait timeout leaves the work running. Each reader supplies its own cursors. "
        "Inspect the terminal result before claiming success.")
    cancel = ToolSpec(name="task_cancel", grant="control", executor="task_control", replay_class="safe",
        required=("task_ref",), parameters=common,
        description="Request cancellation of owned background work. It is stopping until cleanup finishes. "
        "Cancelling a tool task does not cancel the user's whole request.")
    target = (("task_ref", {"type": "string", "minLength": 1}),)
    input_spec = ToolSpec(name="task_input", grant="control", executor="task_control", replay_class="unsafe",
        parallel_safe=True, required=("task_ref",), parameters=(*target,
            ("data", {"type": "string", "maxLength": 65536}), ("eof", {"type": "boolean"})),
        description="Deliver UTF-8 data to an owned input-enabled process; no newline is added. "
        "eof closes pipe input after data; terminal EOF is mode-dependent. Delivery is not command success. "
        "Uncertain input is never automatically replayed.")
    interrupt = ToolSpec(name="task_signal", grant="control", executor="task_control", replay_class="unsafe",
        parallel_safe=True, required=("task_ref", "signal"), parameters=(*target,
            ("signal", {"type": "string", "enum": ["interrupt"]})),
        description="Send an interrupt to an owned terminal's foreground program; the task may remain running.")
    resize = ToolSpec(name="task_resize", grant="control", executor="task_control", replay_class="safe",
        parallel_safe=True, required=("task_ref", "cols", "rows"), parameters=(*target,
            ("cols", {"type": "integer", "minimum": 1, "maximum": 1000}),
            ("rows", {"type": "integer", "minimum": 1, "maximum": 1000})),
        description="Set the size of an owned live terminal.")
    return {spec.name: spec for spec in (output, cancel, input_spec, interrupt, resize)}
